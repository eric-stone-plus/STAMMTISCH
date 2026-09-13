"""Live full test run: feed chains + backtests across every market zone.

Uses only keyless public market-data endpoints. Trading APIs are NOT
touched. Two backtest paths are exercised:

- engine path: QuantEngine -> quantkit.data (the TUI's verified seam)
- free-feed path: tui.datafeeds daily/crypto candles -> quantkit.backtest
  (same run_long_only accounting, explicitly labeled unverified feed)
"""

import sys
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

import pandas as pd

from tui import livefeed
from tui.config import Config
from tui.datafeeds import journal, registry, service
from tui.datafeeds.http import configure_data_proxy

_config = Config()
configure_data_proxy(_config.data_proxy_url)
print(f"data proxy configured: {'yes' if _config.data_proxy_url else 'no (direct only)'}")
from tui.engine import QuantEngine

print("=" * 72)
print(f"STAMMTISCH live full test run — {datetime.now().isoformat(timespec='seconds')}")
print("=" * 72)

# ── 1. Live quotes through the fallback chain ────────────────────────
basket = ["000001.SS", "600519.SS", "0700.HK", "HSI", "AAPL", "NVDA",
          "QQQ", "BTC-USD"]
quotes = livefeed.fetch_batch(basket)
print(f"\n[1] LIVE QUOTES  {len(quotes)}/{len(basket)} symbols served")
for symbol in basket:
    quote = quotes.get(symbol)
    if not quote:
        print(f"  {symbol:<12} (no data)")
        continue
    chg = (quote["last"] / quote["prev_close"] - 1) * 100 if quote.get("prev_close") else 0.0
    print(f"  {symbol:<12} {quote['last']:>12,.2f} {chg:+6.2f}%  src={quote['source']}")

# ── 2. Crypto board chain ────────────────────────────────────────────
try:
    board = service.crypto_board(limit=10)
    top = board["rows"][0]
    print(f"\n[2] CRYPTO BOARD  {len(board['rows'])} rows  "
          f"btc.dominance={board['btc_dominance'] and round(board['btc_dominance'], 1)}%  "
          f"src={top['source']}")
    for row in board["rows"][:5]:
        print(f"  {row['symbol']:<8} {row['last']:>12,.2f}  {row['chg_24h']:+6.2f}%")
except Exception as exc:
    print(f"\n[2] CRYPTO BOARD FAILED: {exc}")

# ── 3. Daily candle chains ───────────────────────────────────────────
print("\n[3] DAILY CANDLES (stooq -> yahoo chain)")
for symbol in ("AAPL", "600519.SS", "0700.HK"):
    try:
        bars = service.daily_candles(symbol)
        print(f"  {symbol:<12} {len(bars):>5} bars  last={bars[-1]['time']}  "
              f"close={bars[-1]['close']:,.2f}")
    except Exception as exc:
        print(f"  {symbol:<12} FAILED: {exc}")

# ── 4. Journal round trip ────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    rows = journal.append(tmp, quotes)
    tape = journal.read_tape(tmp, "AAPL")
    print(f"\n[4] QUOTE JOURNAL  wrote {rows} rows, read back {len(tape)} for AAPL"
          f"  ({tape[0]['source'] if tape else 'n/a'})")

# ── 5. Backtests across the full universe ────────────────────────────
from tui.symbols import _NAMES

universe = [symbol for symbol, _market, _name in _NAMES] + ["BZ=F"]
engine = QuantEngine(data_dir=__import__("tui.config", fromlist=["Config"]).Config().data_dir)

pilot = engine.run_backtest("AAPL", strategy="dual_ma", start="2023-01-01")
use_engine = pilot.get("ok", False)
print(f"\n[5] BACKTESTS  universe={len(universe)} symbols "
      f"(A-share / HK / JP / KR / US + futures)")
print(f"    path: {'QuantEngine -> quantkit.data (verified seam)' if use_engine else 'datafeeds candles -> quantkit.backtest (free feed)'}")
if not use_engine:
    print(f"    engine pilot failed: {str(pilot.get('error'))[:100]} — using free-feed path")

def close_series_from_candles(bars):
    index = pd.to_datetime([bar["time"] for bar in bars])
    return pd.Series([float(bar["close"]) for bar in bars], index=index)

