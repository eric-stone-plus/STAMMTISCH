"""Full-market backtest screener for the daily decision runner.

When no manual watchlist is configured the decision has to come from the
whole cached market, not a preference universe: every parquet in the
quantkit cache gets a vectorized dual_ma backtest with the exact
semantics of ``QuantEngine.run_backtest`` (same MA pair, same next-bar
position shift, same cost tier), the passing names are ranked by total
return, and only the finalists go through the authoritative engine pass.
The screen is the funnel; the engine stays the source of truth for every
number the decision model sees.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .engine import QuantEngine
from .screens.domains import security_zone

# Quality floors a candidate must clear before ranking: enough bars in
# the window for the slow MA to mean anything, enough trades to prove
# the signal is not one lucky round trip, and risk shapes a real book
# can hold. These are admission floors, not a recommendation.
MIN_BARS = 300
MIN_TRADES = 6
MIN_SHARPE = 0.5
MIN_MAXDD = -0.55  # drawdown floor: worse than -55% disqualifies

FAST = 20
SLOW = 50
# Per-side rates in bps. quantkit COST_TIERS declare a round-trip
# (both legs) rate and charge half per unit of one-sided turnover
# (quantkit/backtest.py: ``per_side_bps = notional_bps / 2``), so the
# mirror carries the same halved per-side numbers — parity with the
# engine pass the decision chain trusts.
COST_TIER_BPS = {"low": 10.0, "mid": 20.0, "high": 25.0}


def _symbol_of(path: Path) -> str | None:
    # <provider>_auto_<SYMBOL>_1d_<start>_<end>.parquet -> SYMBOL
    parts = path.name.split("_")
    for index, part in enumerate(parts):
        if part == "auto" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def _dual_ma_metrics(close: np.ndarray, per_side_bps: float) -> dict[str, float] | None:
    """Numpy mirror of quantkit run_long_only + dual_ma_signal.

    Position = (MA20 > MA50) evaluated at bar close, taken next bar;
    cost = |position change| * per-side bps. The metrics match the
    engine's summary to rounding, which keeps the screen's ranking and
    the engine's verification consistent.
    """
    n = len(close)
    if n < max(SLOW + 2, MIN_BARS):
        return None
    csum = np.cumsum(close)
    ma_fast = (csum[FAST - 1:] - np.concatenate(([0.0], csum[: n - FAST]))) / FAST
    ma_slow = (csum[SLOW - 1:] - np.concatenate(([0.0], csum[: n - SLOW]))) / SLOW
    # full-length arrays, zeros before the slow MA exists — mirrors the
    # pandas path where rolling(NaN) comparisons collapse to 0.0
    sig = np.zeros(n)
    sig[SLOW - 1:] = ma_fast[SLOW - FAST:] > ma_slow
    pos = np.zeros(n)  # next-bar execution
    pos[1:] = sig[:-1]
    ret = np.zeros(n)
    ret[1:] = close[1:] / close[:-1] - 1.0
    changes = np.diff(pos, prepend=0.0)
    strat = pos * ret - np.abs(changes) * (per_side_bps / 10_000.0)
    equity = np.cumprod(1.0 + strat)
    trades = int((np.abs(changes) > 1e-12).sum())
    active = strat[pos > 1e-12]
    std = float(strat.std())
    peak = np.maximum.accumulate(equity)
    return {
        "tr": float(equity[-1] - 1.0),
        "sharpe": float(strat.mean() / std * np.sqrt(252.0)) if std > 0 else 0.0,
        "maxdd": float(((equity - peak) / peak).min()),
        "trades": trades,
        "win": float((active > 0).mean()) if len(active) else 0.0,
    }


def _screen_one(path: Path, per_side_bps: float,
                window_bars: int) -> tuple[str, Any, dict[str, float]] | None:
    symbol = _symbol_of(path)
    if symbol is None:
        return None
    try:
        close = pd.read_parquet(path, columns=["close"])["close"].dropna()
    except Exception:
        return None
    if close.empty:
        return None
    metrics = _dual_ma_metrics(
        close.tail(window_bars).to_numpy(dtype=np.float64), per_side_bps)
    if metrics is None:
        return None
    return symbol, close.index[-1], metrics


def screen_market(engine: QuantEngine, per_zone: int = 40,
                  window_bars: int = 500, cost_tier: str = "low",
                  workers: int = 8) -> dict[str, list[dict[str, Any]]]:
    """Rank the whole cache per market zone; returns top rows per zone.

    Duplicate snapshots of one symbol resolve to the latest end date —
    the same rule the post-training corpus uses.
    """
    cache = Path(engine.data_dir) / "cache"
    if not cache.is_dir():
        return {}
    per_side_bps = COST_TIER_BPS[cost_tier]
    paths = sorted(cache.glob("*_auto_*_1d_*.parquet"))

    best: dict[str, tuple[Any, dict[str, float]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(lambda p: _screen_one(p, per_side_bps, window_bars),
                             paths):
            if item is None:
                continue
            symbol, end_ts, metrics = item
            held = best.get(symbol)
            if held is None or str(end_ts) > str(held[0]):
                best[symbol] = (end_ts, metrics)

    zones: dict[str, list[dict[str, Any]]] = {}
    for symbol, (_, m) in best.items():
        if (m["trades"] < MIN_TRADES or m["sharpe"] < MIN_SHARPE
                or m["maxdd"] < MIN_MAXDD):
            continue
        zones.setdefault(security_zone(symbol), []).append({
            "symbol": symbol,
            "tr": round(m["tr"] * 100, 1),
            "sharpe": round(m["sharpe"], 2),
            "maxdd": round(m["maxdd"] * 100, 1),
            "win": round(m["win"] * 100),
            "trades": int(m["trades"]),
        })
    for rows in zones.values():
        rows.sort(key=lambda r: r["tr"], reverse=True)
    return {zone: rows[:per_zone] for zone, rows in zones.items()
            if zone != "OTHER" and rows}
