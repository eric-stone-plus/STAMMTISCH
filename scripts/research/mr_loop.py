"""Resident RSI(2) mean-reversion loop for the Binance futures testnet.

Implements the strategy-v2 intraday leg as an operator loop: poll the
1h bars for the MR pair (ETHUSDT — BTCUSDT is reserved for the hedge
leg), enter a 5%-notional long when RSI(2) crosses below the entry
threshold, exit above the exit threshold or after max-hold bars, and
journal every decision to <state_root>/intel/mr-loop.jsonl. Gated by
trading_mode=paper and the USDT peg alert, fail-closed on transport.

Usage: python scripts/research/mr_loop.py --once   # one cycle
       python scripts/research/mr_loop.py --loop   # every 5 minutes
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tui.brokers.binance import binance_broker  # noqa: E402
from tui.config import Config  # noqa: E402
from tui.datafeeds import http as dfhttp  # noqa: E402
from tui.datafeeds.http import (  # noqa: E402
    configure_data_proxy, configure_proxy_fallback)

PAIR = "ETHUSDT"
ENTRY_RSI = 10.0
EXIT_RSI = 70.0
MAX_HOLD_HOURS = 24
SIZE_FRACTION = 0.05
USDT_DEV_ALERT = 0.001

def journal(state_root: str, record: dict) -> None:
    path = Path(state_root) / "intel" / "mr-loop.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
             **record}, ensure_ascii=False) + "\n")

def usdt_peg_ok() -> tuple[bool | None, float | None]:
    """True/False when the peg is measurable; None when the check itself
    fails (rate-limited shared egress) — None means trade-skip, never a
    guessed pass."""
    try:
        payload = dfhttp.get_json(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=tether&vs_currencies=usd", provider="coingecko")
        price = float(payload["tether"]["usd"])
    except Exception:
        return None, None
    return abs(price - 1.0) <= USDT_DEV_ALERT, price

def rsi2_last(symbol: str) -> tuple[float, float]:
    raw = dfhttp.get_json(
        f"https://api.binance.com/api/v3/klines?symbol={symbol}"
        "&interval=1h&limit=200", provider="binance")
    closes = [float(k[4]) for k in raw]
    deltas = pd_diff(closes)
    return deltas, closes[-1]

def pd_diff(closes):
    import pandas as pd
    s = pd.Series(closes)
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    rsi = (100 - 100 / (1 + gain / loss.replace(0, pd.NA))).fillna(100.0)
    return float(rsi.iloc[-1])

def cycle(state_root: str) -> dict:
    from tui.brokers.gate import trading_mode

    record: dict = {"mode": trading_mode(config)}
    peg_ok, peg_price = usdt_peg_ok()
    record["usdt"] = peg_price
    if peg_ok is None:
        record["action"] = "SKIPPED: peg check unavailable (fail-closed)"
        return record
    if not peg_ok:
        record["action"] = "SKIPPED: USDT peg beyond alert band"
        return record
    if record["mode"] != "paper":
        record["action"] = "SKIPPED: trading_mode not paper"
        return record

    r2, last = rsi2_last(PAIR)
    record["rsi2"], record["last"] = round(r2, 1), last
    position_amt = 0.0
    for pos in broker.positions():
        if pos.get("symbol") == PAIR:
            position_amt = float(pos.get("positionAmt", 0))
    record["position"] = position_amt

    entry_state = json.loads(
        (Path(state_root) / "intel" / "mr-entry.json").read_text()
    ) if (Path(state_root) / "intel" / "mr-entry.json").is_file() else None

    if position_amt > 0 and entry_state:
        held_hours = (time.time() - float(entry_state["ts"])) / 3600.0
        if r2 > EXIT_RSI or held_hours > MAX_HOLD_HOURS:
            out = broker.place_limit_order(
                PAIR, "sell", entry_state["qty"],
                f"{round(last * 0.998, 2):.2f}")
            record["action"] = f"EXIT {out.get('status')}"
            record["orderId"] = out.get("orderId")
            (Path(state_root) / "intel" / "mr-entry.json").unlink()
        else:
            record["action"] = f"HOLDING ({held_hours:.1f}h, r2 {r2:.1f})"
    elif position_amt == 0 and r2 < ENTRY_RSI:
        available = float(broker.account().get("availableBalance") or 0)
        qty = max(0.01, round(available * SIZE_FRACTION / last, 2))
        out = broker.place_limit_order(PAIR, "buy", f"{qty:.2f}",
                                       f"{round(last * 1.002, 2):.2f}")
        record["action"] = f"ENTRY {out.get('status')}"
        record["orderId"] = out.get("orderId")
        (Path(state_root) / "intel" / "mr-entry.json").write_text(json.dumps(
            {"ts": time.time(), "qty": f"{qty:.2f}", "entry_ref": last,
             "orderId": out.get("orderId")}))
    else:
        record["action"] = "WAIT"
    return record

def safe_cycle(state_root: str) -> dict:
    """A transport failure (429, timeout) journals and waits — never kills
    the loop, never trades on unverified state."""
    try:
        return cycle(state_root)
    except Exception as exc:
        return {"action": f"ERROR (skip cycle): {type(exc).__name__}: {exc}"[:220]}

config = Config()
configure_data_proxy(config.data_proxy_url)
configure_proxy_fallback(config.egress_proxy_url)
broker = binance_broker(config)
state_root = config.state_root or str(Path.home() / ".local/share/stammtisch")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=300)
    args = parser.parse_args()
    if args.loop:
        while True:
            record = safe_cycle(state_root)
            journal(state_root, record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            time.sleep(args.interval)
    else:
        record = safe_cycle(state_root)
        journal(state_root, record)
        print(json.dumps(record, ensure_ascii=False))
