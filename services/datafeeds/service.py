"""Feed service — the composed chains the TUI actually calls.

Each entry point encodes its fallback order, provenance stamps, and
cache policy once, so screens never hand-roll transports. Live quotes
skip the cache deliberately (the boards already own their poll cadence
and must not lag behind a poll); candles and crypto boards cache.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .cache import cached
from .registry import tracked
from .providers import binance, coingecko, stooq, tencent, yahoo
from .providers import tencent as _tencent  # noqa: F401


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


# Ordered chain legs — the single source of truth for fallback order.
# tui/charts.py renders its header labels from these tuples; the chain
# builders below iterate them, so label and behaviour cannot diverge.
DAILY_CHAIN = ("stooq", "yahoo", "alpaca")
CRYPTO_CHAIN = ("binance", "coingecko")
# Served-by for cache entries written before chains carried the stamp.
SERVED_BY_UNKNOWN = "unknown"


def daily_candles(symbol: str, *, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Daily candles — the DAILY_CHAIN legs in order, cached."""
    return daily_candles_with_source(symbol, timeout=timeout)[0]


def daily_candles_with_source(
    symbol: str, *, timeout: float = 10.0
) -> tuple[list[dict[str, Any]], str]:
    """``daily_candles`` plus which chain leg actually served.

    The served-by stamp lives INSIDE the cached payload, so a cache hit
    keeps naming the leg that really fetched the bars. Entries cached
    before the stamp existed report ``"unknown"`` rather than guessing.
    """
    text = symbol.strip().upper()
    payload = cached(
        f"candles:{text}",
        ttl_seconds=6 * 3600,
        producer=lambda: _daily_candles_chain(text, timeout=timeout),
    )
    return _candles_and_source(payload)


def _candles_and_source(payload: Any) -> tuple[list[dict[str, Any]], str]:
    """Split a cached chain payload into (candles, served_by).

    Tolerates the pre-stamp list shape (memory/disk entries written
    before chains carried served_by): honestly ``"unknown"``, never a
    guessed leg. Any other type is corrupt cache state — fail closed
    (rule 2), never silently render an empty chart.
    """
    if isinstance(payload, list):
        return payload, SERVED_BY_UNKNOWN
    if isinstance(payload, dict):
        return (payload.get("candles") or [],
                str(payload.get("served_by") or SERVED_BY_UNKNOWN))
    raise TypeError(
        f"corrupt candles cache payload: expected list or dict, "
        f"got {type(payload).__name__}")


