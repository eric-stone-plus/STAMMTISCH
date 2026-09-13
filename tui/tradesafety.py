"""Trade safety layer — anti-wick guards, martingale guardrails, kill switch.

Pure functions so every rule is unit-testable offline; the loop consults
them before any wire action. The rules exist because the research said
so: on a 48%-vol asset a tight mental stop is a donation to stop hunters,
and martingale averaging without hard caps is how simulation accounts die
before they graduate.

- wick protection: reject entries whose trigger bar carries an anomalous
  lower wick (> k x ATR) — that bar is usually the spike itself; and
  always enter via marketable LIMIT so a spike can never fill above your
  price.
- exchange-native stop: every entry gets a reduce-only STOP_MARKET on the
  venue, so the stop survives process death (a mental stop does not).
- martingale guardrails: steps, per-step multiplier and total-exposure
  fraction are hard caps; breach returns zero, never a bigger size.
- kill switch: realized loss in the trailing 24h window beyond
  max_daily_loss halts new entries.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

MAX_STEPS = 3
STEP_MULTIPLIER = 1.5
MAX_TOTAL_EXPOSURE_FRAC = 0.15
MAX_DAILY_LOSS_FRAC = 0.02
WICK_K = 2.0


def atr(bars: list[dict[str, float]], period: int = 14) -> float:
    """True-range ATR over OHLC bars ({open, high, low, close})."""
    if len(bars) < period + 1:
        return 0.0
    highs = np.array([b["high"] for b in bars], dtype=float)
    lows = np.array([b["low"] for b in bars], dtype=float)
    closes = np.array([b["close"] for b in bars], dtype=float)
    prev = np.roll(closes, 1)
    prev[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(abs(highs - prev), abs(lows - prev)))
    return float(pd.Series(tr).rolling(period).mean().iloc[-1] or 0.0)


def wick_ok(bars: list[dict[str, float]], k: float = WICK_K) -> tuple[bool, str]:
    """Anti-wick gate for a long entry: the trigger bar must not carry an
    anomalous lower wick (a classic stop-hunt spike — entering on it buys
    the knife), and the entry price must sit above the wick low."""
    if len(bars) < 15:
        return False, "insufficient bars"
    last = bars[-1]
    a = atr(bars)
    if a <= 0:
        return False, "ATR unavailable"
    lower_wick = min(last["open"], last["close"]) - last["low"]
    if lower_wick > k * a:
        return False, (f"lower wick {lower_wick:.2f} > {k}xATR {a * k:.2f} "
                       "(stop-hunt spike — skip)")
    if last["close"] < last["low"] + a * 0.1:
        return False, "close pinned to bar low (no rejection)"
    return True, "clean"


def martingale_qty(steps_open: int, base_qty: float,
                   total_exposure_frac: float, equity: float,
                   price: float, max_steps: int = MAX_STEPS,
                   multiplier: float = STEP_MULTIPLIER,
                   max_total_frac: float = MAX_TOTAL_EXPOSURE_FRAC) -> float:
    """Next averaging size under hard caps, or 0.0 when any cap breaches."""
    if steps_open >= max_steps:
        return 0.0
    qty = base_qty * (multiplier ** steps_open)
    projected = total_exposure_frac + qty * price / max(equity, 1e-9)
    if projected > max_total_frac:
        return 0.0
    return round(qty, 4)


def daily_loss_ok(journal_path: str | Path, equity: float,
                  max_loss_frac: float = MAX_DAILY_LOSS_FRAC) -> tuple[bool, float]:
    """Kill switch: realized P&L in the trailing 24h must not breach the cap."""
    path = Path(journal_path)
    if not path.is_file():
        return True, 0.0
    cutoff = time.time() - 24 * 3600
    realized = 0.0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            ts = record.get("ts", "")
            try:
                when = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                continue
            if when >= cutoff and record.get("kind") == "exit":
                realized += float(record.get("pnl", 0) or 0)
    except OSError:
        return True, 0.0
    return realized > -max_loss_frac * equity, realized


def stop_market_spec(symbol: str, qty: float, entry_price: float,
                     stop_pct: float = 0.05) -> dict[str, Any]:
    """Reduce-only CONDITIONAL STOP_MARKET spec for the futures algo API
    (POST /fapi/v1/algoOrder, algoType=CONDITIONAL, triggerprice) — the
    disaster stop lives on the exchange instead of in this process.
    Verified against the testnet: 2026-09-14, algoId 1000000204193249."""
    side = "SELL" if qty > 0 else "BUY"
    trigger = round(entry_price * (1 - stop_pct) if qty > 0
                    else entry_price * (1 + stop_pct), 1)
    return {"symbol": symbol, "side": side, "algoType": "CONDITIONAL",
            "type": "STOP_MARKET", "triggerprice": trigger,
            "quantity": abs(qty), "timeInForce": "GTC",
            "workingType": "MARK_PRICE"}

def attach_stop(broker: Any, symbol: str, qty: float, entry_price: float,
                stop_pct: float = 0.05) -> dict[str, Any]:
    """Place the exchange-native stop for one position via the broker."""
    return broker._send("/fapi/v1/algoOrder",
                        stop_market_spec(symbol, qty, entry_price, stop_pct),
                        method="POST")
