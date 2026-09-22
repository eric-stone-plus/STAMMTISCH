"""Service lane for the quant workbench — reconciled with the shared engine.

M7 true merge: the real engine now lives in the shared ``services``
package (``services/engine.py``, git-mv'd from ``tui/engine.py``), so
this module no longer re-implements its call shapes — the five tool
calls, the degrade-to-``{"ok": False, "error": str}`` discipline, the
summary dataclasses, and the reactive egress rotation all have exactly
one implementation. What stays here:

- :class:`QuantEngine` — the shared engine plus the ``label``
  vocabulary the resolver reports (the shared class carries no UI
  words);
- :class:`DemoQuantEngine` — the deterministic no-network twin: pure
  functions of the arguments via blake2b seeds, so the workbench
  renders realistic results in demo/degraded mode;
- :func:`resolve_engine` — picks one per tool run, honestly, from the
  snapshot's services strip.

Symbol normalization delegates to the shared offline resolver
(``services.symbols.normalize_symbol``) — the M4 compact copy is
retired with the rest of the duplication.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from interface.snapshot import ServiceStatus
from services.engine import (
    BacktestSummary,
    FactorSummary,
    GateReport,
    IndicatorSummary,
    PortfolioSummary,
)
from services.engine import QuantEngine as _SharedQuantEngine
from services.symbols import normalize_symbol as _normalize_symbol

__all__ = [
    "BacktestSummary",
    "DemoQuantEngine",
    "FactorSummary",
    "GateReport",
    "IndicatorSummary",
    "PortfolioSummary",
    "QuantEngine",
    "quantkit_importable",
    "resolve_engine",
]


class QuantEngine(_SharedQuantEngine):
    """The shared quantkit engine, labelled for the resolver."""

    label = "quantkit"


def quantkit_importable() -> bool:
    """True when the quantkit package is importable in this interpreter."""
    try:
        import quantkit  # noqa: F401
    except Exception:  # noqa: BLE001 - broken installs degrade like absences
        return False
    return True


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
