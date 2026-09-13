"""Binance public API — crypto candles and the 24h-ticker board fallback.

Read-only, keyless market endpoints only (``/api/v3/klines`` and
``/api/v3/ticker/24hr``); the pinned-proxy egress discipline of the
private adapters does not apply to this keyless feed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from ..errors import FeedError
from ..http import get_json

BASE = "https://api.binance.com/api/v3"
SOURCE = "Binance public API"


def parse_klines(payload: Any) -> list[dict[str, Any]]:
    """Parse the klines array (newest last) into candle dicts."""
    if not isinstance(payload, list) or not payload:
        raise FeedError("binance", "klines payload is empty")
    candles: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            opened_ms = int(row[0])
        except (TypeError, ValueError):
            continue
        opened = datetime.fromtimestamp(opened_ms / 1000, tz=timezone.utc)
        time_format = "%Y-%m-%d" if opened.hour == 0 and opened.minute == 0 \
            else "%Y-%m-%d %H:%M"
        candles.append({
            "time": opened.strftime(time_format),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })
    if not candles:
        raise FeedError("binance", "klines payload has no rows")
    return candles


def fetch_candles(symbol: str, *, interval: str = "1d", limit: int = 200,
                  timeout: float = 10.0) -> list[dict[str, Any]]:
    """Candles for one trading pair (e.g. BTCUSDT)."""
    url = (BASE + "/klines?" + urlencode({
        "symbol": symbol.strip().upper(),
        "interval": interval,
        "limit": str(max(1, min(limit, 1000))),
    }))
    try:
        payload = get_json(url, timeout=timeout, provider="binance")
    except Exception as exc:
        raise FeedError("binance", f"klines for {symbol}: {exc}") from exc
    return parse_klines(payload)


def parse_ticker24h(payload: Any) -> dict[str, dict[str, Any]]:
    """Parse 24hr tickers -> {symbol: normalized quote}."""
    if not isinstance(payload, list):
        raise FeedError("binance", "ticker payload is not a list")
    out: dict[str, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        try:
            last = float(row["lastPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        prev = row.get("prevClosePrice") or row.get("openPrice")
        out[str(row["symbol"])] = {
            "symbol": str(row["symbol"]),
            "name": str(row.get("symbol")),
            "last": last,
            "prev_close": float(prev) if prev else None,
            "open": float(row["openPrice"]) if row.get("openPrice") else None,
            "high": float(row["highPrice"]) if row.get("highPrice") else None,
            "low": float(row["lowPrice"]) if row.get("lowPrice") else None,
            "volume": float(row["volume"]) if row.get("volume") else None,
            "time": "",
            "source": SOURCE,
        }
    if not out:
        raise FeedError("binance", "ticker payload has no priced rows")
    return out


def fetch_quotes(symbols: list[str], *, timeout: float = 10.0) -> dict[str, dict[str, Any]]:
    """24h tickers for a list of trading pairs, one request."""
    pairs = [str(symbol).strip().upper().replace("-", "") for symbol in symbols]
    pairs = [pair for pair in pairs if pair]
    if not pairs:
        return {}
    from json import dumps

    url = BASE + "/ticker/24hr?symbols=" + urlencode({"": dumps(pairs)})[1:]
    try:
        payload = get_json(url, timeout=timeout, provider="binance")
    except Exception as exc:
        raise FeedError("binance", f"ticker request failed: {exc}") from exc
    return parse_ticker24h(payload)
