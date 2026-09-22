"""LiveFeed quotes — the old tui/livefeed.py fetch_batch contract, copied.

Copy + adapt (no ``tui.`` import; the old tree stays live until M5 and
this module must import cleanly everywhere):

- Public shape copied from tui/livefeed.py:27-37: ``fetch_batch(
  symbols, timeout) -> {symbol: {last, prev_close, open, high, low,
  volume, time, name, source}}``; symbols no provider knows are absent;
  a TOTAL failure returns ``{}`` — callers degrade to no-data quotes.
- Provider chain copied from the old datafeeds stack it delegated to
  (tui/datafeeds/service.py:19-43): one Tencent batch request covers
  the whole symbol set (free, unauthenticated, GBK, CN-side), then a
  per-symbol Yahoo chart fallback fills what Tencent missed. Every
  returned row carries the ``source`` of the provider that served it —
  merged boards stay provenance-explicit.
- Deliberately NOT copied: the datafeeds proxy-pinning/tracking/cache
  layers (tui/datafeeds/http.py, registry.py, cache.py). Those move
  wholesale with the old tree at M5 per the blueprint; this provider is
  the collector-side glance feed, direct-egress only, and says so in
  its source stamps.
- Time fields keep the provider formats verbatim (Tencent field 30:
  ``YYYYMMDDHHMMSS`` CN-side or ``YYYY-MM-DD HH:MM:SS``; Yahoo: epoch
  seconds as a string) — :func:`quote_age_s` normalizes them.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from interface.services.feeds_health import tracked

__all__ = [
    "CN_TZ",
    "QT_ENDPOINT",
    "YAHOO_CHART_ENDPOINT",
    "fetch_batch",
    "parse_batch",
    "quote_age_s",
    "to_tencent_code",
]

#: Tencent wall-clock timezone (copy of tui/livefeed.py:23).
CN_TZ = ZoneInfo("Asia/Shanghai")

#: Copied from tui/datafeeds/providers/tencent.py:16-17.
QT_ENDPOINT = "http://qt.gtimg.cn/q="
TENCENT_SOURCE = "Tencent qt.gtimg.cn"

#: Copied from tui/datafeeds/providers/yahoo.py:15-16.
YAHOO_CHART_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/"
YAHOO_SOURCE = "Yahoo Finance (chart API)"

_FIELD_RE = re.compile(r'v_(?P<code>[A-Za-z]{2}\w+)="(?P<payload>[^"]*)"')


def to_tencent_code(symbol: str) -> str | None:
    """Board symbol -> Tencent quote code; None when unmappable.

    Copy of tui/datafeeds/providers/tencent.py:22-35.
    """
    text = symbol.strip().upper()
    if text == "HSI":
        return "hkHSI"
    if text.endswith(".SZ"):
        return "sz" + text[: -len(".SZ")]
    if text.endswith((".SS", ".BJ")):
        return "sh" + text.split(".")[0]
    if text.endswith(".HK"):
        return "hk" + text.split(".")[0].zfill(5)
    if "." not in text:
        return "us" + text
    return None


def parse_batch(text: str) -> dict[str, dict[str, Any]]:
    """Parse one batch response body -> {tencent_code: normalized quote}.

    Copy of tui/datafeeds/providers/tencent.py:38-62 (field map and
    the len<46 / empty-price guards verbatim).
    """
    out: dict[str, dict[str, Any]] = {}
    for match in _FIELD_RE.finditer(text):
        code = match.group("code")
        fields = match.group("payload").split("~")
        if len(fields) < 46 or not fields[3]:
            continue
        try:
            out[code] = {
                "name": fields[1],
                "last": float(fields[3]),
                "prev_close": float(fields[4]),
                "open": float(fields[5]),
                "volume": float(fields[6]),
                "high": (float(fields[33])
                         if len(fields) > 33 and fields[33] else None),
                "low": (float(fields[34])
                        if len(fields) > 34 and fields[34] else None),
                # field 30: A-share feeds use YYYYMMDDHHMMSS; US feeds
                # use "YYYY-MM-DD HH:MM:SS" (both CN-side wall clock).
                "time": fields[30] if len(fields) > 30 else "",
                "source": TENCENT_SOURCE,
            }
        except (ValueError, IndexError):
            continue
    return out


def _get(url: str, *, timeout: float, encoding: str | None = None
         ) -> str:
    """Minimal GET (the old chain's proxy layer is deliberately absent)."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read()
    return body.decode(encoding or "utf-8", errors="replace")


def _tencent_batch(symbols: list[str], timeout: float
                   ) -> dict[str, dict[str, Any]]:
    """One batch request for every mappable symbol (old tencent.py:65-90)."""
    codes: list[tuple[str, str]] = []
    for symbol in symbols:
        code = to_tencent_code(symbol)
        if code:
            codes.append((symbol.strip().upper(), code))
    if not codes:
        return {}
    url = QT_ENDPOINT + ",".join(code for _, code in codes)
    text = _get(url, timeout=timeout, encoding="gbk")
    if not text.strip():
        return {}
    by_code = {code: symbol for symbol, code in codes}
    return {
        by_code[code]: quote
        for code, quote in parse_batch(text).items()
        if code in by_code
    }


def _to_yahoo_symbol(symbol: str) -> str:
    """Copy of tui/datafeeds/providers/yahoo.py:19-32 (BRK.B -> BRK-B)."""
    text = symbol.strip().upper()
    if "." not in text:
        return text
    prefix, suffix = text.rsplit(".", 1)
    if prefix.isalpha() and suffix.isalpha():
        return prefix + "-" + suffix
    return text


def _yahoo_quote(symbol: str, timeout: float) -> dict[str, Any]:
    """One chart-meta quote (old yahoo.py:35-58 parse, trimmed to meta)."""
    url = (YAHOO_CHART_ENDPOINT
           + urllib.parse.quote(_to_yahoo_symbol(symbol))
           + "?interval=1d&range=5d")
    payload = json.loads(_get(url, timeout=timeout))
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    if (not isinstance(results, list) or not results
            or not isinstance(results[0], dict)):
        raise ValueError(f"yahoo chart result for {symbol} is empty")
    meta = results[0].get("meta") or {}
    last = meta.get("regularMarketPrice")
    if last is None:
        raise ValueError(f"yahoo chart meta for {symbol} has no price")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    return {
        "symbol": symbol,
        "name": meta.get("shortName") or meta.get("longName") or symbol,
        "last": float(last),
        "prev_close": float(prev) if prev else None,
        "open": None,
        "high": (float(meta["regularMarketDayHigh"])
                 if meta.get("regularMarketDayHigh") else None),
        "low": (float(meta["regularMarketDayLow"])
                if meta.get("regularMarketDayLow") else None),
        "volume": (float(meta["regularMarketVolume"])
                   if meta.get("regularMarketVolume") else None),
        "time": str(meta.get("regularMarketTime") or ""),
        "source": YAHOO_SOURCE,
    }


def fetch_batch(symbols: list[str],
                timeout: float = 6.0) -> dict[str, dict[str, Any]]:
    """Live quotes through the fallback chain; a total failure is ``{}``.

    Public contract copied from tui/livefeed.py:27-37 (which delegated
    to tui/datafeeds/service.quotes): Tencent batch first, Yahoo
    per-symbol fallback for the misses; unknown symbols stay absent so
    callers render honest no-data rows, never fabricated ones. Since M6
    every provider call runs through the feeds-health counters registry
    (the old tracked() contract) so the FEEDS screen can answer "which
    provider is serving me" — counting never changes the result.
    """
    wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not wanted:
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        out = tracked("tencent", lambda: _tencent_batch(wanted, timeout))
    except Exception:  # noqa: BLE001 - chain continues to the fallback
        out = {}
    missing = [symbol for symbol in wanted if symbol not in out]
    for symbol in missing:
        try:
            out[symbol] = tracked(
                "yahoo", lambda sym=symbol: _yahoo_quote(sym, timeout))
        except Exception:  # noqa: BLE001, S112 - absent symbols degrade
            continue
    return out


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
