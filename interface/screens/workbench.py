"""The quant workbench — ONE parameterized screen, five tools.

Replaces the old TUI's five analysis screens (``tui/analysis.py``,
read-only reference — never imported): :data:`TOOLS` specs drive title,
input fields with defaults, the action button label and the result
renderer, so a new tool is a spec + renderer pair, not a new screen.

Discipline carried over from the spine:

- Every tool call runs OFF the UI thread (a Textual thread worker);
  the spine's :class:`~interface.app.spine.SingleFlight` gate is reused
  verbatim — one run in the air at a time, repeats notify instead of
  stacking. A generation counter drops results from a popped screen.
- Engine choice is honest: the snapshot's services strip decides
  (:func:`interface.services.quant_engine.resolve_engine`); the degraded
  state renders its reason instead of hiding it, and the demo path
  always works.
- Recents per tool live in memory only (config persistence is M4).
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Static

from interface.app.router import WORKBENCH_TOOLS
from interface.app.spine import SingleFlight
from interface.render.tokens import token
from interface.services.quant_engine import resolve_engine

__all__ = ["TOOLS", "FieldSpec", "ToolEngine", "ToolSpec", "WorkbenchScreen"]

_MISSING = "—"


@dataclass(frozen=True)
class FieldSpec:
    """One input field of a tool form."""

    key: str
    label: str
    default: str = ""
    placeholder: str = ""


@dataclass(frozen=True)
class ToolSpec:
    """Everything one workbench tool needs, spec-driven."""

    tool: str
    title: str
    fields: tuple[FieldSpec, ...]
    action: str  # the run button's label
    hint: str  # one-line usage hint for the header


TOOLS: dict[str, ToolSpec] = {
    "fetch": ToolSpec(
        "fetch", "DATA FETCH",
        (FieldSpec("symbol", "symbol", "AAPL", "AAPL, 600519.SS, BTC-USD"),),
        "Fetch", "fetch market data for one symbol",
    ),
    "backtest": ToolSpec(
        "backtest", "BACKTEST",
        (FieldSpec("symbol", "symbol", "AAPL"),
         FieldSpec("strategy", "strategy", "dual_ma", "dual_ma | rsi_mr"),
         FieldSpec("fast", "fast MA", "20"),
         FieldSpec("slow", "slow MA", "50")),
        "Run backtest", "run a strategy backtest",
    ),
    "indicators": ToolSpec(
        "indicators", "INDICATORS",
        (FieldSpec("symbol", "symbol", "AAPL"),),
        "Compute", "compute technical indicators (RSI/MACD/BB/SMA/ATR)",
    ),
    "portfolio": ToolSpec(
        "portfolio", "PORTFOLIO",
        (FieldSpec("symbols", "symbols", "AAPL,MSFT,GOOG"),
         FieldSpec("strategy", "strategy", "momentum",
                   "momentum | dual_ma")),
        "Run portfolio", "run a portfolio across symbols",
    ),
    "gates": ToolSpec(
        "gates", "GATES",
        (FieldSpec("symbol", "symbol", "AAPL"),),
        "Evaluate", "run a backtest, then evaluate the six gates",
    ),
}

# Spec/registry guard: router validation and the spec table cannot drift.
assert set(TOOLS) == set(WORKBENCH_TOOLS)

#: In-memory recents per tool (process lifetime; persistence is M4).
_TOOL_RECENTS: dict[str, deque[str]] = {}
_RECENTS_CAP = 5


def _remember(tool: str, line: str) -> None:
    recent = _TOOL_RECENTS.setdefault(tool, deque(maxlen=_RECENTS_CAP))
    if line in recent:
        recent.remove(line)
    recent.appendleft(line)


def _recent_line(tool: str) -> str:
    recent = _TOOL_RECENTS.get(tool)
    return " · ".join(recent) if recent else ""


class ToolEngine(Protocol):
    """The engine seam the workbench consumes (quantkit or demo twin)."""

    label: str

    def fetch_data(self, symbol: str, **kwargs: Any) -> dict[str, Any]: ...

    def run_backtest(self, symbol: str, strategy: str = "dual_ma",
                     fast: int = 20, slow: int = 50,
                     **kwargs: Any) -> dict[str, Any]: ...

    def compute_indicators(
        self, symbol: str, **kwargs: Any) -> dict[str, Any]: ...

    def run_portfolio(self, symbols: list[str], strategy: str = "momentum",
                      **kwargs: Any) -> dict[str, Any]: ...

    def evaluate_gates(self, metrics: dict[str, Any]) -> dict[str, Any]: ...


def _call_tool(engine: ToolEngine, tool: str,
               args: dict[str, str]) -> dict[str, Any]:
    """One tool call through the engine (the copied call shapes)."""
    symbol = args.get("symbol", "").strip()
    if tool == "fetch":
        return engine.fetch_data(symbol)
    if tool == "indicators":
        return engine.compute_indicators(symbol)
    if tool == "backtest":
        try:
            fast = int(args.get("fast", "20"))
            slow = int(args.get("slow", "50"))
        except ValueError:
            return {"ok": False, "error": "fast/slow must be integers"}
        return engine.run_backtest(
            symbol, strategy=args.get("strategy", "dual_ma").strip(),
            fast=fast, slow=slow)
    if tool == "portfolio":
        symbols = [s.strip() for s in args.get("symbols", "").split(",")
                   if s.strip()]
        if not symbols:
            return {"ok": False, "error": "no symbols given"}
        return engine.run_portfolio(
            symbols, strategy=args.get("strategy", "momentum").strip())
    # gates: a backtest first, then the six-gate evaluation on its stats
    # (the old TUI's call shape).
    backtest = engine.run_backtest(symbol)
    if not backtest["ok"]:
        return backtest
    stats = backtest["stats"]
    metrics = {key: stats.get(key, 0) for key in (
        "total_return", "cagr", "sharpe", "max_drawdown", "win_rate",
        "trades")}
    metrics["cost_bps_effective"] = stats.get("cost_bps_effective", 5.0)
    return engine.evaluate_gates(metrics)


# ── result renderers (rich; every color via tokens) ─────────────────────


def _error_text(result: dict[str, Any]) -> Text:
    return Text(f"error: {result.get('error', '?')}",
                style=token("state.crit"))


def _metric_table(pairs: list[tuple[str, str]]) -> Table:
    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("metric", style=token("text.muted"))
    table.add_column("value", style=token("text.primary"), justify="right")
    for metric, value in pairs:
        table.add_row(metric, value)
    return table


def _render_fetch(result: dict[str, Any]):
    if not result.get("ok"):
        return _error_text(result)
    return _metric_table([
        ("symbol", str(result["symbol"])),
        ("rows", f"{result['rows']:,}"),
        ("columns", ", ".join(result["columns"])),
        ("first", str(result["first_date"])),
        ("last", str(result["last_date"])),
        ("last close", f"{result['last_close']:,.2f}"),
        ("last volume", f"{result['last_volume']:,.0f}"),
    ])


def _render_backtest(result: dict[str, Any]):
    if not result.get("ok"):
        return _error_text(result)
    s = result["summary"]
    return _metric_table([
        ("total return", f"{s.total_return:.2%}"),
        ("cagr", f"{s.cagr:.2%}"),
        ("sharpe", f"{s.sharpe:.2f}"),
        ("max drawdown", f"{s.max_drawdown:.2%}"),
        ("win rate", f"{s.win_rate:.2%}"),
        ("trades", f"{s.trades:d}"),
        ("final equity", f"{s.final_equity:.4f}"),
        ("cost (bps)", f"{s.cost_bps:.1f}"),
    ])


def _render_indicators(result: dict[str, Any]):
    if not result.get("ok"):
        return _error_text(result)
    s = result["summary"]
    price = result["last_price"]
    rsi_label = ("OVERSOLD" if s.rsi < 30
                 else "OVERBOUGHT" if s.rsi > 70 else "NEUTRAL")
    macd_label = "BULLISH" if s.macd_hist > 0 else "BEARISH"
    return _metric_table([
        ("last price", f"{price:,.2f}"),
        ("RSI(14)", f"{s.rsi:.2f}  {rsi_label}"),
        ("MACD", f"{s.macd:.4f}"),
        ("MACD signal", f"{s.macd_signal:.4f}"),
        ("MACD hist", f"{s.macd_hist:.4f}  {macd_label}"),
        ("BB upper/mid/lower",
         f"{s.bb_upper:,.2f} / {s.bb_mid:,.2f} / {s.bb_lower:,.2f}"),
        ("SMA(20/50/200)",
         f"{s.sma_20:,.2f} / {s.sma_50:,.2f} / {s.sma_200:,.2f}"),
        ("ATR(14)", f"{s.atr_14:.2f}"),
        ("vol(20)", f"{s.vol_20:.4f}"),
    ])


def _render_portfolio(result: dict[str, Any]):
    if not result.get("ok"):
        return _error_text(result)
    s = result["summary"]
    return _metric_table([
        ("total return", f"{s.total_return:.2%}"),
        ("cagr", f"{s.cagr:.2%}"),
        ("sharpe", f"{s.sharpe:.2f}"),
        ("max drawdown", f"{s.max_drawdown:.2%}"),
        ("win rate", f"{s.win_rate:.2%}"),
        ("trades", f"{s.trades:d}"),
        ("n assets", f"{s.n_assets:d}"),
        ("avg turnover", f"{s.avg_turnover:.4f}"),
        ("avg gross exposure", f"{s.avg_gross_exposure:.4f}"),
    ])


def _render_gates(result: dict[str, Any]):
    if not result.get("ok"):
        return _error_text(result)
    report = result["report"]
    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("gate", style=token("text.muted"))
    table.add_column("status", justify="right")
    table.add_column("value", style=token("text.primary"), justify="right")
    table.add_column("threshold", style=token("text.muted"))
    table.add_column("detail", style=token("text.muted"))
    for gate in report.gates:
        status = Text("PASS" if gate["passed"] else "FAIL",
                      style=token("state.ok" if gate["passed"]
                                  else "state.crit"))
        table.add_row(
            str(gate["gate_id"]),
            status,
            _MISSING if gate.get("value") is None else str(gate["value"]),
            _MISSING if gate.get("threshold") is None
            else str(gate["threshold"]),
            str(gate.get("detail") or ""),
        )
    verdict = Text()
    verdict.append(f"{report.n_passed}/{report.n_total} passed", style=token(
        "state.ok" if report.all_passed else "state.warn"))
    return Group(verdict, table)


_RENDERERS = {
    "fetch": _render_fetch,
    "backtest": _render_backtest,
    "indicators": _render_indicators,
    "portfolio": _render_portfolio,
    "gates": _render_gates,
}


class WorkbenchScreen(Screen[None]):
    """One parameterized tool screen (route: ``workbench``)."""

    route_name = "workbench"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back to the previous screen"),
    ]
    CSS = """
    WorkbenchScreen { layout: vertical; }
    #wb-header { height: auto; color: $text-primary; padding: 0 1; }
    #wb-form { height: auto; padding: 0 1; }
    #wb-form .wb-label { width: auto; padding: 1 1 0 0; color: $text-muted; }
    #wb-form Input { width: 24; }
    #wb-run { margin: 0 1; }
    #wb-result {
        border: round $panel-border;
        border-title-color: $panel-title;
        padding: 0 1;
        color: $text-primary;
    }
    """

    def __init__(self, tool: str, *, engine: ToolEngine | None = None) -> None:
        super().__init__()
        if tool not in TOOLS:
            raise ValueError(f"unknown workbench tool: {tool!r}")
        self.spec = TOOLS[tool]
        self._engine_override = engine
        self._flight = SingleFlight()
        self._generation = 0
        self._lock = threading.Lock()
        #: Test observability: results landed, engines used, lines recorded.
        self.results_landed = 0
        self.engine_labels: list[str] = []
        self.recorded_inputs: list[str] = []
        self.last_result: dict[str, Any] | None = None

    def compose(self):
        yield Static(id="wb-header")
        with Horizontal(id="wb-form"):
            for field in self.spec.fields:
                yield Static(field.label, classes="wb-label")
                yield Input(value=field.default, placeholder=field.placeholder,
                            id=f"wb-field-{field.key}")
            yield Button(self.spec.action, id="wb-run")
        with VerticalScroll():
            yield Static("waiting for input", id="wb-result")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#wb-header", Static).update(self._header_text())
        self.query_one("#wb-result", Static).border_title = "RESULT"
        self.query_one("#wb-field-" + self.spec.fields[0].key, Input).focus()

    # -- header -----------------------------------------------------------

    def _header_text(self) -> Text:
        header = Text()
        header.append(f"{self.spec.title}  ", style=f"bold {token('panel.title')}")
        header.append(self.spec.hint, style=token("text.muted"))
        recent = _recent_line(self.spec.tool)
        if recent:
            header.append(f"   recent: {recent}", style=token("text.muted"))
        return header

    # -- running a tool -----------------------------------------------------

    def _resolve_engine(self) -> tuple[ToolEngine, str]:
        """The engine for this run (an injected engine wins — tests)."""
        if self._engine_override is not None:
            return self._engine_override, self._engine_override.label
        frame = self.app.spine.last_frame
        services = frame.services if frame is not None else ()
        return resolve_engine(services)

    def _submit(self) -> None:
        if not self._flight.try_start():
            self.notify("a run is already in flight", severity="warning")
            return
        args = {
            field.key: self.query_one(f"#wb-field-{field.key}", Input).value
            for field in self.spec.fields
        }
        line = " ".join(args[field.key].strip() for field in self.spec.fields
                        if args[field.key].strip())
        if line:
            _remember(self.spec.tool, line)
            self.recorded_inputs.append(line)
            self.query_one("#wb-header", Static).update(self._header_text())
        engine, label = self._resolve_engine()
        self.engine_labels.append(label)
        with self._lock:
            self._generation += 1
            generation = self._generation
        self.query_one("#wb-result", Static).update(
            Text(f"running {self.spec.tool} via {label}…",
                 style=token("text.muted")))
        self.run_worker(
            lambda: self._execute(engine, generation, args),
            name="workbench-tool", group="workbench", thread=True,
        )

    #: Review C-M3: the old tui/engine.py carried a 30s per-call ceiling
    #: (a hung provider must not block a zone scan); the interface copy
    #: dropped it and a hung call wedged this screen's SingleFlight slot
    #: forever. Same ceiling, applied to every tool call uniformly.
    TOOL_CALL_CEILING_S = 30.0

    def _execute(self, engine: ToolEngine, generation: int,
                 args: dict[str, str]) -> None:
        """Worker-thread body: one engine call, degraded to a result dict.

        The call runs in a one-shot worker pool with a hard ceiling: on
        timeout the RESULT degrades (the pool worker may still be wedged,
        but the flight slot frees and the UI stays live — same trade-off
        the old engine made with its manual pool lifecycle).
        """
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FT

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_call_tool, engine, self.spec.tool, args)
            try:
                result = future.result(timeout=self.TOOL_CALL_CEILING_S)
            except FT:
                result = {"ok": False, "error":
                          f"engine call exceeded {self.TOOL_CALL_CEILING_S:.0f}s "
                          "ceiling; it was abandoned (the provider may still "
                          "be wedged) — try again or check the feed"}
            except Exception as exc:  # noqa: BLE001 - degrade, never raise
                result = {"ok": False, "error": str(exc)}
        finally:
            pool.shutdown(wait=False)
            self._flight.finish()
        try:
            self.app.call_from_thread(self._deliver, generation, result)
        except Exception:  # noqa: BLE001, S110 - app torn down; land quietly
            pass

    def _deliver(self, generation: int, result: dict[str, Any]) -> None:
        """UI-thread landing: newest generation wins, stale runs drop."""
        if not self.is_mounted:
            return
        with self._lock:
            if generation != self._generation:
                return
        body = _RENDERERS[self.spec.tool](result)
        # Review C-M3: the engine label used to live only in the transient
        # "running…" line — once results landed, a degraded engine became
        # invisible. Stamp the serving engine onto the result surface so
        # the provenance survives.
        label = (self.engine_labels[generation - 1]
                 if 0 < generation <= len(self.engine_labels) else "")
        if label:
            from rich.console import Group

            stamped = Group(
                body,
                Text(f"via {label}", style=token("provenance.src")),
            )
            self.query_one("#wb-result", Static).update(stamped)
        else:
            self.query_one("#wb-result", Static).update(body)
        self.results_landed += 1
        self.last_result = result

    # -- events -----------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "wb-run":
            self._submit()

    def action_back(self) -> None:
        self.app.pop_screen()
