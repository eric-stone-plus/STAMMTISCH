"""Analysis screens — with recent items, input validation, proper feedback."""

from __future__ import annotations

import threading
from typing import Any, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Sparkline, Static


import logging

logger = logging.getLogger(__name__)


def _series_values(series: Any, cap: int = 400) -> list[float]:
    """Flatten a pandas Series/list into floats, downsampled to ``cap``.

    The equity/returns series quantkit returns can be thousands of
    points; the terminal sparkline renders a handful of columns, so
    subsample instead of shipping every point across the UI thread.
    """
    if series is None:
        return []
    try:
        values = [float(value) for value in series]
    except (TypeError, ValueError):
        return []
    if len(values) <= cap:
        return values
    step = len(values) / cap
    return [values[int(index * step)] for index in range(cap)]


def _run_async(screen: Any, target: Callable, callback: Callable,
               dedup_key: str | None = None) -> None:
    """Run target() in a background thread, deliver to callback on the UI thread.

    Guards: worker exceptions become error results; stale submissions are
    dropped via a per-screen generation counter (the newest submission
    wins, out-of-order completions never overwrite it); callbacks never
    fire after the screen was popped or the app quit.

    dedup_key: when given, only one target per key may be in flight —
    repeated submissions with the same key while it runs are dropped
    instead of stacking identical workers (a heavy board fetch would
    otherwise pile up under rapid refreshes).
    """
    gen = getattr(screen, "_async_gen", 0) + 1
    screen._async_gen = gen
    inflight = getattr(screen, "_async_inflight", None)
    if inflight is None:
        inflight = {}
        screen._async_inflight = inflight
    # Per-key delivery generation: screens now run several keyed loads at
    # once (futures board/adapters/quotes). A single screen-wide counter
    # silently discarded every callback but the last-submitted one.
    key_gen = getattr(screen, "_async_key_gen", None)
    if key_gen is None:
        key_gen = {}
        screen._async_key_gen = key_gen
    if dedup_key is not None:
        if dedup_key in inflight:
            return
        inflight[dedup_key] = gen
        key_gen[dedup_key] = gen

    def _worker():
        try:
            try:
                result = target()
            except Exception as e:
                result = {"ok": False, "error": str(e)}
        finally:
            if dedup_key is not None:
                inflight.pop(dedup_key, None)

        def _deliver():
            if not screen.is_mounted:
                return
            if dedup_key is not None:
                if getattr(screen, "_async_key_gen", {}).get(dedup_key) != gen:
                    return
            elif getattr(screen, "_async_gen", 0) != gen:
                return
            try:
                callback(result)
            except Exception:
                # display helpers must never crash the UI thread, but a
                # swallowed bug makes refreshes silently do nothing —
                # log it instead of vanishing it.
                logger.exception("async callback failed")
                screen.notify("refresh failed; see log", severity="error")

        try:
            screen.app.call_from_thread(_deliver)
        except Exception:
            # Periodic refresh workers routinely race app teardown on
            # exit; that is the normal shutdown path, not an error.
            logger.debug("call_from_thread skipped (app torn down)")

    threading.Thread(target=_worker, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════
# Data Fetch
# ═══════════════════════════════════════════════════════════════════


class DataFetchScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("v", "view_chart", "Chart"),
    ]
    CSS = "DataFetchScreen { layout: vertical; }"

    def __init__(self, engine: Any, config: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.engine = engine
        self.config = config
        self._last_symbol: str | None = None
        self._last_df: Any = None

    def compose(self) -> ComposeResult:
        recent = ""
        if self.config:
            symbols = self.config.recent_symbols[:5]
            if symbols:
                recent = f"  Recent: {', '.join(symbols)}"
        yield Static(f"  Data Fetch  |  Enter symbol (e.g. AAPL, 600519.SS, BTC-USD)  |  [V] Chart  |  Esc back{recent}", classes="header-bar")
        yield Input(placeholder="Symbol (e.g. AAPL)...", id="df-input")
        with ScrollableContainer():
            yield Static("  Enter a symbol to fetch data.", id="df-output")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#df-input", Input).focus()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_view_chart(self) -> None:
        from .charts import TerminalChartScreen, df_to_candles

        if self._last_df is None or not self._last_symbol:
            self.notify("Fetch a symbol first — [V] charts the fetched frame.",
                        severity="warning")
            return
        candles = df_to_candles(self._last_df)
        if not candles:
            self.notify("The fetched frame has no OHLC columns to chart.",
                        severity="warning")
            return
        self.app.push_screen(TerminalChartScreen(
            self.engine, self.config, self._last_symbol, candles=candles))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        symbol = event.value.strip().upper()
        if not symbol:
            return
        event.input.value = ""
        out = self.query_one("#df-output", Static)
        out.update(f"  Fetching {symbol}...")

        if self.config:
            self.config.add_recent_symbol(symbol)

        def _fetch():
            return self.engine.fetch_data(symbol)

        def _on_result(result):
            if result["ok"]:
                self._last_symbol = symbol
                self._last_df = result.get("df")
                out.update(
                    f"  [{symbol}]  —  [V] chart\n"
                    f"  Rows:       {result['rows']}\n"
                    f"  Columns:    {', '.join(result['columns'])}\n"
                    f"  First:      {result['first_date']}\n"
                    f"  Last:       {result['last_date']}\n"
                    f"  Last Close: {result['last_close']:.2f}\n"
                    f"  Last Vol:   {result['last_volume']:,.0f}"
                )
            else:
                out.update(f"  [ERROR] {result['error']}")

        _run_async(self, _fetch, _on_result)


# ═══════════════════════════════════════════════════════════════════
# Backtest
# ═══════════════════════════════════════════════════════════════════


class BacktestScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]
    CSS = "BacktestScreen { layout: vertical; }"

    def __init__(self, engine: Any, config: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.engine = engine
        self.config = config

    def compose(self) -> ComposeResult:
        recent = ""
        if self.config:
            symbols = self.config.recent_symbols[:3]
            strategies = self.config.recent_strategies[:3]
            if symbols:
                recent += f"  Symbols: {', '.join(symbols)}"
            if strategies:
                recent += f"  |  Strategies: {', '.join(strategies)}"
        yield Static("  Backtest  |  Format: SYMBOL STRATEGY FAST SLOW  |  Esc back", classes="header-bar")
        if recent:
            yield Static(recent, classes="header-bar")
        yield Input(placeholder="e.g. AAPL dual_ma 20 50", id="bt-input")
        with ScrollableContainer():
            yield Static(
                "  Strategies:\n"
                "    dual_ma   - Dual moving average crossover\n"
                "    rsi_mr    - RSI mean reversion\n\n"
                "  Example: AAPL dual_ma 20 50\n"
                "  Example: 600519.SS rsi_mr",
                id="bt-output",
            )
            yield DataTable(id="bt-table", cursor_type="row")
            yield Sparkline(min_color="#404040", max_color="#66bb6a",
                            id="bt-equity")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#bt-input", Input).focus()
        self.query_one("#bt-table", DataTable).add_columns("METRIC", "VALUE")

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        parts = event.value.strip().split()
        if not parts:
            return
        event.input.value = ""

        symbol = parts[0].upper()
        strategy = parts[1] if len(parts) > 1 else (self.config.default_strategy if self.config else "dual_ma")
        out = self.query_one("#bt-output", Static)
        try:
            fast = int(parts[2]) if len(parts) > 2 else (self.config.default_fast if self.config else 20)
            slow = int(parts[3]) if len(parts) > 3 else (self.config.default_slow if self.config else 50)
        except ValueError:
            out.update("  [ERROR] fast/slow must be integers")
            return

        if self.config:
            self.config.add_recent_symbol(symbol)
            self.config.add_recent_strategy(strategy)

        out.update(f"  Running: {symbol} / {strategy} / fast={fast} slow={slow}...")

        cost_tier = self.config.default_cost_tier if self.config else "low"

        def _run():
            return self.engine.run_backtest(symbol, strategy=strategy, fast=fast, slow=slow,
                                            cost_tier=cost_tier)

        def _on_result(result):
            table = self.query_one("#bt-table", DataTable)
            spark = self.query_one("#bt-equity", Sparkline)
            table.clear()
            spark.data = []
            if result["ok"]:
                s = result["summary"]
                out.update(f"  [{symbol} / {strategy}]  —  equity curve below")
                for metric, value in (
                    ("Total Return", f"{s.total_return:.2%}"),
                    ("CAGR", f"{s.cagr:.2%}"),
                    ("Sharpe", f"{s.sharpe:.2f}"),
                    ("Max Drawdown", f"{s.max_drawdown:.2%}"),
                    ("Win Rate", f"{s.win_rate:.2%}"),
                    ("Trades", f"{s.trades:d}"),
                    ("Final Equity", f"{s.final_equity:.4f}"),
                    ("Cost (bps)", f"{s.cost_bps:.1f}"),
                ):
                    table.add_row(metric, value)
                spark.data = _series_values(s.equity_series)
            else:
                out.update(f"  [ERROR] {result['error']}")

        _run_async(self, _run, _on_result)


# ═══════════════════════════════════════════════════════════════════
# Indicators
# ═══════════════════════════════════════════════════════════════════


class IndicatorsScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]
    CSS = "IndicatorsScreen { layout: vertical; }"

    def __init__(self, engine: Any, config: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.engine = engine
        self.config = config

    def compose(self) -> ComposeResult:
        recent = ""
        if self.config:
            symbols = self.config.recent_symbols[:5]
            if symbols:
                recent = f"  Recent: {', '.join(symbols)}"
        yield Static(f"  Technical Indicators  |  Enter symbol  |  Esc back{recent}", classes="header-bar")
        yield Input(placeholder="Symbol (e.g. AAPL)...", id="ind-input")
        with ScrollableContainer():
            yield Static("  Enter a symbol to compute indicators.", id="ind-output")
            yield DataTable(id="ind-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ind-input", Input).focus()
        self.query_one("#ind-table", DataTable).add_columns(
            "INDICATOR", "VALUE", "SIGNAL")

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        symbol = event.value.strip().upper()
        if not symbol:
            return
        event.input.value = ""

        if self.config:
            self.config.add_recent_symbol(symbol)

        out = self.query_one("#ind-output", Static)
        out.update(f"  Computing indicators for {symbol}...")

        def _run():
            return self.engine.compute_indicators(symbol)

        def _on_result(result):
            table = self.query_one("#ind-table", DataTable)
            table.clear()
            if result["ok"]:
                s = result["summary"]
                price = result["last_price"]
                rsi_label = "OVERSOLD" if s.rsi < 30 else ("OVERBOUGHT" if s.rsi > 70 else "NEUTRAL")
                macd_label = "BULLISH" if s.macd_hist > 0 else "BEARISH"
                out.update(f"  [{symbol}]  Price: {price:.2f}")
                table.add_row("RSI(14)", f"{s.rsi:.2f}", rsi_label)
                table.add_row("MACD", f"{s.macd:.4f}", "")
                table.add_row("MACD Signal", f"{s.macd_signal:.4f}", "")
                table.add_row("MACD Hist", f"{s.macd_hist:.4f}", macd_label)
                table.add_row("BB Upper", f"{s.bb_upper:.2f}", "")
                table.add_row("BB Mid", f"{s.bb_mid:.2f}", "")
                table.add_row("BB Lower", f"{s.bb_lower:.2f}", "")
                table.add_row("SMA(20)", f"{s.sma_20:.2f}",
                              "ABOVE" if price > s.sma_20 else "BELOW")
                table.add_row("SMA(50)", f"{s.sma_50:.2f}",
                              "ABOVE" if price > s.sma_50 else "BELOW")
                table.add_row("SMA(200)", f"{s.sma_200:.2f}",
                              "ABOVE" if price > s.sma_200 else "BELOW")
                table.add_row("ATR(14)", f"{s.atr_14:.2f}", "")
                table.add_row("Vol(20)", f"{s.vol_20:.4f}", "")
            else:
                out.update(f"  [ERROR] {result['error']}")

        _run_async(self, _run, _on_result)


# ═══════════════════════════════════════════════════════════════════
# Portfolio
# ═══════════════════════════════════════════════════════════════════


class PortfolioScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]
    CSS = "PortfolioScreen { layout: vertical; }"

    def __init__(self, engine: Any, config: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.engine = engine
        self.config = config

    def compose(self) -> ComposeResult:
        yield Static("  Portfolio  |  Format: SYM1,SYM2,SYM3 STRATEGY  |  Esc back", classes="header-bar")
        yield Input(placeholder="e.g. AAPL,MSFT,GOOG momentum", id="pf-input")
        with ScrollableContainer():
            yield Static(
                "  Strategies: momentum, dual_ma, topk (TopkDropout selection —\n"
                "  needs a quantkit tree with selection; format SYMS topk TOPK NDROP)\n\n"
                "  Example: AAPL,MSFT,GOOG,AMZN momentum\n"
                "  Example: 600519.SS,600036.SS dual_ma\n"
                "  Example: AAPL,MSFT,GOOG,NVDA,AMD topk 3 1",
                id="pf-output",
            )
            yield DataTable(id="pf-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pf-input", Input).focus()
        self.query_one("#pf-table", DataTable).add_columns("METRIC", "VALUE")

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        parts = event.value.strip().split()
        if not parts:
            return
        event.input.value = ""

        symbols = [s.strip().upper() for s in parts[0].split(",")]
        strategy = parts[1] if len(parts) > 1 else "momentum"

        if self.config:
            for s in symbols:
                self.config.add_recent_symbol(s)
            self.config.add_recent_strategy(strategy)

        out = self.query_one("#pf-output", Static)
        out.update(f"  Running portfolio: {', '.join(symbols)} / {strategy}...")

        rebalance = self.config.default_rebalance if self.config else "M"
        lookback = self.config.default_lookback if self.config else 60

        def _run():
            if strategy == "topk":
                topk = int(parts[2]) if len(parts) > 2 else 5
                n_drop = int(parts[3]) if len(parts) > 3 else 1
                return self.engine.run_topk_portfolio(
                    symbols, topk=topk, n_drop=n_drop,
                    rebalance=rebalance, lookback=lookback)
            return self.engine.run_portfolio(symbols, strategy=strategy,
                                             rebalance=rebalance, lookback=lookback)

        def _on_result(result):
            table = self.query_one("#pf-table", DataTable)
            table.clear()
            if result["ok"]:
                s = result["summary"]
                out.update(f"  [{', '.join(symbols)} / {strategy}]")
                for metric, value in (
                    ("Total Return", f"{s.total_return:.2%}"),
                    ("CAGR", f"{s.cagr:.2%}"),
                    ("Sharpe", f"{s.sharpe:.2f}"),
                    ("Max Drawdown", f"{s.max_drawdown:.2%}"),
                    ("Win Rate", f"{s.win_rate:.2%}"),
                    ("Trades", f"{s.trades:d}"),
                    ("N Assets", f"{s.n_assets:d}"),
                    ("Avg Turnover", f"{s.avg_turnover:.4f}"),
                    ("Avg Exposure", f"{s.avg_gross_exposure:.4f}"),
                ):
                    table.add_row(metric, value)
            else:
                out.update(f"  [ERROR] {result['error']}")

        _run_async(self, _run, _on_result)


# ═══════════════════════════════════════════════════════════════════
# Six-Gate Evaluation
# ═══════════════════════════════════════════════════════════════════


class GatesScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]
    CSS = "GatesScreen { layout: vertical; }"

    def __init__(self, engine: Any, config: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.engine = engine
        self.config = config

    def compose(self) -> ComposeResult:
        yield Static("  Six-Gate Evaluation  |  Enter symbol to run backtest + gate eval  |  Esc back", classes="header-bar")
        yield Input(placeholder="Symbol (e.g. AAPL)...", id="gate-input")
        with ScrollableContainer():
            yield Static("  Enter a symbol. Runs a backtest first, then evaluates all six gates.", id="gate-output")
            yield DataTable(id="gate-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#gate-input", Input).focus()
        self.query_one("#gate-table", DataTable).add_columns(
            "GATE", "STATUS", "VALUE", "THRESHOLD", "DETAIL")

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        symbol = event.value.strip().upper()
        if not symbol:
            return
        event.input.value = ""

        if self.config:
            self.config.add_recent_symbol(symbol)

        out = self.query_one("#gate-output", Static)
        out.update(f"  Running backtest + gate evaluation for {symbol}...")

        cost_tier = self.config.default_cost_tier if self.config else "low"

        def _run():
            bt = self.engine.run_backtest(symbol, cost_tier=cost_tier)
            if not bt["ok"]:
                return {"ok": False, "error": bt["error"]}
            stats = bt["stats"]
            metrics = {
                "total_return": stats.get("total_return", 0), "cagr": stats.get("cagr", 0),
                "sharpe": stats.get("sharpe", 0), "max_drawdown": stats.get("max_drawdown", 0),
                "win_rate": stats.get("win_rate", 0), "trades": stats.get("trades", 0),
            }
            return self.engine.evaluate_gates(metrics)

        def _on_result(result):
            table = self.query_one("#gate-table", DataTable)
            table.clear()
            if result["ok"]:
                report = result["report"]
                out.update(
                    f"  [{symbol}]  Gates: {report.n_passed}/{report.n_total} passed"
                    f"  —  All Passed: {'YES' if report.all_passed else 'NO'}"
                )
                for g in report.gates:
                    table.add_row(
                        str(g["gate_id"]),
                        "PASS" if g["passed"] else "FAIL",
                        "—" if g.get("value") is None else str(g["value"]),
                        "—" if g.get("threshold") is None else str(g["threshold"]),
                        str(g.get("detail") or ""),
                    )
            else:
                out.update(f"  [ERROR] {result['error']}")

        _run_async(self, _run, _on_result)
