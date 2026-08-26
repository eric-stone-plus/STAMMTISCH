"""Batch live quotes + market-session clock for the realtime boards.

One HTTP request per poll covers the whole visible zone (Tencent's
qt.gtimg.cn batch endpoint — free, unauthenticated, GBK). Domestic
symbols go direct; the endpoint is CN-side so no egress is involved.

Sources are explicit: every quote carries the endpoint it came from,
and every board that renders live cells shows an as-of stamp.
"""

from __future__ import annotations

import re
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import requests

QT_ENDPOINT = "http://qt.gtimg.cn/q="
QT_SOURCE = "腾讯批量行情 qt.gtimg.cn"
CN_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")

_FIELD_RE = re.compile(r'v_(?P<code>[A-Za-z]{2}\w+)="(?P<payload>[^"]*)"')


def to_tencent_code(symbol: str) -> str | None:
    """Map a board symbol to the Tencent quote code, or None if unmappable."""
    text = symbol.strip().upper()
    if text.endswith(".SZ"):
        return "sz" + text[: -len(".SZ")]
    if text.endswith((".SS", ".BJ")):
        return "sh" + text.split(".")[0]
    if text.endswith(".HK"):
        return "hk" + text.split(".")[0].zfill(5)
    if "." not in text:
        return "us" + text
    return None


def fetch_batch(symbols: list[str], timeout: float = 6.0) -> dict[str, dict[str, Any]]:
    """Fetch live quotes for board symbols in one request.

    Returns {symbol: {last, prev_close, open, high, low, volume, time,
    name, source}}; symbols the endpoint does not know are absent.
    """
    if not symbols:
        return {}
    codes: list[tuple[str, str]] = []
    for symbol in symbols:
        code = to_tencent_code(symbol)
        if code:
            codes.append((symbol, code))
    if not codes:
        return {}
    url = QT_ENDPOINT + ",".join(code for _, code in codes)
    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="replace")
    except requests.RequestException:
        return {}
    by_tencent_code = {code: symbol for symbol, code in codes}
    out: dict[str, dict[str, Any]] = {}
    for match in _FIELD_RE.finditer(text):
        code = match.group("code")
        symbol = by_tencent_code.get(code)
        if symbol is None:
            continue
        fields = match.group("payload").split("~")
        if len(fields) < 46 or not fields[3]:
            continue
        try:
            out[symbol] = {
                "name": fields[1],
                "last": float(fields[3]),
                "prev_close": float(fields[4]),
                "open": float(fields[5]),
                "volume": float(fields[6]),
                "high": float(fields[33]) if len(fields) > 33 and fields[33] else None,
                "low": float(fields[34]) if len(fields) > 34 and fields[34] else None,
                # field 30: A股 YYYYMMDDHHMMSS; 美股 "2026-08-25 16:00:01"
                "time": fields[30] if len(fields) > 30 else "",
                "source": QT_SOURCE,
            }
        except (ValueError, IndexError):
            continue
    return out


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
