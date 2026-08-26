"""Analysis screens — with recent items, input validation, proper feedback."""

from __future__ import annotations

import threading
from typing import Any, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, Static

from .widgets import GRAY, DIM, GREEN, AMBER, RED, CYAN, WHITE

import logging

logger = logging.getLogger(__name__)


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
    BINDINGS = [Binding("escape", "back", "Back")]
    CSS = "DataFetchScreen { layout: vertical; }"

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
        yield Static(f"  Data Fetch  |  Enter symbol (e.g. AAPL, 600519.SS, BTC-USD)  |  Esc back{recent}", classes="header-bar")
        yield Input(placeholder="Symbol (e.g. AAPL)...", id="df-input")
        with ScrollableContainer():
            yield Static("  Enter a symbol to fetch data.", id="df-output")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#df-input", Input).focus()

    def action_back(self) -> None:
        self.app.pop_screen()

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
                out.update(
                    f"  [{symbol}]\n"
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
        yield Static(f"  Backtest  |  Format: SYMBOL STRATEGY FAST SLOW  |  Esc back", classes="header-bar")
        if recent:
            yield Static(recent, classes="header-bar")
        yield Input(placeholder=f"e.g. AAPL dual_ma 20 50", id="bt-input")
        with ScrollableContainer():
            yield Static(
                "  Strategies:\n"
                "    dual_ma   - Dual moving average crossover\n"
                "    rsi_mr    - RSI mean reversion\n\n"
                "  Example: AAPL dual_ma 20 50\n"
                "  Example: 600519.SS rsi_mr",
                id="bt-output",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#bt-input", Input).focus()

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
            if result["ok"]:
                s = result["summary"]
                out.update(
                    f"  [{symbol} / {strategy}]\n\n"
                    f"  Total Return:   {s.total_return:>10.2%}\n"
                    f"  CAGR:           {s.cagr:>10.2%}\n"
                    f"  Sharpe:         {s.sharpe:>10.2f}\n"
                    f"  Max Drawdown:   {s.max_drawdown:>10.2%}\n"
                    f"  Win Rate:       {s.win_rate:>10.2%}\n"
                    f"  Trades:         {s.trades:>10d}\n"
                    f"  Final Equity:   {s.final_equity:>10.4f}\n"
                    f"  Cost (bps):     {s.cost_bps:>10.1f}"
                )
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
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ind-input", Input).focus()

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
            if result["ok"]:
                s = result["summary"]
                price = result["last_price"]
                rsi_label = "OVERSOLD" if s.rsi < 30 else ("OVERBOUGHT" if s.rsi > 70 else "NEUTRAL")
                macd_label = "BULLISH" if s.macd_hist > 0 else "BEARISH"
                out.update(
                    f"  [{symbol}]  Price: {price:.2f}\n\n"
                    f"  RSI(14):        {s.rsi:>10.2f}  {rsi_label}\n"
                    f"  MACD:           {s.macd:>10.4f}\n"
                    f"  MACD Signal:    {s.macd_signal:>10.4f}\n"
                    f"  MACD Hist:      {s.macd_hist:>10.4f}  {macd_label}\n\n"
                    f"  BB Upper:       {s.bb_upper:>10.2f}\n"
                    f"  BB Mid:         {s.bb_mid:>10.2f}\n"
                    f"  BB Lower:       {s.bb_lower:>10.2f}\n\n"
                    f"  SMA(20):        {s.sma_20:>10.2f}  {'ABOVE' if price > s.sma_20 else 'BELOW'}\n"
                    f"  SMA(50):        {s.sma_50:>10.2f}  {'ABOVE' if price > s.sma_50 else 'BELOW'}\n"
                    f"  SMA(200):       {s.sma_200:>10.2f}  {'ABOVE' if price > s.sma_200 else 'BELOW'}\n"
                    f"  ATR(14):        {s.atr_14:>10.2f}\n"
                    f"  Vol(20):        {s.vol_20:>10.4f}"
                )
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
                "  Strategies: momentum, dual_ma\n\n"
                "  Example: AAPL,MSFT,GOOG,AMZN momentum\n"
                "  Example: 600519.SS,600036.SS dual_ma",
                id="pf-output",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pf-input", Input).focus()

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
            return self.engine.run_portfolio(symbols, strategy=strategy,
                                             rebalance=rebalance, lookback=lookback)

        def _on_result(result):
            if result["ok"]:
                s = result["summary"]
                out.update(
                    f"  [{', '.join(symbols)} / {strategy}]\n\n"
                    f"  Total Return:   {s.total_return:>10.2%}\n"
                    f"  CAGR:           {s.cagr:>10.2%}\n"
                    f"  Sharpe:         {s.sharpe:>10.2f}\n"
                    f"  Max Drawdown:   {s.max_drawdown:>10.2%}\n"
                    f"  Win Rate:       {s.win_rate:>10.2%}\n"
                    f"  Trades:         {s.trades:>10d}\n"
                    f"  N Assets:       {s.n_assets:>10d}\n"
                    f"  Avg Turnover:   {s.avg_turnover:>10.4f}\n"
                    f"  Avg Exposure:   {s.avg_gross_exposure:>10.4f}"
                )
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
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#gate-input", Input).focus()

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
            if result["ok"]:
                report = result["report"]
                lines = [
                    f"  [{symbol}]  Gates: {report.n_passed}/{report.n_total} passed",
                    f"  All Passed: {'YES' if report.all_passed else 'NO'}",
                    "",
                ]
                for g in report.gates:
                    sym = "[PASS]" if g["passed"] else "[FAIL]"
                    lines.append(f"  {sym} {g['gate_id']}")
                    if g.get("value") is not None:
                        lines.append(f"        Value: {g['value']}")
                    if g.get("threshold") is not None:
                        lines.append(f"        Threshold: {g['threshold']}")
                    if g.get("detail"):
                        lines.append(f"        {g['detail']}")
                out.update("\n".join(lines))
            else:
                out.update(f"  [ERROR] {result['error']}")

        _run_async(self, _run, _on_result)
