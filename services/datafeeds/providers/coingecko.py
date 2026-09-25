"""CoinGecko markets endpoint — the crypto board primary source.

Keyless ``/coins/markets`` with 7-day sparkline series; Binance public
tickers are the fallback chain partner (see providers.binance).
"""

from __future__ import annotations

from typing import Any

from ..errors import FeedError
from ..http import get_json

MARKETS_ENDPOINT = "https://api.coingecko.com/api/v3/coins/markets"
SEARCH_ENDPOINT = "https://api.coingecko.com/api/v3/search"
OHLC_ENDPOINT = "https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
SOURCE = "CoinGecko /coins/markets"
CANDLE_SOURCE = "CoinGecko /coins/{id}/ohlc"

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


def parse_ohlc(payload: Any) -> list[dict[str, Any]]:
    """Parse the /ohlc array (newest last) into candle dicts.

    The endpoint reports no volume; the field stays present (0.0) so the
    candle shape matches the binance chain partner.
    """
    if not isinstance(payload, list) or not payload:
        raise FeedError("coingecko", "ohlc payload is empty")
    from datetime import datetime, timezone

    candles: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            stamp_ms = int(row[0])
            values = [float(v) for v in row[1:5]]
        except (TypeError, ValueError):
            continue
        opened = datetime.fromtimestamp(stamp_ms / 1000, tz=timezone.utc)
        time_format = "%Y-%m-%d" if opened.hour == 0 and opened.minute == 0 \
            else "%Y-%m-%d %H:%M"
        candles.append({
            "time": opened.strftime(time_format),
            "open": values[0],
            "high": values[1],
            "low": values[2],
            "close": values[3],
            "volume": 0.0,
        })
    if not candles:
        raise FeedError("coingecko", "ohlc payload has no rows")
    return candles


def resolve_coin_id(symbol: str, *, timeout: float = 10.0) -> str:
    """Map a board ticker (BTC) to its CoinGecko id (bitcoin).

    Exact symbol match wins; among matches the best market-cap rank is
    taken so look-alike tickers on the /search page cannot hijack the
    candle chain.
    """
    from urllib.parse import urlencode

    wanted = symbol.strip().upper()
    if not wanted:
        raise FeedError("coingecko", "empty symbol for id lookup")
    url = SEARCH_ENDPOINT + "?" + urlencode({"query": wanted})
    try:
        payload = get_json(url, timeout=timeout, provider="coingecko")
    except Exception as exc:
        raise FeedError("coingecko", f"search for {wanted}: {exc}") from exc
    coins = payload.get("coins") if isinstance(payload, dict) else None
    if not isinstance(coins, list):
        raise FeedError("coingecko", f"search payload for {wanted} has no coins")
    matches = [
        coin for coin in coins
        if isinstance(coin, dict) and coin.get("id")
        and str(coin.get("symbol") or "").upper() == wanted
    ]
    if not matches:
        raise FeedError("coingecko", f"no coin matches symbol {wanted}")
    matches.sort(key=lambda coin: coin.get("market_cap_rank") or float("inf"))
    return str(matches[0]["id"])


def fetch_candles(symbol: str, *, days: int = 180,
                  timeout: float = 10.0) -> list[dict[str, Any]]:
    """OHLC candles for one coin symbol via the keyless /ohlc endpoint.

    Granularity is endpoint-controlled: hourly for short windows, 4h up
    to 30 days, daily beyond — callers get whatever /ohlc serves for the
    requested ``days`` (capped to the API's 365-day window).
    """
    from urllib.parse import urlencode

    coin_id = resolve_coin_id(symbol, timeout=timeout)
    url = OHLC_ENDPOINT.format(coin_id=coin_id) + "?" + urlencode({
        "vs_currency": "usd",
        "days": str(max(1, min(int(days), 365))),
    })
    try:
        payload = get_json(url, timeout=timeout, provider="coingecko")
    except Exception as exc:
        raise FeedError("coingecko", f"ohlc for {coin_id}: {exc}") from exc
    return parse_ohlc(payload)
