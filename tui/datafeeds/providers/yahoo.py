"""Yahoo Finance v8 chart API — quotes and daily candles, keyless.

The chart endpoint needs no crumb/cookie session, which makes it the
resilient fallback for quotes (when Tencent misses or fails) and the
second daily-candle source behind Stooq. One request per symbol.
"""

from __future__ import annotations

from typing import Any

from ..errors import FeedError
from ..http import get_json

CHART_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/"
SOURCE = "Yahoo Finance (chart API)"


def to_yahoo_symbol(symbol: str) -> str:
    """Board symbol -> Yahoo ticker.

    US class-share dots become dashes (BRK.B -> BRK-B); numeric-prefixed
    codes keep the dot because Yahoo itself quotes 600519.SS / 0700.HK /
    7203.T in dot form.
    """
    text = symbol.strip().upper()
    if "." not in text:
        return text
    prefix, suffix = text.rsplit(".", 1)
    if prefix.isalpha() and suffix.isalpha():
        return prefix + "-" + suffix
    return text


def parse_chart_quote(symbol: str, payload: Any) -> dict[str, Any]:
    """Extract the normalized quote from one v8 chart payload."""
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        error = chart.get("error") if isinstance(chart, dict) else None
        raise FeedError("yahoo", f"chart result for {symbol}: {error or 'empty'}")
    meta = results[0].get("meta") or {}
    last = meta.get("regularMarketPrice")
    if last is None:
        raise FeedError("yahoo", f"chart meta for {symbol} has no price")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    return {
        "symbol": symbol,
        "name": meta.get("shortName") or meta.get("longName") or symbol,
        "last": float(last),
        "prev_close": float(prev) if prev else None,
        "open": None,
        "high": float(meta["regularMarketDayHigh"]) if meta.get("regularMarketDayHigh") else None,
        "low": float(meta["regularMarketDayLow"]) if meta.get("regularMarketDayLow") else None,
        "volume": float(meta["regularMarketVolume"]) if meta.get("regularMarketVolume") else None,
        "time": str(meta.get("regularMarketTime") or ""),
        "source": SOURCE,
    }


def parse_chart_candles(payload: Any) -> list[dict[str, Any]]:
    """Extract daily candles (newest last) from one v8 chart payload."""
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise FeedError("yahoo", "chart result is empty")
    result = results[0]
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    fields = {key: quote.get(key) or [] for key in
              ("open", "high", "low", "close", "volume")}
    candles: list[dict[str, Any]] = []
    for index, stamp in enumerate(stamps):
        close = (fields["close"][index:index + 1] or [None])[0]
        if close is None:
            continue
        candles.append({
            "time": str(stamp),
            "open": _float(fields["open"], index),
            "high": _float(fields["high"], index),
            "low": _float(fields["low"], index),
            "close": float(close),
            "volume": _float(fields["volume"], index) or 0.0,
        })
    if not candles:
        raise FeedError("yahoo", "chart payload has no candles")
    return candles


def _float(values: list[Any], index: int) -> float | None:
    if index >= len(values):
        return None
    value = values[index]
    return float(value) if value is not None else None


def fetch_quotes(symbols: list[str], *, timeout: float = 6.0) -> dict[str, dict[str, Any]]:
    """Per-symbol quotes; symbols that fail stay absent (fallback contract)."""
    out: dict[str, dict[str, Any]] = {}
    last_error: Exception | None = None
    for symbol in symbols:
        try:
            payload = get_json(
                CHART_ENDPOINT + to_yahoo_symbol(symbol) + "?interval=1d&range=5d",
                timeout=timeout, provider="yahoo")
            out[symbol.strip().upper()] = parse_chart_quote(
                symbol.strip().upper(), payload)
        except Exception as exc:  # noqa: BLE001 - per-symbol degradation
            last_error = exc
    if not out and last_error is not None:
        raise FeedError("yahoo", f"all {len(symbols)} quote fetches failed: {last_error}")
    return out


def fetch_candles(symbol: str, *, interval: str = "1d", range_: str = "1y",
                  timeout: float = 10.0) -> list[dict[str, Any]]:
    """Daily candles for one symbol from the chart API."""
    url = (CHART_ENDPOINT + to_yahoo_symbol(symbol)
           + f"?interval={interval}&range={range_}&events=div%2Csplit")
    try:
        payload = get_json(url, timeout=timeout, provider="yahoo")
    except Exception as exc:
        raise FeedError("yahoo", f"candles for {symbol}: {exc}") from exc
    return parse_chart_candles(payload)
