"""LiveFeed quotes — delegated to the shared services chain since M7.

The public contract is the old ``tui/livefeed.py`` ``fetch_batch``
shape (``{symbol: {last, prev_close, open, high, low, volume, time,
name, source}}``; unknown symbols absent; a TOTAL failure returns
``{}`` so callers degrade to no-data quotes). M4 carried that contract
as a copy+adapt extraction because the boundary rule forbade importing
the old tree; the M7 true merge moved the real module and its
datafeeds chain (Tencent batch → Yahoo per-symbol fallback, tracked,
provenance-stamped) into the shared ``services`` package, so the copy
is retired — this lane delegates and the parser can only drift in one
place.

What stays interface-native: :func:`quote_age_s`, the STALE-side
normalization of the provider time formats (the shared tree has no
age helper), and the call-time import seam — collectors and tests
stub :func:`fetch_batch` (or ``services.livefeed``) and stay offline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

__all__ = [
    "CN_TZ",
    "fetch_batch",
    "quote_age_s",
]

#: Tencent wall-clock timezone (the feed's convention, kept so ages
#: match what the old boards rendered).
CN_TZ = ZoneInfo("Asia/Shanghai")


def fetch_batch(symbols: list[str],
                timeout: float = 6.0) -> dict[str, dict[str, Any]]:
    """Live quotes through the shared chain; a total failure is ``{}``.

    Delegates to ``services.livefeed.fetch_batch`` (resolved at call
    time, never import time, so importing this module can never drag a
    network transport onto the path and tests can stub the seam).
    Since M6 every provider call under the shared chain runs through
    the datafeeds counters registry — the FEEDS screen reads the same
    registry, so "which provider is serving me" stays answerable.
    """
    from services import livefeed

    return livefeed.fetch_batch(list(symbols), timeout=timeout)


def quote_age_s(quote: dict[str, Any], now: float) -> float | None:
    """Age of one quote row in seconds, or None when undatable.

    Normalizes the provider time formats: Yahoo epoch-seconds strings
    (UTC), Tencent ``YYYYMMDDHHMMSS``, ``YYYY-MM-DD HH:MM:SS`` and
    ``YYYY/MM/DD HH:MM:SS`` (HK/US rows on the CN-side feed use slashes
    or dashes — all Asia/Shanghai wall clock, the feed's convention,
    kept so ages match what the old boards rendered).
    """
    raw = str(quote.get("time") or "").strip()
    if not raw:
        return None
    stamp: datetime | None = None
    if raw.isdigit() and len(raw) == 14:
        stamp = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    elif len(raw) == 19 and raw[10] == " " and raw[4] in "-/":
        fmt = "%Y-%m-%d %H:%M:%S" if raw[4] == "-" else "%Y/%m/%d %H:%M:%S"
        stamp = datetime.strptime(raw, fmt).replace(tzinfo=CN_TZ)
    elif raw.replace(".", "", 1).isdigit():
        # Yahoo epoch seconds (int or float string; UTC).
        try:
            return max(0.0, now - float(raw))
        except ValueError:
            return None
    if stamp is None:
        return None
    return max(0.0, now - stamp.timestamp())
