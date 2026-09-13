"""Stooq daily-history CSV — keyless candle fallback.

``https://stooq.com/q/d/l/?s=<code>&i=d`` serves one CSV per symbol.
Mapping: bare US tickers get ``.us``, ``.HK`` passes through, A-share
suffixes map onto ``.cn``.
"""

from __future__ import annotations

import csv
from typing import Any

from ..errors import FeedError
from ..http import get_text

ENDPOINT = "https://stooq.com/q/d/l/"
SOURCE = "Stooq CSV"


def stooq_code(symbol: str) -> str:
    text = symbol.strip().upper()
    if text.endswith(".HK"):
        return text.removesuffix(".HK").zfill(5).lower() + ".hk"
    if text.endswith((".SS", ".SZ", ".BJ")):
        return text.split(".")[0].lower() + ".cn"
    if "." in text:
        return text.lower()
    return text.lower() + ".us"


def parse_csv(text: str) -> list[dict[str, Any]]:
    """Parse the Date,Open,High,Low,Close,Volume CSV (newest last)."""
    reader = csv.DictReader(text.splitlines())
    required = {"Date", "Open", "High", "Low", "Close"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise FeedError("stooq", f"unexpected CSV header: {(reader.fieldnames or [])[:8]}")
    candles: list[dict[str, Any]] = []
    for row in reader:
        try:
            candles.append({
                "time": row["Date"],
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row.get("Volume") or 0),
            })
        except (ValueError, TypeError, KeyError):
            continue
    if not candles:
        raise FeedError("stooq", "CSV has no candle rows")
    return candles


def fetch_candles(symbol: str, *, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Daily candles for one symbol."""
    from urllib.parse import urlencode

    url = ENDPOINT + "?" + urlencode({"s": stooq_code(symbol), "i": "d"})
    try:
        text = get_text(url, timeout=timeout, provider="stooq")
    except Exception as exc:
        raise FeedError("stooq", f"candles for {symbol}: {exc}") from exc
    return parse_csv(text)
