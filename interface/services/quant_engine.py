"""Service lane for the quant workbench (M3) — engine + demo twin.

The old TUI's ``tui/engine.py`` call shapes, copied clean of the old
tree (the boundary rule forbids importing ``tui/`` from shipped code):
five tool calls, each returning a plain ``{"ok": bool, ...}`` dict and
each degrading to ``{"ok": False, "error": str}`` — quantkit imports are
lazy, so this module (like the old QuantEngine) imports fine without
quantkit installed.

:class:`DemoQuantEngine` is the deterministic no-network twin: pure
functions of the arguments via blake2b seeds, so the workbench renders
realistic results in demo/degraded mode. :func:`resolve_engine` picks
one per tool run, honestly, from the snapshot's services strip.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from interface.snapshot import ServiceStatus

__all__ = [
    "BacktestSummary",
    "DemoQuantEngine",
    "GateReport",
    "IndicatorSummary",
    "PortfolioSummary",
    "QuantEngine",
    "quantkit_importable",
    "resolve_engine",
]


def _normalize_symbol(symbol: str) -> str:
    """Compact copy of the old TUI's resolver (no ``tui.`` import).

    Bare 6-digit CN codes gain ``.SS``/``.SZ``/``.BJ`` by first digit,
    bare numeric codes up to 5 digits become ``.HK``; anything already
    suffixed (or alphabetic) passes through upper-cased.
    """
    text = (symbol or "").strip().upper()
    if not text:
        return text
    if any(text.endswith(suffix) for suffix in (
            ".SS", ".SZ", ".BJ", ".HK", ".T", ".KS", ".KQ", "-USD")):
        return text
    if len(text) == 6 and text.isdigit():
        if text[0] in "69":
            return f"{text}.SS"
        if text[0] in "023":
            return f"{text}.SZ"
        if text[0] in "48":
            return f"{text}.BJ"
        return text
    if text.isdigit() and len(text) <= 5:
        core = text.lstrip("0") or "0"
        return (core.zfill(4) if len(core) <= 4 else core) + ".HK"
    return text


def _missing_ohlcv(df: Any) -> bool:
    """True when a fetch returned None or a zero-row frame."""
    return df is None or getattr(df, "empty", True)


def quantkit_importable() -> bool:
    """True when the quantkit package is importable in this interpreter."""
    try:
        import quantkit  # noqa: F401
    except Exception:  # noqa: BLE001 - broken installs degrade like absences
        return False
    return True


@dataclass(frozen=True)
class BacktestSummary:
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    trades: int
    final_equity: float
    cost_bps: float


@dataclass(frozen=True)
class IndicatorSummary:
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    bb_upper: float
    bb_mid: float
    bb_lower: float
    sma_20: float
    sma_50: float
    sma_200: float
    atr_14: float
    vol_20: float


@dataclass(frozen=True)
class PortfolioSummary:
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    trades: int
    n_assets: int
    avg_turnover: float
    avg_gross_exposure: float


@dataclass(frozen=True)
class GateReport:
    gates: tuple[dict[str, Any], ...]
    all_passed: bool
    n_passed: int
    n_total: int


class QuantEngine:
    """quantkit-backed tool calls; every call degrades, none raises."""

    label = "quantkit"

    def __init__(self, data_dir: str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else Path.home() / ".quant_cache"

    @property
    def available(self) -> bool:
        return quantkit_importable()

    def fetch_data(self, symbol: str, market: str = "auto",
                   start: str = "2020-01-01", end: str | None = None,
                   force: bool = False) -> dict[str, Any]:
        symbol = _normalize_symbol(symbol)
        try:
            from quantkit.data import fetch_ohlcv

            df = fetch_ohlcv(symbol, market=market, start=start, end=end,
                             data_dir=str(self.data_dir), force_refresh=force)
            if _missing_ohlcv(df):
                return {"ok": False, "error": f"No data for {symbol}"}
            last = df.iloc[-1]
            return {
                "ok": True, "symbol": symbol, "rows": len(df),
                "columns": list(df.columns),
                "first_date": str(df.index[0]), "last_date": str(df.index[-1]),
                "last_close": float(last.get("close", 0)),
                "last_volume": float(last.get("volume", 0)),
            }
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            return {"ok": False, "error": str(exc)}

    def run_backtest(self, symbol: str, strategy: str = "dual_ma", fast: int = 20,
                     slow: int = 50, start: str = "2020-01-01",
                     cost_tier: str = "low") -> dict[str, Any]:
        symbol = _normalize_symbol(symbol)
        try:
            from quantkit.backtest import (
                dual_ma_signal,
                rsi_mean_reversion_signal,
                run_long_only,
            )
            from quantkit.data import fetch_ohlcv

            df = fetch_ohlcv(symbol, market="auto", start=start,
                             data_dir=str(self.data_dir))
            if _missing_ohlcv(df):
                return {"ok": False, "error": f"No data for {symbol}"}
            close = df["close"]
            if strategy == "dual_ma":
                signal = dual_ma_signal(close, fast=fast, slow=slow)
            elif strategy == "rsi_mr":
                signal = rsi_mean_reversion_signal(close)
            else:
                # Unknown strategy is a user typo, not a dual_ma request.
                return {"ok": False, "error": f"unknown strategy '{strategy}'"}
            result = run_long_only(close, signal, cost_tier=cost_tier)
            summary = BacktestSummary(
                total_return=result.total_return, cagr=result.cagr,
                sharpe=result.sharpe, max_drawdown=result.max_drawdown,
                win_rate=result.win_rate, trades=result.trades,
                final_equity=result.stats.get("final_equity", 1.0),
                cost_bps=result.stats.get("cost_bps_effective", 5.0),
            )
            return {"ok": True, "summary": summary, "stats": result.stats}
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            return {"ok": False, "error": str(exc)}

    def compute_indicators(self, symbol: str,
                           start: str = "2020-01-01") -> dict[str, Any]:
        symbol = _normalize_symbol(symbol)
        try:
            from quantkit.data import fetch_ohlcv
            from quantkit.indicators import add_core_indicators

            df = fetch_ohlcv(symbol, market="auto", start=start,
                             data_dir=str(self.data_dir))
            if _missing_ohlcv(df):
                return {"ok": False, "error": f"No data for {symbol}"}
            feat = add_core_indicators(df)
            last = feat.iloc[-1]
            summary = IndicatorSummary(
                rsi=float(last.get("rsi_14", 0)), macd=float(last.get("macd", 0)),
                macd_signal=float(last.get("macd_signal", 0)),
                macd_hist=float(last.get("macd_hist", 0)),
                bb_upper=float(last.get("bb_upper", 0)),
                bb_mid=float(last.get("bb_mid", 0)),
                bb_lower=float(last.get("bb_lower", 0)),
                sma_20=float(last.get("sma_20", 0)),
                sma_50=float(last.get("sma_50", 0)),
                sma_200=float(last.get("sma_200", 0)),
                atr_14=float(last.get("atr_14", 0)),
                vol_20=float(last.get("vol_20", 0)),
            )
            return {"ok": True, "summary": summary,
                    "last_price": float(last.get("close", 0))}
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            return {"ok": False, "error": str(exc)}

    def run_portfolio(self, symbols: list[str], strategy: str = "momentum",
                      start: str = "2020-01-01", rebalance: str = "M",
                      lookback: int = 60) -> dict[str, Any]:
        symbols = [_normalize_symbol(s) for s in symbols]
        try:
            from quantkit.portfolio import (
                dual_ma_panel,
                fetch_price_panel,
                momentum_panel,
                signal_to_weights,
            )
            from quantkit.portfolio import (
                run_portfolio as run_port,
            )

            prices = fetch_price_panel(symbols, start=start,
                                       data_dir=str(self.data_dir))
            if _missing_ohlcv(prices):
                return {"ok": False, "error": "No price data"}
            if strategy == "momentum":
                signals = momentum_panel(prices, lookback=lookback)
            elif strategy == "dual_ma":
                signals = dual_ma_panel(prices)
            else:
                return {"ok": False, "error": f"unknown strategy '{strategy}'"}
            weights = signal_to_weights(signals, long_only=True, max_weight=0.3)
            result = run_port(prices, weights, rebalance=rebalance)
            summary = PortfolioSummary(
                total_return=result.total_return, cagr=result.cagr,
                sharpe=result.sharpe, max_drawdown=result.max_drawdown,
                win_rate=result.win_rate, trades=result.trades,
                n_assets=result.stats.get("n_assets", len(symbols)),
                avg_turnover=result.stats.get("avg_turnover", 0),
                avg_gross_exposure=result.stats.get("avg_gross_exposure", 0),
            )
            return {"ok": True, "summary": summary, "stats": result.stats}
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            return {"ok": False, "error": str(exc)}

    def evaluate_gates(self, metrics: dict[str, Any]) -> dict[str, Any]:
        try:
            from quantkit.gates import evaluate_gates as eval_gates

            result = eval_gates(metrics)
            gates_list: list[dict[str, Any]] = []
            all_passed = True
            n_passed = 0
            # quantkit returns GateResult dataclasses (gate/passed/
            # failures/missing/metrics); tolerate dicts from other
            # revisions so the UI never assumes one shape.
            for gate in result.get("gates", []):
                if isinstance(gate, dict):
                    gate_id = gate.get("gate", gate.get("gate_id", "?"))
                    passed = bool(gate.get("passed", False))
                    detail = gate.get("reason", gate.get("detail", ""))
                    value = gate.get("value")
                    threshold = gate.get("threshold")
                else:
                    gate_id = getattr(gate, "gate", "?")
                    passed = bool(getattr(gate, "passed", False))
                    detail = "; ".join(getattr(gate, "failures", []) or [])
                    value = getattr(gate, "metrics", {})
                    threshold = None
                if passed:
                    n_passed += 1
                else:
                    all_passed = False
                gates_list.append({
                    "gate_id": gate_id, "passed": passed,
                    "detail": detail, "value": value, "threshold": threshold,
                })
            report = GateReport(gates=tuple(gates_list), all_passed=all_passed,
                                n_passed=n_passed, n_total=len(gates_list))
            return {"ok": True, "report": report}
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            return {"ok": False, "error": str(exc)}


class DemoQuantEngine:
    """Deterministic no-network twin of the quantkit tool calls.

    Every value is a pure function of the arguments (blake2b seeds), so
    demo/degraded results are stable across runs and testable without
    quantkit, market data or a network. ``delay_s`` simulates a slow
    engine so tests can prove the UI thread never blocks.
    """

    label = "demo"

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def _seed(self, *parts: object) -> float:
        digest = hashlib.blake2b(
            "|".join(str(p) for p in parts).encode(), digest_size=8
        ).digest()
        return int.from_bytes(digest, "big") / 2**64

    def _nap(self) -> None:
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)

    def _metrics(self, key: str) -> dict[str, Any]:
        seed = self._seed("metrics", key)
        return {
            "total_return": round(seed * 0.62 - 0.18, 6),
            "cagr": round(seed * 0.18 - 0.05, 6),
            "sharpe": round(0.3 + seed * 2.1, 4),
            "max_drawdown": round(0.06 + seed * 0.33, 6),
            "win_rate": round(0.36 + seed * 0.31, 6),
            "trades": 8 + int(seed * 160),
            "cost_bps_effective": round(3.0 + seed * 7.0, 1),
        }

    def fetch_data(self, symbol: str, **_ignored: Any) -> dict[str, Any]:
        self._nap()
        symbol = _normalize_symbol(symbol)
        seed = self._seed("fetch", symbol)
        rows = 700 + int(seed * 1_600)
        first = date(2020, 1, 2)
        last = first + timedelta(days=rows - 1)
        return {
            "ok": True, "symbol": symbol, "rows": rows,
            "columns": ["open", "high", "low", "close", "volume"],
            "first_date": str(first), "last_date": str(last),
            "last_close": round(12.0 + seed * 480.0, 2),
            "last_volume": float(1_000_000 + int(seed * 90_000_000)),
        }

    def run_backtest(self, symbol: str, strategy: str = "dual_ma",
                     fast: int = 20, slow: int = 50,
                     **_ignored: Any) -> dict[str, Any]:
        self._nap()
        symbol = _normalize_symbol(symbol)
        if strategy not in ("dual_ma", "rsi_mr"):
            return {"ok": False, "error": f"unknown strategy '{strategy}'"}
        metrics = self._metrics(f"bt:{symbol}:{strategy}:{fast}:{slow}")
        summary = BacktestSummary(
            total_return=metrics["total_return"], cagr=metrics["cagr"],
            sharpe=metrics["sharpe"], max_drawdown=metrics["max_drawdown"],
            win_rate=metrics["win_rate"], trades=metrics["trades"],
            final_equity=round(1.0 + metrics["total_return"], 6),
            cost_bps=metrics["cost_bps_effective"],
        )
        return {"ok": True, "summary": summary, "stats": metrics}

    def compute_indicators(self, symbol: str,
                           **_ignored: Any) -> dict[str, Any]:
        self._nap()
        symbol = _normalize_symbol(symbol)
        seed = self._seed("ind", symbol)
        price = round(12.0 + seed * 480.0, 2)
        drift = (seed - 0.5) * price * 0.08
        summary = IndicatorSummary(
            rsi=round(15.0 + seed * 70.0, 2),
            macd=round(drift * 0.6, 4),
            macd_signal=round(drift * 0.4, 4),
            macd_hist=round(drift * 0.2, 4),
            bb_upper=round(price * 1.06, 2), bb_mid=price,
            bb_lower=round(price * 0.94, 2),
            sma_20=round(price - drift * 0.3, 2),
            sma_50=round(price - drift * 0.6, 2),
            sma_200=round(price - drift, 2),
            atr_14=round(price * (0.01 + seed * 0.03), 2),
            vol_20=round(0.08 + seed * 0.3, 4),
        )
        return {"ok": True, "summary": summary, "last_price": price}

    def run_portfolio(self, symbols: list[str], strategy: str = "momentum",
                      **_ignored: Any) -> dict[str, Any]:
        self._nap()
        symbols = [_normalize_symbol(s) for s in symbols]
        if strategy not in ("momentum", "dual_ma"):
            return {"ok": False, "error": f"unknown strategy '{strategy}'"}
        if not symbols:
            return {"ok": False, "error": "no symbols given"}
        metrics = self._metrics(f"pf:{','.join(symbols)}:{strategy}")
        summary = PortfolioSummary(
            total_return=metrics["total_return"], cagr=metrics["cagr"],
            sharpe=metrics["sharpe"], max_drawdown=metrics["max_drawdown"],
            win_rate=metrics["win_rate"], trades=metrics["trades"],
            n_assets=len(symbols),
            avg_turnover=round(0.02 + self._seed("to", *symbols) * 0.2, 4),
            avg_gross_exposure=round(0.6 + self._seed("ge", *symbols) * 0.4, 4),
        )
        return {"ok": True, "summary": summary, "stats": metrics}

    def evaluate_gates(self, metrics: dict[str, Any]) -> dict[str, Any]:
        self._nap()
        checks: tuple[tuple[str, bool, Any, Any], ...] = (
            ("return.positive", metrics.get("total_return", 0) > 0,
             metrics.get("total_return"), "> 0"),
            ("drawdown.cap", metrics.get("max_drawdown", 1) < 0.35,
             metrics.get("max_drawdown"), "< 0.35"),
            ("sharpe.floor", metrics.get("sharpe", 0) > 0.5,
             metrics.get("sharpe"), "> 0.5"),
            ("winrate.floor", metrics.get("win_rate", 0) > 0.40,
             metrics.get("win_rate"), "> 0.40"),
            ("trades.min", metrics.get("trades", 0) >= 20,
             metrics.get("trades"), ">= 20"),
            ("cost.cap", metrics.get("cost_bps_effective", 5.0) <= 12.0,
             metrics.get("cost_bps_effective"), "<= 12.0"),
        )
        rows = tuple({
            "gate_id": gate_id, "passed": passed,
            "detail": "" if passed else f"failed {threshold}",
            "value": value, "threshold": threshold,
        } for gate_id, passed, value, threshold in checks)
        n_passed = sum(1 for row in rows if row["passed"])
        report = GateReport(gates=rows, all_passed=n_passed == len(rows),
                            n_passed=n_passed, n_total=len(rows))
        return {"ok": True, "report": report}


def resolve_engine(
    services: Sequence[ServiceStatus], real: QuantEngine | None = None
) -> tuple[DemoQuantEngine | QuantEngine, str]:
    """Pick the engine for one tool run, honestly, from the services strip.

    The real engine wins only when the strip reports quantkit up AND the
    process can import it; otherwise the demo twin runs and the label
    says why (``demo (quantkit DOWN)`` / ``demo (quantkit not
    importable)``) — the degraded state is rendered, never hidden.
    """
    engine = real if real is not None else QuantEngine()
    quantkit_up = any(
        s.name == "quantkit" and s.available for s in services
    )
    if quantkit_up and engine.available:
        return engine, engine.label
    why = "quantkit DOWN" if not quantkit_up else "quantkit not importable"
    return DemoQuantEngine(), f"demo ({why})"