def _daily_candles_chain(symbol: str, *, timeout: float) -> dict[str, Any]:
    errors: list[str] = []
    def _alpaca_candles():
        from ..config import Config as _Config
        from ..brokers.alpaca import AlpacaBroker

        broker = AlpacaBroker(_Config())
        bars = broker.daily_bars(symbol, start="2024-01-01")
        if not bars:
            raise RuntimeError("no alpaca bars")
        return [{"time": str(b["t"])[:10], "open": float(b["o"]),
                 "high": float(b["h"]), "low": float(b["l"]),
                 "close": float(b["c"]), "volume": float(b.get("v", 0))}
                for b in bars]
    legs = {
        "stooq": lambda: stooq.fetch_candles(symbol, timeout=timeout),
        "yahoo": lambda: yahoo.fetch_candles(symbol, timeout=timeout),
        "alpaca": _alpaca_candles,
    }
    # A leg named by DAILY_CHAIN but absent from `legs` is a programming
    # error (constant↔implementation drift), not a provider outage. Raise
    # BEFORE the loop so the BLE001 catch cannot launder the KeyError into
    # a "provider failed" string that would mask the missing leg forever.
    missing = [name for name in DAILY_CHAIN if name not in legs]
    if missing:
        raise RuntimeError(f"DAILY_CHAIN legs without a fetcher: {missing}")
    # Iterate DAILY_CHAIN (the single source of truth for order) so the
    # fallback sequence can never drift from the label charts.py renders.
    for name in DAILY_CHAIN:
        try:
            # Stamp the serving leg inside the producer: cache hits keep
            # naming the source that actually served.
            return {"candles": tracked(name, legs[name]), "served_by": name}
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
            payload = tracked(name, fetch)
        except Exception as exc:  # noqa: BLE001 - try the next source
            errors.append(f"{name}: {exc}")
            continue
        # Stamped inside the producer so a cache hit keeps reporting the
        # true data age instead of the moment of the last cache read.
        payload.setdefault(
            "generated_at",
            datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return payload
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
    """Crypto candles — Binance klines, CoinGecko /ohlc fallback, cached.

    Binance carries full fidelity (intraday intervals, real volume); when
    it geo-blocks the configured egress (HTTP 451) or is otherwise down,
    CoinGecko still serves daily candles with volume reported as 0.0.
    The fallback only serves ``interval="1d"``: CoinGecko granularity is
    endpoint-controlled, and quietly caching endpoint-chosen bars under
    a key that promises another interval would make the cache lie.
    The serving provider lands in the registry stats.
    """
    return crypto_candles_with_source(
        symbol, interval=interval, limit=limit, timeout=timeout)[0]


def crypto_candles_with_source(
    symbol: str, *, interval: str = "1d", limit: int = 180,
    timeout: float = 10.0
) -> tuple[list[dict[str, Any]], str]:
    """``crypto_candles`` plus which chain leg actually served.

    The stamp lives inside the cached payload (same doctrine as
    ``daily_candles_with_source``): cache hits keep telling the truth,
    and pre-stamp entries report ``"unknown"`` instead of guessing.
    """
    pair = symbol.strip().upper().replace("-", "").replace("/", "")
    if pair.endswith("USD") and not pair.endswith("USDT"):
        # Board symbols are Yahoo-style (BTC-USD); the pair feed is USDT.
        pair = pair[:-3] + "USDT"
    if not pair.endswith("USDT"):
        pair = pair + "USDT"
    coin = pair[:-4]
    payload = cached(
        f"cryptocandles:{pair}:{interval}:{limit}",
        ttl_seconds=60,
        # Bounded staleness: unlike the board (which carries generated_at),
        # a candles list has no age channel, so a dead chain must not serve
        # days-old disk snapshots as current.
        disk_ttl_seconds=3600,
        producer=lambda: _crypto_candles_chain(
            pair, coin, interval=interval, limit=limit, timeout=timeout),
    )
    return _candles_and_source(payload)


def _crypto_candles_chain(pair: str, coin: str, *, interval: str, limit: int,
                          timeout: float) -> dict[str, Any]:
    legs = {
        "binance": lambda: binance.fetch_candles(
            pair, interval=interval, limit=limit, timeout=timeout),
        # CoinGecko granularity is endpoint-controlled, so it only serves
        # daily bars; caching endpoint-chosen bars under a key that promises
        # another interval would make the cache lie.
        "coingecko": lambda: coingecko.fetch_candles(
            coin, days=max(1, min(limit, 365)), timeout=timeout)[-limit:],
    }
    # Same drift guard as the daily chain: a leg named by CRYPTO_CHAIN but
    # missing from `legs` is a programming error, not a provider outage.
    missing = [name for name in CRYPTO_CHAIN if name not in legs]
    if missing:
        raise RuntimeError(f"CRYPTO_CHAIN legs without a fetcher: {missing}")
    errors: list[str] = []
    # CRYPTO_CHAIN is the ordered source of truth; coingecko drops out for
    # intraday intervals (it cannot honour them).
    for name in CRYPTO_CHAIN:
        if name == "coingecko" and interval != "1d":
            continue
        try:
            return {"candles": tracked(name, legs[name]), "served_by": name}
        except Exception as exc:  # noqa: BLE001 - try the next source
            errors.append(f"{name}: {exc}")
    raise RuntimeError(f"no crypto candles provider ({'; '.join(errors)})")
