"""Full backtest run — every cached symbol x two strategies, offline.

Data: quantkit's verified parquet cache (what the TUI boards read) for
equities/futures, plus the keyless Binance feed through the configured
egress proxy for crypto. quantkit.backtest provides the accounting for
both paths. No trading API is contacted.
"""

import re
import sys
from pathlib import Path

import pandas as pd

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

from quantkit.backtest import dual_ma_signal, rsi_mean_reversion_signal, run_long_only

from services.config import Config
from services.datafeeds import registry, service
from services.datafeeds.http import configure_data_proxy

config = Config()
print("=" * 78)
print("STAMMTISCH full backtest run (offline cache + egress feeds)")
print("=" * 78)

# ── 1. Load the freshest cached frame per symbol ─────────────────────
cache_dir = Path(config.data_dir) / "cache"
files = sorted(cache_dir.glob("*.parquet")) if cache_dir.is_dir() else []
by_symbol: dict[str, tuple[pd.Timestamp, Path]] = {}
for path in files:
    match = re.search(r"auto_(?P<symbol>.+?)_1d", path.name)
    if not match:
        continue
    symbol = match.group("symbol").upper()
    try:
        frame = pd.read_parquet(path)
    except Exception:
        continue
    if frame is None or getattr(frame, "empty", True):
        continue
    last = frame.index[-1]
    if symbol not in by_symbol or last > by_symbol[symbol][0]:
        by_symbol[symbol] = (last, path)

print(f"\n[1] CACHE  {len(files)} files -> {len(by_symbol)} unique symbols "
      f"(freshest frame per symbol)")

def close_series(symbol: str) -> pd.Series | None:
    _last, path = by_symbol[symbol]
    frame = pd.read_parquet(path)
    frame.columns = [str(column).lower() for column in frame.columns]
    if "close" not in frame.columns:
        return None
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    close.index = pd.to_datetime(frame.index[close.index.argsort()])
    close = close.sort_index()
    return close if len(close) >= 60 else None

def zone_of(symbol: str) -> str:
    text = symbol.upper()
    if text.endswith((".SS", ".SZ", ".BJ")):
        return "A-SHARE"
    if text.endswith(".HK"):
        return "HK"
    if "." in text:
        return "OTHER"
    return "US"

# ── 2. Run both strategies on every cached symbol ────────────────────
rows = []
for symbol in sorted(by_symbol):
    close = close_series(symbol)
    if close is None:
        continue
    row = {"symbol": symbol, "zone": zone_of(symbol), "bars": len(close),
           "last": str(close.index[-1])[:10]}
    for name, signal_fn in (("dual_ma", dual_ma_signal),
                            ("rsi_mr", rsi_mean_reversion_signal)):
        try:
            out = run_long_only(close, signal_fn(close), cost_tier="low")
            row[name] = out
        except Exception as exc:
            row[name] = exc
    rows.append(row)

def fmt(result):
    if isinstance(result, Exception):
        return f"{'FAIL':>9} {'—':>7} {'—':>8}"
    return (f"{result.total_return:>+8.2%} {result.sharpe:>7.2f} "
            f"{result.max_drawdown:>8.2%}")

print(f"\n[2] BACKTESTS  {len(rows)} symbols x dual_ma + rsi_mr  "
      f"(quantkit.run_long_only, cost_tier=low, verified local cache)")
print(f"    {'SYMBOL':<14} {'ZONE':<8} {'BARS':>5} {'LAST':>11}  "
      f"{'-- dual_ma: RETURN SHARPE MAXDD --':<32} {'-- rsi_mr --':<28}")
for row in rows:
    print(f"    {row['symbol']:<14} {row['zone']:<8} {row['bars']:>5} {row['last']:>11}  "
          f"{fmt(row['dual_ma']):<32} {fmt(row['rsi_mr']):<28}")

