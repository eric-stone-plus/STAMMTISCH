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

from .ai_driver import build_market_context
from .engine import QuantEngine


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

    tools = [
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
    ]
    return {tool.name: tool for tool in tools}
