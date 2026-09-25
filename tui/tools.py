"""Hot-pluggable chat tools (AI function calling).

A tool is a name, a JSON Schema description the model reads to decide
whether to call, and a local handler. Register or remove entries in
`default_tools()` to plug or unplug capabilities — the model and the
persona never change. Handlers are offline-first: they read the local
cache and never expose host paths in their results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from services.ai_driver import build_market_context
from services.engine import QuantEngine


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]

    def wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _sibling_galahad_root():
    """The optional GALAHAD checkout cloned next to this repository."""
    from pathlib import Path

    return Path(__file__).resolve().parents[2] / "GALAHAD" / "quantkit"


def _load_sibling_ml_pipeline(galahad_root=None):
    """File-based load of GALAHAD's ``ml_pipeline.py``.

    Returns None when the sibling is absent; raises RuntimeError when it
    is present but broken — a broken sibling must never masquerade as an
    absent one (AGENTS.md rule 2, FIXES.md B-3/E-1 degrade-loudly).

    The operator's quantkit tree (the venv editable install or the engine
    bridge's ``quantkit_path``) may be an evolved fork that intentionally
    does not ship ``ml_pipeline`` — its doctrine home is the public
    GALAHAD checkout. Loading the single file by path (instead of
    inserting the sibling onto ``sys.path``) keeps the installed tree
    authoritative for every other quantkit module: a path insert would
    silently shadow the operator's data providers with GALAHAD's. The
    module's own package import (``quantkit.indicators``) resolves
    against whichever quantkit is installed.
    """
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(galahad_root) if galahad_root is not None \
        else _sibling_galahad_root()
    module_path = root / "quantkit" / "ml_pipeline.py"
    if not module_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "_stammtisch_galahad_ml_pipeline", module_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: dataclasses with postponed (string)
    # annotations resolve their defining module through sys.modules
    # during class construction (Python 3.14 dataclasses._is_type), so
    # an unregistered module dies mid-exec. Same pattern as the
    # consensus loader in services/tests/test_galahad_consensus_integration.py.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        # Pop the half-initialized module so no stale partial stays
        # reachable by name, then surface the reason.
        sys.modules.pop(spec.name, None)
        raise RuntimeError(
            f"sibling ml_pipeline failed to load: {exc}") from exc
    return module


def _ml_signal(engine: QuantEngine, args: dict[str, Any]) -> str:
    """ML pipeline tool handler."""
    from datetime import date, timedelta
    from pathlib import Path

    symbols = args.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        return "error: symbols must be a non-empty array"
    if len(symbols) > 20:
        return "error: at most 20 symbols per ML prediction"

    try:
        years = float(args.get("years", 2))
    except (TypeError, ValueError):
        return "error: years must be a number"
    if years < 1:
        return "error: years must be >= 1"

    start = (date.today() - timedelta(days=int(years * 365.25))).isoformat()

    try:
        from quantkit.ml_pipeline import MLPipeline
    except ImportError:
        # The installed quantkit tree does not ship ml_pipeline (evolved
        # fork): fall back to the file-based sibling load before giving up.
        try:
            sibling = _load_sibling_ml_pipeline()
        except Exception as exc:  # noqa: BLE001 — handlers degrade to error strings
            return f"error: ml_pipeline sibling load failed: {str(exc)[:150]}"
        MLPipeline = getattr(sibling, "MLPipeline", None)
    if MLPipeline is None:
        return "error: ml_pipeline module not available"

    # Fetch data
    data_dict = {}
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if not symbol:
            continue
        result = engine.fetch_data(symbol, market="auto", start=start)
        if result.get("ok") and "df" in result:
            data_dict[symbol] = result["df"]

    if not data_dict:
        return "error: no data available for any symbol"

    # Train or load model. The tool contract is a clear error string, so
    # runtime-dependency gaps (a pickled model needing an absent booster,
    # a missing optional extra) degrade here instead of escaping as an
    # unhandled exception into the chat driver.
    model_dir = str(Path.home() / ".local" / "share" / "stammtisch" / "ml_models")
    try:
        pipe = MLPipeline(model_dir=model_dir)
        if not pipe.load_best():
            if len(data_dict) >= 3:
                pipe.train_cross_sectional(data_dict, label_col="fwd_ret_5")
            else:
                # Single-symbol fallback: train on that one fetched frame.
                sym, df = next(iter(data_dict.items()))
                pipe.train(df, label_col="fwd_ret_5")
    except Exception as exc:  # noqa: BLE001 — handlers degrade to error strings
        return f"error: ml_pipeline failed: {str(exc)[:150]}"

    # Generate predictions
    out = []
    for sym, df in data_dict.items():
        try:
            pred = pipe.predict(df)
            last_pred = float(pred.iloc[-1]) if len(pred) > 0 else 0
            hi = float(pred.expanding(min_periods=60).quantile(0.7).iloc[-1]) if len(pred) > 60 else 0
            lo = float(pred.expanding(min_periods=60).quantile(0.3).iloc[-1]) if len(pred) > 60 else 0
            signal = 1 if last_pred >= hi else (-1 if last_pred <= lo else 0)
            out.append({
                "symbol": sym,
                "ml_prediction": round(last_pred, 6),
                "signal": signal,
                "model": pipe.best_model.model_type if pipe.best_model else "none",
                "valid_ic": round(pipe.best_model.valid_ic, 4) if pipe.best_model else 0,
            })
        except Exception as e:
            out.append({"symbol": sym, "error": str(e)[:100]})

    return json.dumps(out, ensure_ascii=False, default=str)


def default_tools(engine: QuantEngine) -> dict[str, Tool]:
    """Built-in tool set. Edit here to plug or unplug capabilities."""

    def get_ohlcv(args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol", "")).strip().upper()
        if not symbol:
            return "error: symbol is required"
        context = build_market_context([symbol], engine)
        return context or f"{symbol}: no local OHLCV available"

    def run_backtest(args: dict[str, Any]) -> str:
        from datetime import date, timedelta

        symbol = str(args.get("symbol", "")).strip().upper()
        strategy = str(args.get("strategy", "dual_ma")).strip() or "dual_ma"
        try:
            fast = int(args.get("fast", 20))
            slow = int(args.get("slow", 50))
            years = float(args.get("years", 2))
        except (TypeError, ValueError):
            return "error: fast/slow must be integers and years a number"
        if not 0.5 <= years <= 10:
            return "error: years must be within 0.5..10"
        if not symbol:
            return "error: symbol is required"
        # The window defaults to the same 2-year span the strategy scan
        # uses, so tool-verified numbers and the board agree; the model can
        # widen it explicitly when a longer history matters.
        start = (date.today() - timedelta(days=int(years * 365.25))).isoformat()
        result = engine.run_backtest(
            symbol, strategy=strategy, fast=fast, slow=slow,
            start=start, cost_tier="low",
        )
        if not result.get("ok"):
            return f"{symbol}: backtest unavailable ({result.get('error', 'unknown')})"
        summary = result.get("summary")
        if summary is not None and not isinstance(summary, dict):
            from dataclasses import asdict, is_dataclass
            summary = asdict(summary) if is_dataclass(summary) else dict(summary)
        payload: dict[str, Any] = {
            "symbol": symbol, "strategy": strategy, "fast": fast, "slow": slow,
        }
        if isinstance(summary, dict):
            payload.update({
                k: v for k, v in summary.items()
                if not str(k).startswith("_")
                and k not in ("equity_series", "returns_series", "positions_series")
            })
        payload["stats"] = result.get("stats", {})
        return json.dumps(payload, ensure_ascii=False, default=str)

    def scan_backtests(args: dict[str, Any]) -> str:
        import json as _json
        from datetime import date, timedelta

        symbols = args.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            return "error: symbols must be a non-empty array"
        if len(symbols) > 40:
            return "error: at most 40 symbols per scan"
        strategy = str(args.get("strategy", "dual_ma")).strip() or "dual_ma"
        try:
            years = float(args.get("years", 2))
        except (TypeError, ValueError):
            return "error: years must be a number"
        if not 0.5 <= years <= 10:
            return "error: years must be within 0.5..10"
        start = (date.today() - timedelta(days=int(years * 365.25))).isoformat()
        out = []
        for raw in symbols:
            symbol = str(raw).strip().upper()
            if not symbol:
                continue
            result = engine.run_backtest(
                symbol, strategy=strategy, start=start, cost_tier="low",
            )
            if not result.get("ok"):
                out.append({"symbol": symbol, "error": str(result.get("error"))[:60]})
                continue
            summary = result.get("summary")
            if summary is not None and not isinstance(summary, dict):
                from dataclasses import asdict, is_dataclass
                summary = asdict(summary) if is_dataclass(summary) else dict(summary)
            if isinstance(summary, dict):
                out.append({
                    "symbol": symbol,
                    **{k: v for k, v in summary.items()
                       if not str(k).startswith("_")
                       and k not in ("equity_series", "returns_series", "positions_series")},
                })
        return _json.dumps(out, ensure_ascii=False, default=str)

    tools = [
        Tool(
            name="scan_backtests",
            description=(
                "Batch backtest one strategy over many tickers in one call "
                "(same engine and window as run_backtest). Returns an array "
                "of per-symbol summaries: total_return, cagr, sharpe, "
                "max_drawdown, win_rate, trades. Prefer this over repeated "
                "run_backtest calls whenever more than two symbols need "
                "verification."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Tickers like 600519.SS, 0700.HK, AAPL (max 40)",
                    },
                    "strategy": {"type": "string", "enum": ["dual_ma", "rsi_mr"], "default": "dual_ma"},
                    "years": {"type": "number", "default": 2},
                },
                "required": ["symbols"],
            },
            handler=scan_backtests,
        ),
        Tool(
            name="get_ohlcv",
            description=(
                "Get local daily OHLCV market data summary for a ticker: bar "
                "count and range, last close, 5/20/60-day returns, MA20/MA60, "
                "52-week range, last 5 closes. Use whenever a question needs "
                "current or historical prices; never quote prices without it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Ticker like 600519.SS, AAPL, SOHU, 0700.HK",
                    }
                },
                "required": ["symbol"],
            },
            handler=get_ohlcv,
        ),
        Tool(
            name="run_backtest",
            description=(
                "Run a long-only backtest over local daily bars and return "
                "trades, total return, CAGR, Sharpe, max drawdown, win rate. "
                "Use when asked how a rule-based strategy performed on a ticker."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker like 600519.SS or AAPL"},
                    "strategy": {"type": "string", "enum": ["dual_ma", "rsi_mr"], "default": "dual_ma"},
                    "fast": {"type": "integer", "default": 20},
                    "slow": {"type": "integer", "default": 50},
                    "years": {
                        "type": "number", "default": 2,
                        "description": "Lookback window in years (0.5..10); default matches the strategy-scan board",
                    },
                },
                "required": ["symbol"],
            },
            handler=run_backtest,
        ),
        Tool(
            name="ml_signal",
            description=(
                "Run ML factor-based prediction (Alpha158 + LightGBM) on "
                "one or more tickers. Returns predicted return direction and "
                "signal strength. Use when the user asks for ML/quantitative "
                "analysis beyond simple backtests. Requires enough history "
                "(200+ bars)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tickers like 600519.SS, 0700.HK, AAPL",
                    },
                    "years": {
                        "type": "number",
                        "default": 2,
                        "description": "Lookback window in years (>= 1)",
                    },
                },
                "required": ["symbols"],
            },
            handler=lambda args: _ml_signal(engine, args),
        ),
    ]
    return {tool.name: tool for tool in tools}
