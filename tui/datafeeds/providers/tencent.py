"""Tencent qt.gtimg.cn batch quotes — free, unauthenticated, GBK.

One request covers the whole visible board (CN A-shares, HK, and US
tickers all resolve through the `v_` prefixed batch endpoint). The
CN-side endpoint is reached directly; no egress class is involved.
"""

from __future__ import annotations

import re
from typing import Any

from ..http import get_text
from ..errors import FeedError

QT_ENDPOINT = "http://qt.gtimg.cn/q="
SOURCE = "Tencent qt.gtimg.cn"

_FIELD_RE = re.compile(r'v_(?P<code>[A-Za-z]{2}\w+)="(?P<payload>[^"]*)"')


def to_tencent_code(symbol: str) -> str | None:
    """Map a board symbol to the Tencent quote code, or None if unmappable."""
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
    """Parse one batch response body -> {tencent_code: normalized quote}."""
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
                "high": float(fields[33]) if len(fields) > 33 and fields[33] else None,
                "low": float(fields[34]) if len(fields) > 34 and fields[34] else None,
                # field 30: A-share feeds use YYYYMMDDHHMMSS; US feeds use
                # "YYYY-MM-DD HH:MM:SS"
                "time": fields[30] if len(fields) > 30 else "",
                "source": SOURCE,
            }
        except (ValueError, IndexError):
            continue
    return out


def fetch_quotes(symbols: list[str], *, timeout: float = 6.0) -> dict[str, dict[str, Any]]:
    """Fetch live quotes for board symbols in one request.

    Returns {symbol: normalized quote}; symbols the endpoint does not
    know are absent so the fallback chain can fill them.
    """
    codes: list[tuple[str, str]] = []
    for symbol in symbols:
        code = to_tencent_code(symbol)
        if code:
            codes.append((symbol.strip().upper(), code))
    if not codes:
        return {}
    url = QT_ENDPOINT + ",".join(code for _, code in codes)
    try:
        text = get_text(url, timeout=timeout, encoding="gbk", provider="tencent")
    except Exception as exc:
        raise FeedError("tencent", f"batch request failed: {exc}") from exc
    if not text.strip():
        raise FeedError("tencent", "empty batch response")
    by_code = {code: symbol for symbol, code in codes}
    return {
        by_code[code]: quote
        for code, quote in parse_batch(text).items()
        if code in by_code
    }
