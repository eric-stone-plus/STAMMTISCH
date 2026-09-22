"""The screen router and the ONE command vocabulary (⌃P palette + ``:`` bar).

A screen registry replaces per-screen push/pop fan-out: routes are
name → lazy factory, opened through :meth:`Router.goto`. Screens PUSH
onto Textual's existing stack and Esc pops them — the current push/pop
discipline is kept deliberately (``switch_screen`` has a known
Textual 8.2.8 IndexError on the initial screen; do not switch).

The palette (ctrl+p) and the command bar (``:``) are two thin entry
points to the same :meth:`Router.run_command` vocabulary; executed
commands land in a shared MRU recents deque (capacity 8). Unknown or
failed commands notify and are NEVER recorded.

The parse/registry/recents core is Textual-free apart from the two modal
screens at the bottom of this module; :class:`Router` talks to its host
through the :class:`RouterHost` protocol so the logic drives headlessly
under a fake host in tests.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from interface.snapshot import WorkstationSnapshot

__all__ = [
    "COMMANDS",
    "ROOT_ROUTE",
    "WORKBENCH_TOOLS",
    "Command",
    "CommandBarScreen",
    "CommandPaletteScreen",
    "Recents",
    "Router",
]

#: The base route: ``goto("runs")`` pops every screen above it.
ROOT_ROUTE = "runs"

#: The workbench's parameterized tools (kept here so command validation
#: and the workbench specs cannot drift apart).
WORKBENCH_TOOLS: tuple[str, ...] = (
    "fetch", "backtest", "indicators", "portfolio", "gates",
)

_RECENTS_CAPACITY = 8

#: A registry factory: called lazily, only when a route is opened.
ScreenFactory = Callable[..., Any]


@dataclass(frozen=True)
class Command:
    """One command of the shared vocabulary (name, help, routing)."""

    name: str
    description: str
    route: str
    arg: str | None = None  # single positional forwarded as this kwarg
    default_arg: str | None = None  # used when omitted (None = required)
    fixed_kwargs: Mapping[str, str] = field(default_factory=dict)


COMMANDS: dict[str, Command] = {
    "runs": Command("runs", "show the runs overview wall", ROOT_ROUTE),
    "workbench": Command(
        "workbench",
        "open a workbench tool: workbench <tool>",
        "workbench",
        arg="tool",
        default_arg="fetch",
    ),
    "fetch": Command(
        "fetch", "workbench: fetch market data for one symbol",
        "workbench", fixed_kwargs={"tool": "fetch"},
    ),
    "backtest": Command(
        "backtest", "workbench: run a strategy backtest",
        "workbench", fixed_kwargs={"tool": "backtest"},
    ),
    "indicators": Command(
        "indicators", "workbench: compute technical indicators",
        "workbench", fixed_kwargs={"tool": "indicators"},
    ),
    "portfolio": Command(
        "portfolio", "workbench: run a portfolio across symbols",
        "workbench", fixed_kwargs={"tool": "portfolio"},
    ),
    "gates": Command(
        "gates", "workbench: evaluate the six trading gates",
        "workbench", fixed_kwargs={"tool": "gates"},
    ),
    "detail": Command(
        "detail", "open the full run detail screen: detail <run-id>",
        "detail", arg="run_id",
    ),
    "delete": Command(
        "delete",
        "delete a run (fail-closed confirm dialog, audited): delete <run-id>",
        "confirm_delete", arg="run_id",
    ),
    # M6: the first absorbed read-only screens (RETIREMENT Option B).
    "feeds": Command(
        "feeds",
        "feed provider health: ok/fail/latency counters + cache split",
        "feeds",
    ),
    "ledger": Command(
        "ledger",
        "trade ledger: FIFO positions + fills, read-only (marks live)",
        "ledger",
    ),
    "energy": Command(
        "energy",
        "EIA energy watchlist tape (read-only, needs key + proxy)",
        "energy",
    ),
    "polymarket": Command(
        "polymarket",
        "Polymarket prediction-market tape (read-only, filterable)",
        "polymarket",
    ),
    "sentiment": Command(
        "sentiment",
        "daily sentiment tape with report-day history",
        "sentiment",
    ),
}


class RouterHost(Protocol):
    """What the router needs from its app (WorkstationShell satisfies it)."""

    def push_screen(self, screen: Any) -> Any: ...

    def pop_screen(self) -> Any: ...

    def pop_to_root(self) -> None: ...

    def notify(self, message: str, **kwargs: Any) -> None: ...

    @property
    def screen(self) -> Any: ...

    def current_frame(self) -> WorkstationSnapshot | None: ...


class Recents:
    """MRU of executed command lines, shared by the bar and the palette."""

    def __init__(self, capacity: int = _RECENTS_CAPACITY) -> None:
        self._capacity = capacity
        self._lines: deque[str] = deque()

    def record(self, line: str) -> None:
        """Move ``line`` to the front (deduped, capacity-capped)."""
        cleaned = " ".join(line.split())
        if not cleaned:
            return
        if cleaned in self._lines:
            self._lines.remove(cleaned)
        self._lines.appendleft(cleaned)
        while len(self._lines) > self._capacity:
            self._lines.pop()

    def __iter__(self) -> Any:
        return iter(tuple(self._lines))

    def __len__(self) -> int:
        return len(self._lines)

    def first(self) -> str | None:
        return self._lines[0] if self._lines else None


class Router:
    """Name → lazy screen factory, with the shared command vocabulary."""

    def __init__(self, host: RouterHost) -> None:
        self._host = host
        self._factories: dict[str, ScreenFactory] = {}
        self._recents = Recents()

    @property
    def recents(self) -> tuple[str, ...]:
        """Executed command lines, most recent first."""
        return tuple(self._recents)

    def register(self, name: str, factory: ScreenFactory) -> None:
        """Register one route; the factory runs only on ``goto``."""
        self._factories[name] = factory

    def goto(self, name: str, **kwargs: Any) -> bool:
        """Open a route by pushing its (freshly built) screen.

        Re-running a route that is already the top screen replaces it, so
        command repetition refreshes instead of stacking. ``False`` (with
        a notify) when the route is unknown.
        """
        factory = self._factories.get(name)
        if factory is None:
            self._host.notify(f"no route: {name}", severity="error")
            return False
        if name == ROOT_ROUTE:
            self._host.pop_to_root()
            return True
        screen = factory(**kwargs)
        try:
            screen.route_name = name  # best-effort identity for goto/replace
        except AttributeError:  # pragma: no cover - exotic screen objects
            pass
        if getattr(self._host.screen, "route_name", None) == name:
            self._host.pop_screen()
        self._host.push_screen(screen)
        return True

    def run_command(self, line: str) -> bool:
        """Parse and execute one command line; record recents on success.

        Unknown names, bad usage and failed validation notify with a
        warning/error and return ``False`` — they are never recorded.
        """
        tokens = line.split()
        if not tokens:
            return False
        name, args = tokens[0], tokens[1:]
        command = COMMANDS.get(name)
        if command is None:
            self._host.notify(
                f"unknown command: {name} (ctrl+p lists the vocabulary)",
                severity="error",
            )
            return False
        kwargs: dict[str, Any] = dict(command.fixed_kwargs)
        if command.arg is not None:
            if len(args) > 1:
                self._host.notify(
                    f"usage: {name} <{command.arg}>", severity="warning")
                return False
            if args:
                value = args[0]
                problem = self._validate_arg(command.arg, value)
                if problem is not None:
                    self._host.notify(problem, severity="warning")
                    return False
                kwargs[command.arg] = value
            elif command.default_arg is not None:
                kwargs[command.arg] = command.default_arg
            else:
                self._host.notify(
                    f"usage: {name} <{command.arg}>", severity="warning")
                return False
        elif args:
            self._host.notify(
                f"{name} takes no arguments", severity="warning")
            return False
        if self.goto(command.route, **kwargs):
            self._recents.record(" ".join(tokens))
            return True
        return False

    def _validate_arg(self, arg: str, value: str) -> str | None:
        """Reject unknown tools and run ids BEFORE any screen is built."""
        if arg == "tool" and value not in WORKBENCH_TOOLS:
            return f"unknown tool: {value} (one of {', '.join(WORKBENCH_TOOLS)})"
        if arg == "run_id":
            frame = self._host.current_frame()
            if frame is None or value not in {r.id for r in frame.runs}:
                return f"no such run: {value}"
        return None

    def completions(self) -> list[tuple[str, str]]:
        """(line, description) pairs for the palette: recents first."""
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for line in self._recents:
            command = COMMANDS.get(line.split()[0])
            if command is None:
                continue  # vocabulary drifted since recording: drop ghosts
            pairs.append((line, command.description))
            seen.add(line)
        for command in COMMANDS.values():
            if command.name not in seen:
                pairs.append((command.name, command.description))
        return pairs


# ── the two entry points (palette + bar) ────────────────────────────────


def _run_and_close(modal: ModalScreen[None], line: str) -> None:
    """Dismiss first, then execute — routed screens never stack on a modal."""
    modal.dismiss(None)
    if line.strip():
        modal.app.router.run_command(line)


class CommandBarScreen(ModalScreen[None]):
    """The ``:`` command line: type a command, Enter runs it, Esc closes."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close the command bar"),
    ]
    CSS = """
    #cmd-bar { align: center middle; }
    #cmd-box {
        width: 76; height: auto;
        border: round $panel-border;
        border-title-color: $panel-title;
        background: $surface;
        padding: 0 1;
    }
    #cmd-hint { color: $text-muted; width: 1fr; }
    #cmd-input { border: none; }
    """

    def compose(self):
        recents = " · ".join(self.app.router.recents[:5])
        with Container(id="cmd-bar"), Container(id="cmd-box"):
            yield Label(
                f"recents: {recents}" if recents else "no recents yet",
                id="cmd-hint",
            )
            yield Input(
                placeholder="runs · feeds · ledger · energy · polymarket · "
                            "sentiment · workbench <tool> · detail <run-id> "
                            "· delete <run-id>",
                id="cmd-input",
            )

    def on_mount(self) -> None:
        self.query_one("#cmd-input", Input).focus()

    def action_close(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        _run_and_close(self, event.value)


class CommandPaletteScreen(ModalScreen[None]):
    """The ⌃P palette: recents first, then the vocabulary; type to filter.

    Up/down move the highlight (the input keeps focus so typing never
    breaks); Enter runs the highlighted line, or the typed text when
    nothing is highlighted; Esc closes.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close the palette"),
        Binding("down", "highlight_next", "Next command"),
        Binding("up", "highlight_prev", "Previous command"),
    ]
    CSS = """
    #palette-bar { align: center middle; }
    #palette-box {
        width: 76; height: auto; max-height: 70%;
        border: round $panel-border;
        border-title-color: $panel-title;
        background: $surface;
        padding: 0 1;
    }
    #palette-input { border: none; }
    #palette-list { height: auto; max-height: 16; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._lines: list[str] = []

    def compose(self):
        with Container(id="palette-bar"), Container(id="palette-box"):
            yield Input(placeholder="type to filter commands…",
                        id="palette-input")
            yield OptionList(id="palette-list")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#palette-input", Input).focus()

    @property
    def _list(self) -> OptionList:
        return self.query_one("#palette-list", OptionList)

    def _populate(self, filter_text: str) -> None:
        needle = filter_text.strip().lower()
        options: list[Option] = []
        self._lines = []
        for line, description in self.app.router.completions():
            if needle and needle not in f"{line} {description}".lower():
                continue
            self._lines.append(line)
            options.append(Option(f"{line} — {description}"))
        option_list = self._list
        option_list.clear_options()
        if options:
            option_list.add_options(options)
            # Highlight the top hit (recents first) so Enter alone runs it.
            option_list.highlighted = 0

    def action_close(self) -> None:
        self.dismiss(None)

    def action_highlight_next(self) -> None:
        self._step_highlight(1)

    def action_highlight_prev(self) -> None:
        self._step_highlight(-1)

    def _step_highlight(self, offset: int) -> None:
        option_list = self._list
        if not self._lines:
            return
        current = option_list.highlighted
        base = 0 if current is None else current
        option_list.highlighted = (base + offset) % len(self._lines)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        highlighted = self._list.highlighted
        if highlighted is not None and highlighted < len(self._lines):
            _run_and_close(self, self._lines[highlighted])
        else:
            _run_and_close(self, event.value)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        index = event.option_index
        if 0 <= index < len(self._lines):
            _run_and_close(self, self._lines[index])