ok_dual = [row for row in rows if not isinstance(row["dual_ma"], Exception)]
ok_rsi = [row for row in rows if not isinstance(row["rsi_mr"], Exception)]
print(f"\n    -> dual_ma {len(ok_dual)}/{len(rows)} ok, "
      f"rsi_mr {len(ok_rsi)}/{len(rows)} ok")
if ok_dual:
    best = max(ok_dual, key=lambda row: row["dual_ma"].sharpe)
    print(f"    best dual_ma sharpe: {best['symbol']} "
          f"({best['dual_ma'].sharpe:.2f}, {best['dual_ma'].total_return:+.2%})")

# ── 3. Crypto through the egress proxy (Binance public feed) ─────────
egress = config.egress_proxy_url or None
configure_data_proxy(egress)
print(f"\n[3] CRYPTO BACKTESTS via Binance public feed "
      f"(egress proxy: {'configured' if egress else 'none'})")
for pair in ("BTC-USD", "ETH-USD"):
    try:
        bars = service.crypto_candles(pair, limit=400)
        close = pd.Series([float(bar["close"]) for bar in bars],
                          index=pd.to_datetime([bar["time"] for bar in bars]))
        out = run_long_only(close, dual_ma_signal(close), cost_tier="low")
        print(f"  {pair:<10} ret={out.total_return:+8.2%} sharpe={out.sharpe:5.2f} "
              f"maxdd={out.max_drawdown:>7.2%} trades={out.trades:3d}  bars={len(bars)}")
    except Exception as exc:
        print(f"  {pair:<10} FAILED: {str(exc)[:90]}")

# ── 4. Free-feed chain through egress (stooq -> yahoo) ───────────────
print("\n[4] FREE-FEED CANDLE CHAIN via egress (stooq -> yahoo)")
for symbol in ("AAPL", "0700.HK"):
    try:
        bars = service.daily_candles(symbol)
        print(f"  {symbol:<10} {len(bars)} bars  last={bars[-1]['time']}")
    except Exception as exc:
        print(f"  {symbol:<10} FAILED: {str(exc)[:90]}")

# ── 5. Six-gate evaluation on one cached result ──────────────────────
try:
    from services.engine import QuantEngine

    engine = QuantEngine(data_dir=config.data_dir)
    sample = next((row for row in rows if row["zone"] == "A-SHARE"
                   and not isinstance(row["dual_ma"], Exception)), None)
    if sample is not None:
        out = sample["dual_ma"]
        metrics = {"total_return": out.total_return, "cagr": out.cagr,
                   "sharpe": out.sharpe, "max_drawdown": out.max_drawdown,
                   "win_rate": out.win_rate, "trades": out.trades}
        report = engine.evaluate_gates(metrics)
        if report.get("ok"):
            gates = report["report"]
            print(f"\n[5] SIX-GATE EVAL ({sample['symbol']} dual_ma)  "
                  f"{gates.n_passed}/{gates.n_total} passed  "
                  f"all_passed={gates.all_passed}")
        else:
            print(f"\n[5] SIX-GATE EVAL unavailable: {report.get('error')}")
except Exception as exc:
    print(f"\n[5] SIX-GATE EVAL FAILED: {exc}")

# ── 6. Provider health ───────────────────────────────────────────────
print("\n[6] PROVIDER HEALTH (this run)")
for entry in sorted(registry.all_stats(), key=lambda item: item.name):
    latency = "—" if entry.avg_latency_ms is None else f"{entry.avg_latency_ms:.0f}ms"
    print(f"  {entry.name:<12} ok={entry.ok:<4} fail={entry.failed:<3} avg={latency:>7}"
          f"  {entry.last_error or ''}")

print("\nNote: trading APIs (Alpaca / Binance private) were NOT contacted.")
print("Note: config data_proxy_url (127.0.0.1:46617) is DOWN — global feeds "
      "in the TUI will fail until it is restarted; egress_proxy_url was used "
      "for this run's crypto/feed checks.")
