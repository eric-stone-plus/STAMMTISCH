"""LedgerService — FIFO positions from the trade ledger, out of the screen.

Copy + adapt (no ``tui.`` import) of the old LEDGER screen's data side:

- the fill-store read contract is copied from ``services/portfolio.py:26-41``
  (``ledger_path`` = ``<state_root>/intel/portfolio/ledger.json``;
  missing file → empty; unreadable/no-fills-list → ``LedgerError``);
- the FIFO fold is copied verbatim from ``services/portfolio.py:94-138``
  (``positions``): buys open long lots, sells close the OLDEST open
  lots first (realized P&L booked at the close price), excess sells
  open short lots a later buy closes; per-(broker, symbol) books keyed
  and sorted; unparseable/zero-qty fills are skipped, never fatal;
- the mark pass is the old screen's quote step (``tui/screens/ledger.py:
  78-92``) made injectable: quotes come from a callable (the interface
  feeds lane by default), a missing mark stays ``None`` and the screen
  renders an honest em dash — never a fabricated price.

M6 scope: READ ONLY. The old write ops (``add_fill`` / ``remove_fill``)
stay in the old tree — they need the confirm lane (:delete's audited
pattern) and are deliberately deferred. This module never writes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

__all__ = [
    "FillRow",
    "LedgerError",
    "LedgerFrame",
    "LedgerService",
    "PositionRow",
    "ledger_path",
    "load_fills",
]

#: One quote row shape ({last: float, ...}); None marks stay honest.
QuoteRow = Mapping[str, Any]
QuoteFn = Callable[[Sequence[str]], Mapping[str, QuoteRow]]


class LedgerError(ValueError):
    """Invalid fill data or a corrupt ledger file (old portfolio.py:22)."""


def ledger_path(state_root: str | Path) -> Path:
    """``<state_root>/intel/portfolio/ledger.json`` (old portfolio.py:26)."""
    return Path(state_root) / "intel" / "portfolio" / "ledger.json"


def load_fills(state_root: str | Path | None) -> list[dict[str, Any]]:
    """Read the fill list; missing file is empty, anything else raises.

    Copy of services/portfolio.py:30-41 (the read half only — M6 writes
    nothing). ``None`` state root reads as no ledger at all (the shell's
    demo/no-root path stays honest instead of fishing a conventional
    default the operator never chose).
    """
    if state_root is None:
        return []
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


@dataclass(frozen=True)
class FillRow:
    """One logged fill, defensively coerced (old screen rows 130-141)."""

    id: str
    ts: str
    broker: str
    side: str
    symbol: str
    qty: float
    price: float
    fee: float


@dataclass(frozen=True)
class PositionRow:
    """One per-(broker, symbol) FIFO fold row (old portfolio.py:125-137)."""

    broker: str
    symbol: str
    net_qty: float
    avg_cost: float
    realized_pnl: float
    #: Mark fields (old ledger.py:113-116): None when the feeds lane does
    #: not know the symbol — the screen renders an honest em dash.
    last: float | None = None
    unrealized: float | None = None


@dataclass(frozen=True)
class LedgerFrame:
    """One ledger read: the fills in file order + the marked positions."""

    fills: tuple[FillRow, ...] = ()
    positions: tuple[PositionRow, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _fill_row(fill: Mapping[str, Any]) -> FillRow:
    try:
        qty = float(fill.get("qty") or 0)
        price = float(fill.get("price") or 0)
        fee = float(fill.get("fee") or 0)
    except (TypeError, ValueError):
        qty = price = fee = 0.0
    return FillRow(
        id=str(fill.get("id") or ""),
        ts=str(fill.get("ts") or ""),
        broker=str(fill.get("broker") or ""),
        side=str(fill.get("side") or "?"),
        symbol=str(fill.get("symbol") or "?"),
        qty=qty, price=price, fee=fee,
    )


def fold_positions(fills: Sequence[Mapping[str, Any]]) -> list[PositionRow]:
    """Fold fills into per-(broker, symbol) rows via signed FIFO.

    Copy of services/portfolio.py:94-138: ts-then-id order, oldest-lot-first
    matching, realized P&L booked on every close, weighted average cost
    over the remaining lots. Unparseable or non-positive fills are
    skipped (the fold never raises on bad data).
    """
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
                realized[key] = (realized.get(key, 0.0)
                                 + (price - lot["price"]) * matched)
            else:
                realized[key] = (realized.get(key, 0.0)
                                 + (lot["price"] - price) * matched)
            lot["qty"] -= matched if lot["qty"] > 0 else -matched
            if abs(lot["qty"]) < 1e-12:
                lots.pop(0)
            signed -= matched if signed > 0 else -matched
        if abs(signed) > 1e-12:
            lots.append({"qty": signed, "price": price})

    rows: list[PositionRow] = []
    for key in sorted(books):
        lots = books[key]
        net = sum(lot["qty"] for lot in lots)
        cost = sum(abs(lot["qty"]) * lot["price"] for lot in lots)
        size = sum(abs(lot["qty"]) for lot in lots)
        rows.append(PositionRow(
            broker=key[0], symbol=key[1], net_qty=net,
            avg_cost=(cost / size) if size else 0.0,
            realized_pnl=realized.get(key, 0.0),
        ))
    return rows


def _real_quotes(symbols: Sequence[str]) -> Mapping[str, QuoteRow]:
    """Default quote leg: the services feeds lane, resolved at call time."""
    from interface.services.feeds import fetch_batch

    return fetch_batch(list(symbols))


class LedgerService:
    """Read the ledger, fold positions, mark to live behind a quote fn."""

    def __init__(self, load: Callable[[str | Path | None],
                                      list[dict[str, Any]]] | None = None,
                 quotes: QuoteFn | None = None) -> None:
        self._load = load
        self._quotes = quotes if quotes is not None else _real_quotes

    def snapshot(self, state_root: str | Path | None) -> LedgerFrame:
        """One ledger read + fold + mark; degrades to an error frame."""
        try:
            fills = (self._load(state_root) if self._load is not None
                     else load_fills(state_root))
        except LedgerError as exc:
            return LedgerFrame(error=str(exc))
        rows = fold_positions(fills)
        symbols = sorted({row.symbol for row in rows if row.symbol})
        quotes: Mapping[str, QuoteRow] = {}
        if symbols:
            try:
                quotes = self._quotes(symbols) or {}
            except Exception:  # noqa: BLE001 - old ledger.py:87-89 pass-branch
                quotes = {}
        marked = tuple(_mark(row, quotes.get(row.symbol)) for row in rows)
        return LedgerFrame(fills=tuple(_fill_row(f) for f in fills),
                           positions=marked)


def _mark(row: PositionRow, quote: QuoteRow | None) -> PositionRow:
    """Apply one live mark; no quote → no mark (None stays honest)."""
    if not quote or quote.get("last") is None:
        return row
    try:
        last = float(quote["last"])
    except (TypeError, ValueError):
        return row
    return replace(row, last=last,
                   unrealized=(last - row.avg_cost) * row.net_qty)
