"""Batch live quotes + market-session clock for the realtime boards.

One Tencent batch request per poll covers the whole visible zone
(free, unauthenticated, GBK, CN-side so no egress is involved). Symbols
Tencent misses or that fail when Tencent is down fall back per-symbol to
the Yahoo chart API through the datafeeds service — every returned row
carries the provider it came from.

Sources are explicit: every quote carries its endpoint, and every board
that renders live cells shows an as-of stamp with the serving sources.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from .datafeeds.providers.tencent import QT_ENDPOINT, SOURCE as QT_SOURCE
from .datafeeds.providers.tencent import to_tencent_code
from .datafeeds import service as _feeds

CN_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")


def fetch_batch(symbols: list[str], timeout: float = 6.0) -> dict[str, dict[str, Any]]:
    """Fetch live quotes for board symbols through the fallback chain.

    Returns {symbol: {last, prev_close, open, high, low, volume, time,
    name, source}}; symbols no provider knows are absent. A total
    failure returns {} — boards degrade to their no-data cells.
    """
    try:
        return _feeds.quotes(list(symbols), timeout=timeout)
    except Exception:
        return {}


def market_phase(zone: str, now: datetime | None = None) -> str:
    """open / pre / closed for a board zone (weekday-approximate)."""
    if zone == "A-SHARE":
        now = now or datetime.now(CN_TZ)
        if now.weekday() >= 5:
            return "closed"
        t = now.hour * 60 + now.minute
        if dtime(9, 15) <= now.time() <= dtime(11, 30) or dtime(13, 0) <= now.time() <= dtime(15, 0):
            return "open"
        return "closed" if t > 15 * 60 else "pre"
    if zone == "HK":
        now = now or datetime.now(CN_TZ)  # HKT == CST
        if now.weekday() >= 5:
            return "closed"
        if (dtime(9, 30) <= now.time() <= dtime(12, 0)
                or dtime(13, 0) <= now.time() <= dtime(16, 0)):
            return "open"
        return "pre" if now.hour * 60 + now.minute < 9 * 60 + 30 else "closed"
    if zone == "US":
        now = now or datetime.now(US_TZ)
        if now.weekday() >= 5:
            return "closed"
        t = now.hour * 60 + now.minute
        if 9 * 60 + 30 <= t <= 16 * 60:
            return "open"
        return "pre" if t < 9 * 60 + 30 else "closed"
    return "closed"


def poll_interval(zone: str) -> float:
    """Poll cadence by session: fast live in-session, slow otherwise."""
    return 5.0 if market_phase(zone) == "open" else 120.0
