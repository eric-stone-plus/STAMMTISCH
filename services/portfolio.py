"""Trade ledger — FIFO positions from logged fills.

A TUI-side bookkeeping file (``<state_root>/intel/portfolio/ledger.json``):
every sandbox fill you log lands here, and positions are folded from the
fill list with signed FIFO accounting — buys open long lots, sells close
the oldest open lots first (realized P&L), and excess sells open short
lots that a later buy closes. The ledger is bookkeeping, not pipeline
evidence: the events-are-authority rule never applies to it, but writes
are still atomic (temp file + rename) so a crash cannot truncate it.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


class LedgerError(ValueError):
    """Invalid fill data or a corrupt ledger file."""


def ledger_path(state_root: str | Path) -> Path:
    return Path(state_root) / "intel" / "portfolio" / "ledger.json"


def load(state_root: str | Path) -> list[dict[str, Any]]:
    path = ledger_path(state_root)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"ledger at {path} is unreadable: {exc}") from exc
    fills = payload.get("fills") if isinstance(payload, dict) else None
    if not isinstance(fills, list):
        raise LedgerError(f"ledger at {path} has no fills list")
    return fills


def save(state_root: str | Path, fills: list[dict[str, Any]]) -> None:
    path = ledger_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "fills": fills},
                              indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def add_fill(state_root: str | Path, side: str, symbol: str, qty: float,
             price: float, broker: str = "", fee: float = 0.0) -> dict[str, Any]:
    """Validate, stamp, and persist one fill; returns the stored record."""
    side = str(side).strip().lower()
    symbol = str(symbol).strip().upper()
    if side not in ("buy", "sell"):
        raise LedgerError(f"invalid side {side!r}")
    if not symbol:
        raise LedgerError("symbol is required")
    qty, price, fee = float(qty), float(price), float(fee)
    if qty <= 0:
        raise LedgerError("quantity must be positive")
    if price < 0:
        raise LedgerError("price must not be negative")
    if fee < 0:
        raise LedgerError("fee must not be negative")
    fills = load(state_root)
    fill = {
        "id": uuid.uuid4().hex[:8],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "broker": broker.strip().lower(),
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "fee": fee,
    }
    fills.append(fill)
    save(state_root, fills)
    return fill


def remove_fill(state_root: str | Path, fill_id: str) -> bool:
    fills = load(state_root)
    kept = [fill for fill in fills if fill.get("id") != fill_id]
    if len(kept) == len(fills):
        return False
    save(state_root, kept)
    return True


def positions(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold fills into per-(broker, symbol) rows via signed FIFO."""
    ordered = sorted(fills, key=lambda fill: (str(fill.get("ts") or ""),
                                              str(fill.get("id") or "")))
    books: dict[tuple[str, str], list[dict[str, float]]] = {}
    realized: dict[tuple[str, str], float] = {}
    for fill in ordered:
        try:
            qty = float(fill["qty"])
            price = float(fill["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        key = (str(fill.get("broker") or ""), str(fill.get("symbol") or ""))
        signed = qty if str(fill.get("side")) == "buy" else -qty
        lots = books.setdefault(key, [])
        while lots and signed != 0 and (lots[0]["qty"] > 0) != (signed > 0):
            lot = lots[0]
            matched = min(abs(signed), abs(lot["qty"]))
            if lot["qty"] > 0:
                realized[key] = realized.get(key, 0.0) + (price - lot["price"]) * matched
            else:
                realized[key] = realized.get(key, 0.0) + (lot["price"] - price) * matched
            lot["qty"] -= matched if lot["qty"] > 0 else -matched
            if abs(lot["qty"]) < 1e-12:
                lots.pop(0)
            signed -= matched if signed > 0 else -matched
        if abs(signed) > 1e-12:
            lots.append({"qty": signed, "price": price})

    rows = []
    for key in sorted(books):
        lots = books[key]
        net = sum(lot["qty"] for lot in lots)
        cost = sum(abs(lot["qty"]) * lot["price"] for lot in lots)
        size = sum(abs(lot["qty"]) for lot in lots)
        rows.append({
            "broker": key[0],
            "symbol": key[1],
            "net_qty": net,
            "avg_cost": (cost / size) if size else 0.0,
            "realized_pnl": realized.get(key, 0.0),
        })
    return rows
