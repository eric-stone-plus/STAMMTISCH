"""Feed service — the composed chains the TUI actually calls.

Each entry point encodes its fallback order, provenance stamps, and
cache policy once, so screens never hand-roll transports. Live quotes
skip the cache deliberately (the boards already own their poll cadence
and must not lag behind a poll); candles and crypto boards cache.
"""

from __future__ import annotations

from typing import Any

from .cache import cached
from .registry import tracked
from .providers import binance, coingecko, stooq, tencent, yahoo


def quotes(symbols: list[str], *, timeout: float = 6.0) -> dict[str, dict[str, Any]]:
    """Live quotes for board symbols — Tencent batch, Yahoo per-symbol fallback.

    Symbols both providers miss are absent from the result; every
    returned row carries the ``source`` stamp of the provider that
    served it, so a merged board stays provenance-explicit.
    """
    wanted = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    if not wanted:
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        out = tracked("tencent", lambda: tencent.fetch_quotes(wanted, timeout=timeout))
    except Exception:  # noqa: BLE001 - chain continues to the fallback
        out = {}
    missing = [symbol for symbol in wanted if symbol not in out]
    if missing:
        try:
            fallback = tracked(
                "yahoo",
                lambda: yahoo.fetch_quotes(missing, timeout=timeout))
            out.update(fallback)
        except Exception:  # noqa: BLE001 - absent symbols render as no-data
            pass
    return out


def daily_candles(symbol: str, *, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Daily candles — Stooq primary, Yahoo chart fallback, cached."""
    text = symbol.strip().upper()
    return cached(
        f"candles:{text}",
        ttl_seconds=6 * 3600,
        producer=lambda: _daily_candles_chain(text, timeout=timeout),
    )


def _daily_candles_chain(symbol: str, *, timeout: float) -> list[dict[str, Any]]:
    errors: list[str] = []
    for name, fetch in (
        ("stooq", lambda: stooq.fetch_candles(symbol, timeout=timeout)),
        ("yahoo", lambda: yahoo.fetch_candles(symbol, timeout=timeout)),
    ):
        try:
            return tracked(name, fetch)
        except Exception as exc:  # noqa: BLE001 - try the next source
            errors.append(f"{name}: {exc}")
    raise RuntimeError(f"no candle provider for {symbol} ({'; '.join(errors)})")


def crypto_board(*, limit: int = 30, timeout: float = 10.0) -> dict[str, Any]:
    """Crypto board — CoinGecko markets, Binance 24h tickers fallback."""
    return cached(
        f"cryptoboard:{limit}",
        ttl_seconds=60,
        producer=lambda: _crypto_board_chain(limit, timeout=timeout),
    )


def _crypto_board_chain(limit: int, *, timeout: float) -> dict[str, Any]:
    errors: list[str] = []
    for name, fetch in (
        ("coingecko", lambda: coingecko.fetch_board(limit=limit, timeout=timeout)),
        ("binance", lambda: _binance_board(limit, timeout=timeout)),
    ):
        try:
            return tracked(name, fetch)
        except Exception as exc:  # noqa: BLE001 - try the next source
            errors.append(f"{name}: {exc}")
    raise RuntimeError(f"no crypto board provider ({'; '.join(errors)})")


def _binance_board(limit: int, *, timeout: float) -> dict[str, Any]:
    """Shape the Binance ticker fallback like the CoinGecko board."""
    pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
             "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT"][:limit]
    tickers = binance.fetch_quotes(pairs, timeout=timeout)
    rows = []
    for pair in pairs:
        quote = tickers.get(pair)
        if not quote:
            continue
        chg = ((quote["last"] / quote["prev_close"] - 1) * 100.0
               if quote.get("prev_close") else 0.0)
        rows.append({
            "symbol": pair.removesuffix("USDT"),
            "name": pair,
            "last": quote["last"],
            "chg_24h": chg,
            "market_cap": 0.0,
            "volume": quote.get("volume") or 0.0,
            "spark": [],
            "source": quote["source"],
        })
    if not rows:
        raise RuntimeError("binance board produced no rows")
    return {"rows": rows, "btc_dominance": None}


def crypto_candles(symbol: str, *, interval: str = "1d", limit: int = 180,
                   timeout: float = 10.0) -> list[dict[str, Any]]:
    """Crypto candles — Binance klines, cached."""
    pair = symbol.strip().upper().replace("-", "").replace("/", "")
    if pair.endswith("USD") and not pair.endswith("USDT"):
        # Board symbols are Yahoo-style (BTC-USD); the pair feed is USDT.
        pair = pair[:-3] + "USDT"
    if not pair.endswith("USDT"):
        pair = pair + "USDT"
    return cached(
        f"cryptocandles:{pair}:{interval}:{limit}",
        ttl_seconds=60,
        producer=lambda: binance.fetch_candles(
            pair, interval=interval, limit=limit, timeout=timeout),
    )