def backtest_one(symbol):
    start = time.time()
    if use_engine:
        result = engine.run_backtest(symbol, strategy="dual_ma", start="2023-01-01")
        if not result.get("ok"):
            return symbol, None, None, time.time() - start, result.get("error")
        summary, stats = result["summary"], result["stats"]
    else:
        try:
            bars = service.daily_candles(symbol)
        except Exception as exc:
            return symbol, None, None, time.time() - start, str(exc)
        from quantkit.backtest import run_long_only, dual_ma_signal
        close = close_series_from_candles(bars)
        out = run_long_only(close, dual_ma_signal(close, fast=20, slow=50),
                            cost_tier="low")
        summary, stats = out, out.stats
    return symbol, summary, stats, time.time() - start, None

results = {}
with ThreadPoolExecutor(max_workers=6) as pool:
    futures = {pool.submit(backtest_one, symbol): symbol for symbol in universe}
    for future in as_completed(futures, timeout=520):
        symbol = futures[future]
        try:
            results[symbol] = future.result()
        except Exception as exc:
            results[symbol] = (symbol, None, None, 0.0, f"worker: {exc}")

print(f"\n    {'SYMBOL':<12} {'RETURN':>9} {'CAGR':>8} {'SHARPE':>7} "
      f"{'MAXDD':>8} {'TRADES':>7}  {'SECS':>5}  PATH")
wins = 0
for symbol in universe:
    symbol, summary, stats, elapsed, error = results.get(
        symbol, (symbol, None, None, 0.0, "missing"))
    if summary is None:
        print(f"  {symbol:<12} {'FAILED':>9}  {str(error)[:70]}")
        continue
    wins += 1
    path = "engine" if use_engine else "feed"
    print(f"  {symbol:<12} {summary.total_return:>+8.2%} {summary.cagr:>+7.2%} "
          f"{summary.sharpe:>7.2f} {summary.max_drawdown:>8.2%} "
          f"{summary.trades:>7d}  {elapsed:>5.1f}  {path}")
print(f"\n    -> {wins}/{len(universe)} backtests completed")

# ── 6. Crypto backtests through the Binance public feed ──────────────
print("\n[6] CRYPTO BACKTESTS (binance klines -> quantkit.backtest)")
for pair in ("BTC-USD", "ETH-USD"):
    try:
        bars = service.crypto_candles(pair, limit=400)
        close = close_series_from_candles(bars)
        from quantkit.backtest import run_long_only, dual_ma_signal
        out = run_long_only(close, dual_ma_signal(close, fast=20, slow=50),
                            cost_tier="low")
        print(f"  {pair:<12} ret={out.total_return:+8.2%} sharpe={out.sharpe:5.2f} "
              f"trades={out.trades:3d}  bars={len(bars)}  src=binance public API")
    except Exception as exc:
        print(f"  {pair:<12} FAILED: {str(exc)[:80]}")

# ── 7. Six-gate evaluation on one verified result ────────────────────
try:
    if use_engine:
        gate_metrics = {
            "total_return": pilot["stats"].get("total_return", 0),
            "cagr": pilot["stats"].get("cagr", 0),
            "sharpe": pilot["stats"].get("sharpe", 0),
            "max_drawdown": pilot["stats"].get("max_drawdown", 0),
            "win_rate": pilot["stats"].get("win_rate", 0),
            "trades": pilot["stats"].get("trades", 0),
        }
        report = engine.evaluate_gates(gate_metrics)
        if report.get("ok"):
            g = report["report"]
            print(f"\n[7] SIX-GATE EVAL (AAPL engine path)  "
                  f"{g.n_passed}/{g.n_total} passed  all_passed={g.all_passed}")
        else:
            print(f"\n[7] SIX-GATE EVAL unavailable: {report.get('error')}")
    else:
        print("\n[7] SIX-GATE EVAL skipped (engine path unavailable)")
except Exception as exc:
    print(f"\n[7] SIX-GATE EVAL FAILED: {exc}")

# ── 8. Provider health summary ───────────────────────────────────────
print("\n[8] PROVIDER HEALTH (this run)")
for entry in sorted(registry.all_stats(), key=lambda item: item.name):
    latency = "—" if entry.avg_latency_ms is None else f"{entry.avg_latency_ms:.0f}ms"
    print(f"  {entry.name:<12} ok={entry.ok:<4} fail={entry.failed:<3} avg={latency:>7}"
          f"  {entry.last_error or ''}")

print("\nTrading APIs (Alpaca / Binance private) were NOT contacted — "
      "public keyless market-data endpoints only.")
