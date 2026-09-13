"""RSI(2) parameter grid on 1h bars — entry x exit x fee sensitivity.

Research tool: fetches the top-30 liquid USDT pairs' 1h bars once and
evaluates an entry/exit threshold grid on the same bars, reporting
median net per trade and the positive share at two fee tiers. The
median (not the mean) is the honest number — long-tail outliers inflate
averages on mean-reversion strategies.
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tui.config import Config  # noqa: E402
from tui.datafeeds import http as dfhttp  # noqa: E402
from tui.datafeeds.http import (  # noqa: E402
    configure_data_proxy, configure_proxy_fallback)


def rsi_series(closes, period=2):
    s = pd.Series(closes)
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return (100 - 100 / (1 + g / l.replace(0, pd.NA))).fillna(100.0).values.astype(float)


def backtest(closes, r, entry, exit_, max_hold=24):
    trades, i = [], 1
    n = len(closes)
    while i < n - 1:
        if r[i] < entry and r[i - 1] >= entry:
            j = i + 1
            while j < n - 1 and not r[j] > exit_ and j - i < max_hold:
                j += 1
            trades.append(closes[j] / closes[i] - 1)
            i = j + 1
        else:
            i += 1
    return trades


def main() -> None:
    config = Config()
    configure_data_proxy(config.data_proxy_url)
    configure_proxy_fallback(config.egress_proxy_url)

    tickers = dfhttp.get_json("https://api.binance.com/api/v3/ticker/24hr",
                              provider="binance")
    usdt = [t for t in tickers if t["symbol"].endswith("USDT")
            and float(t.get("quoteVolume", 0)) >= 10_000_000
            and not t["symbol"].startswith(("USDC", "FDUSD", "TUSD", "EUR"))]
    usdt.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    universe = [t["symbol"] for t in usdt[:30]]

    def fetch(symbol):
        for attempt in range(3):
            try:
                raw = dfhttp.get_json(
                    "https://api.binance.com/api/v3/klines"
                    f"?symbol={symbol}&interval=1h&limit=200",
                    provider="binance")
                return symbol, [float(k[4]) for k in raw]
            except Exception:
                import time as _t
                _t.sleep(1.0 * (attempt + 1))
        return symbol, None

    bars = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fetch, s) for s in universe]
        for future in as_completed(futures):
            sym, closes = future.result()
            if closes and len(closes) >= 150:
                bars[sym] = closes
    print(f"bars: {len(bars)} pairs", flush=True)

    grid = {}
    for entry in (5, 8, 10, 12, 15):
        for exit_ in (50, 60, 70):
            all_trades = []
            for closes in bars.values():
                all_trades += backtest(closes, rsi_series(closes), entry, exit_)
            if len(all_trades) < 100:
                continue
            arr = np.array(all_trades)
            for fee in (0.0005, 0.001):
                grid[(entry, exit_, fee)] = (
                    float(np.median(arr - fee)),
                    float((arr - fee > 0).mean()), len(arr))
    print("GRID — median net/trade @0.10% fee | positive share | trades:")
    for entry in (5, 8, 10, 12, 15):
        row = []
        for exit_ in (50, 60, 70):
            med, share, n = grid[(entry, exit_, 0.001)]
            row.append(f"exit{exit_}: {med:+.3%}/{share:.0%}/n{n}")
        print(f"  entry{entry:>3}: " + "  ".join(row))
    best = max(((k, v) for k, v in grid.items() if k[2] == 0.001),
               key=lambda kv: kv[1][0])
    print(f"BEST @0.10%: entry={best[0][0]} exit={best[0][1]} -> "
          f"{best[1][0]:+.3%}/trade, share {best[1][1]:.0%}, trades {best[1][2]}")
    cousin = grid[(best[0][0], best[0][1], 0.0005)]
    print(f"same cell @0.05%: {cousin[0]:+.3%}/trade, share {cousin[1]:.0%}")


if __name__ == "__main__":
    main()
