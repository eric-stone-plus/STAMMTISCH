"""Batch screeners — hundreds of crypto pairs and US stocks.

Crypto: Binance USDT pairs above a volume floor, 1h bars, RSI(2) mean
reversion, net of round-trip fees at three tiers. Stocks: Alpaca
tradable NASDAQ/NYSE names, daily bars, gap-reversal (intraday-T
proxy: buy a -1.5xATR gap-down open, sell the close) and a 3-day
RSI(2) hold. Results land in the state root as JSON so a screen can
re-read them without re-running the network sweep.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import portfolio  # noqa: F401  (state-root sibling, documents layout)
from .config import Config
from .datafeeds import http as dfhttp
from .datafeeds.http import configure_data_proxy, configure_proxy_fallback

STOCK_COST_RT = 0.0005  # slippage allowance per round trip


def _prepare(config: Config) -> None:
    configure_data_proxy(config.data_proxy_url)
    configure_proxy_fallback(config.egress_proxy_url)


def _rsi(close: pd.Series, period: int = 2) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def crypto_screen(config: Config, *, min_volume: float = 2_000_000,
                  limit: int = 320, workers: int = 10,
                  timeout: float = 480.0) -> dict[str, Any]:
    """RSI(2) mean reversion on 1h bars for every liquid USDT pair."""
    _prepare(config)
    tickers = dfhttp.get_json("https://api.binance.com/api/v3/ticker/24hr",
                              provider="binance")
    ranked = [t for t in tickers if t["symbol"].endswith("USDT")
              and float(t.get("quoteVolume", 0)) >= min_volume
              and not t["symbol"].startswith(("USDC", "FDUSD", "TUSD", "EUR"))]
    ranked.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    universe = [t["symbol"] for t in ranked[:limit]]

    def one(symbol: str) -> dict[str, Any] | None:
        try:
            raw = dfhttp.get_json(
                "https://api.binance.com/api/v3/klines"
                f"?symbol={symbol}&interval=1h&limit=200", provider="binance")
            closes = np.array([float(k[4]) for k in raw], dtype=float)
            if len(closes) < 100:
                return None
            r = _rsi(pd.Series(closes), 2).values
            trades, i = [], 1
            while i < len(closes) - 1:
                if r[i] < 10 and r[i - 1] >= 10:
                    j = i + 1
                    while j < len(closes) - 1 and not r[j] > 60 and j - i < 24:
                        j += 1
                    trades.append(closes[j] / closes[i] - 1)
                    i = j + 1
                else:
                    i += 1
            if len(trades) < 5:
                return None
            arr = np.array(trades)
            return {"symbol": symbol, "trades": len(arr),
                    "mean": float(arr.mean()), "median": float(np.median(arr))}
        except Exception:
            return None

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, s): s for s in universe}
        for future in as_completed(futures, timeout=timeout):
            row = future.result()
            if row:
                rows.append(row)
    tiers = {f"{fee:.2%}": {
        "median_net": float(np.median([r["mean"] - fee for r in rows])),
        "positive_share": float(np.mean([r["mean"] - fee > 0 for r in rows])),
    } for fee in (0.0005, 0.001, 0.002)}
    return {"universe": len(universe), "evaluated": len(rows),
            "fee_tiers": tiers, "rows": sorted(rows, key=lambda r: -r["mean"])}


def stock_screen(config: Config, *, limit: int = 320, workers: int = 6,
                 throttle: float = 0.05, timeout: float = 520.0) -> dict[str, Any]:
    """Gap-reversal and RSI(2) screens over sampled Alpaca names."""
    _prepare(config)
    broker = AlpacaBroker(config)
    assets = dfhttp.get_json(
        "https://paper-api.alpaca.markets/v2/assets?status=active",
        headers=broker._headers(), provider="alpaca")
    tradable = [a for a in assets if a.get("tradable")
                and a.get("exchange") in ("NASDAQ", "NYSE")
                and a.get("class") == "us_equity"]
    tradable.sort(key=lambda a: a["symbol"])
    sample = tradable[::max(1, len(tradable) // limit)][:limit]

    def one(symbol: str) -> dict[str, Any] | None:
        try:
            bars = broker.daily_bars(symbol, start="2024-01-01")
            if len(bars) < 120:
                return None
            df = pd.DataFrame(bars)
            o, c, h, l = (df[k].astype(float).values for k in ("o", "c", "h", "l"))
            prev = np.roll(c, 1)
            prev[0] = np.nan
            tr = np.maximum(h - l, np.maximum(abs(h - prev), abs(l - prev)))
            atr = pd.Series(tr).rolling(14).mean().values
            sig = (o / prev - 1 <= -1.5 * (atr / prev)) & ~np.isnan(atr)
            ent = np.where(sig)[0]
            if len(ent) < 5:
                return None
            gap = (c[ent] / o[ent] - 1) - STOCK_COST_RT
            r2 = _rsi(pd.Series(c), 2).values
            trades, i = [], 1
            while i < len(c) - 1:
                if r2[i] < 10 and r2[i - 1] >= 10:
                    j = min(i + 3, len(c) - 1)
                    trades.append(c[j] / c[i] - 1)
                    i = j + 1
                else:
                    i += 1
            if len(trades) < 5:
                return None
            r2a = np.array(trades)
            return {"symbol": symbol, "gap_trades": len(gap),
                    "gap_mean": float(gap.mean()),
                    "rsi_trades": len(r2a), "rsi_mean": float(r2a.mean()) - STOCK_COST_RT}
        except Exception:
            return None

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, a["symbol"]): a for a in sample}
        for future in as_completed(futures, timeout=timeout):
            time.sleep(throttle)
            row = future.result()
            if row:
                rows.append(row)
    return {"universe": len(sample), "evaluated": len(rows), "rows": rows}


def persist(state_root: str | Path, kind: str, payload: dict[str, Any]) -> Path:
    path = Path(state_root) / "intel" / "screeners" / f"{kind}-{datetime.now():%Y%m%d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                               **payload}, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="batch screeners (crypto pairs / US stocks)")
    parser.add_argument("--crypto", action="store_true")
    parser.add_argument("--stocks", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the payload to stdout")
    args = parser.parse_args(argv)
    config = Config()
    payloads = {}
    if args.crypto:
        payloads["crypto"] = crypto_screen(config)
    if args.stocks or not args.crypto:
        payloads["stocks"] = stock_screen(config)
    for kind, payload in payloads.items():
        root = config.state_root or str(Path.home() / ".local/share/stammtisch")
        path = persist(root, kind, payload)
        print(f"{kind}: {payload['evaluated']}/{payload['universe']} evaluated -> {path}")
        if args.json:
            print(json.dumps(payload, default=str))


if __name__ == "__main__":
    main()
