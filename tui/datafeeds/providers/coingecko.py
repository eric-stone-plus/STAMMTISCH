"""CoinGecko markets endpoint — the crypto board primary source.

Keyless ``/coins/markets`` with 7-day sparkline series; Binance public
tickers are the fallback chain partner (see providers.binance).
"""

from __future__ import annotations

from typing import Any

from ..errors import FeedError
from ..http import get_json

MARKETS_ENDPOINT = "https://api.coingecko.com/api/v3/coins/markets"
SOURCE = "CoinGecko /coins/markets"

# 7-day hourly sparkline downsampled to roughly this many points keeps
# the sidebar sparkline readable at 40 cells.
_SPARK_POINTS = 40


def parse_markets(payload: Any) -> dict[str, Any]:
    """Parse the markets payload -> {rows, btc_dominance}.

    ``btc_dominance`` is computed over the fetched page only (top-N by
    market cap), which is exactly what the board renders.
    """
    if not isinstance(payload, list) or not payload:
        raise FeedError("coingecko", "markets payload is empty")
    rows: list[dict[str, Any]] = []
    total_cap = 0.0
    btc_cap = 0.0
    for coin in payload:
        if not isinstance(coin, dict) or coin.get("current_price") is None:
            continue
        spark = (coin.get("sparkline_in_7d") or {}).get("price") or []
        if spark:
            step = max(1, len(spark) // _SPARK_POINTS)
            spark = spark[::step][-_SPARK_POINTS:]
        market_cap = float(coin.get("market_cap") or 0)
        total_cap += market_cap
        if str(coin.get("symbol") or "").lower() == "btc":
            btc_cap = market_cap
        rows.append({
            "symbol": str(coin.get("symbol") or "").upper(),
            "name": coin.get("name") or "",
            "last": float(coin["current_price"]),
            "chg_24h": float(coin.get("price_change_percentage_24h") or 0),
            "market_cap": market_cap,
            "volume": float(coin.get("total_volume") or 0),
            "spark": [float(point) for point in spark],
            "source": SOURCE,
        })
    if not rows:
        raise FeedError("coingecko", "markets payload has no priced rows")
    return {
        "rows": rows,
        "btc_dominance": (btc_cap / total_cap * 100.0) if total_cap else None,
    }


def fetch_board(*, limit: int = 30, timeout: float = 10.0) -> dict[str, Any]:
    """Top-N coins by market cap with 24h change and 7-day sparkline."""
    from urllib.parse import urlencode

    url = MARKETS_ENDPOINT + "?" + urlencode({
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": str(max(1, min(limit, 250))),
        "page": "1",
        "sparkline": "true",
        "price_change_percentage": "24h",
    })
    try:
        payload = get_json(url, timeout=timeout, provider="coingecko")
    except Exception as exc:
        raise FeedError("coingecko", f"markets request failed: {exc}") from exc
    return parse_markets(payload)
