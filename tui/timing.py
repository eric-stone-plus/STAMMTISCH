"""Market timing preset — dual-MA(50/200) regime for index ETFs.

The low-risk, easy-to-operate preset: hold SPY/QQQ while the close is
above its 200-day mean, stand aside (cash) when below. One rule, ~5
trades a year, drawdown roughly halved versus buy-and-hold on the
2020–2026 window (verified against Alpaca IEX daily bars).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .brokers.alpaca import AlpacaBroker
from .datafeeds.http import configure_data_proxy, configure_proxy_fallback

DEFAULT_SYMBOLS = ("SPY", "QQQ")


def _closes(bars: list[dict[str, Any]]) -> pd.Series:
    close = pd.Series([float(b["c"]) for b in bars],
                      index=pd.to_datetime([b["t"] for b in bars]))
    return close.sort_index()


def status(config: Any, symbols: tuple[str, ...] = DEFAULT_SYMBOLS) -> dict[str, Any]:
    """Current regime per symbol: HOLD above MA200, CASH below."""
    configure_data_proxy(config.data_proxy_url)
    configure_proxy_fallback(config.egress_proxy_url)
    broker = AlpacaBroker(config)
    rows = []
    for symbol in symbols:
        close = _closes(broker.daily_bars(symbol))
        ma50 = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()
        last = close.iloc[-1]
        m200 = ma200.iloc[-1]
        rows.append({
            "symbol": symbol,
            "last": last,
            "ma50": ma50.iloc[-1],
            "ma200": m200,
            "state": "HOLD" if last >= m200 else "CASH",
            "dist_200": last / m200 - 1,
            "dist_50": last / ma50.iloc[-1] - 1,
            "asof": str(close.index[-1].date()),
        })
    return {"rows": rows}
