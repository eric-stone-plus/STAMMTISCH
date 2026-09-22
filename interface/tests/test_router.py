"""Router tests: registry laziness, goto/esc stack discipline, the shared
command vocabulary (bar + palette), MRU recents, and refusal recording.

The pure section drives :class:`~interface.app.router.Router` against a
fake host (no app needed); the Pilot section drives the real bindings.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from interface.app.router import (
    COMMANDS,
    ROOT_ROUTE,
    WORKBENCH_TOOLS,
    Router,
)
from interface.app.shell import WorkstationShell
from interface.collectors.demo import demo_snapshot
from interface.render import panels
from interface.screens.feeds_health import FeedsHealthScreen
from interface.screens.overview import OverviewScreen
from interface.screens.runs_detail import RunsDetailScreen
from interface.screens.workbench import TOOLS, WorkbenchScreen
from interface.snapshot import WorkstationSnapshot

# ── pure: registry, goto, commands, recents ─────────────────────────────


class FakeHost:
    """The RouterHost protocol, recording instead of navigating."""

    def __init__(self, frame: WorkstationSnapshot | None = None) -> None:
        self.pushed: list[object] = []
        self.popped = 0
        self.rooted = 0
        self.messages: list[str] = []
        self._screen: object = SimpleNamespace(route_name=None)
        self._frame = frame

    def push_screen(self, screen: object) -> None:
        self.pushed.append(screen)
        self._screen = screen

    def pop_screen(self) -> None:
        self.popped += 1

    def pop_to_root(self) -> None:
        self.rooted += 1
        self._screen = SimpleNamespace(route_name=None)

    def notify(self, message: str, **kwargs: object) -> None:
        self.messages.append(message)

    @property
    def screen(self) -> object:
        return self._screen

    def current_frame(self) -> WorkstationSnapshot | None:
        return self._frame


def _router(frame: WorkstationSnapshot | None = None) -> tuple[Router, FakeHost]:
    host = FakeHost(frame)
    router = Router(host)
    built: list[dict] = []

    def lazy_screen(**kwargs: dict) -> SimpleNamespace:
        built.append(kwargs)
        return SimpleNamespace(route_name=None, kwargs=kwargs)

    router.register("workbench", lazy_screen)
    router.register("detail", lazy_screen)
    router.register("confirm_delete", lazy_screen)
    router.register(ROOT_ROUTE, lazy_screen)
    router._built = built  # type: ignore[attr-defined]
    return router, host


def test_factories_are_lazy_and_run_once_per_goto() -> None:
    router, _ = _router()
    assert router._built == [], "registration builds nothing"
    assert router.goto("workbench", tool="gates")
    assert router._built == [{"tool": "gates"}], "built exactly once"


def test_goto_unknown_route_notifies_and_fails() -> None:
    router, host = _router()
    assert router.goto("nowhere") is False
    assert host.pushed == [] and host.messages


def test_goto_root_pops_instead_of_pushing() -> None:
    router, host = _router()
    assert router.goto("workbench", tool="fetch")
    assert host.pushed and host.rooted == 0
    assert router.goto(ROOT_ROUTE)
    assert host.rooted == 1 and len(host.pushed) == 1


def test_goto_replaces_the_top_same_route_screen() -> None:
    router, host = _router()
    router.goto("workbench", tool="fetch")
    router.goto("workbench", tool="gates")
    assert len(host.pushed) == 2
    assert host.popped == 1, "second goto replaced, not stacked"
    router.goto("workbench", tool="indicators")
    assert router._built[-1] == {"tool": "indicators"}


def test_vocabulary_covers_every_workbench_tool() -> None:
    assert set(WORKBENCH_TOOLS) == set(TOOLS)
    for tool in WORKBENCH_TOOLS:
        assert tool in COMMANDS
    assert COMMANDS["detail"].arg == "run_id"


def test_run_command_routes_and_records() -> None:
    router, _ = _router()
    assert router.run_command("backtest")
    assert router._built == [{"tool": "backtest"}]
    assert router.recents == ("backtest",)
    assert router.run_command("workbench gates")
    assert router._built[-1] == {"tool": "gates"}
    assert router.run_command("workbench")  # bare form defaults to fetch
    assert router._built[-1] == {"tool": "fetch"}
    assert router.recents == ("workbench", "workbench gates", "backtest")


def test_unknown_command_never_records() -> None:
    router, host = _router()
    assert router.run_command("definitely-not-a-command") is False
    assert host.pushed == []
    assert any("unknown command" in m for m in host.messages)
    assert router.recents == ()


def test_bad_usage_and_validation_never_record() -> None:
    router, host = _router()
    assert router.run_command("workbench nonsense") is False
    assert any("unknown tool" in m for m in host.messages)
    assert router.run_command("runs extra") is False
    assert any("takes no arguments" in m for m in host.messages)
    assert router.run_command("detail") is False
    assert any("usage: detail" in m for m in host.messages)
    assert router.recents == () and host.pushed == []


def test_detail_requires_a_run_in_the_current_frame() -> None:
    router, host = _router(demo_snapshot(1_000.0))
    assert router.run_command("detail nope-nope") is False
    assert any("no such run" in m for m in host.messages)
    assert router.recents == ()
    assert router.run_command("detail demo-run-live")
    assert router._built == [{"run_id": "demo-run-live"}]
    assert router.recents == ("detail demo-run-live",)


# ── the one write command: :delete ────────────────────────────────────


def test_delete_unknown_id_never_opens_the_dialog() -> None:
    """The id is validated against the last frame BEFORE any dialog is
    built: unknown id -> notify, no push, no recents."""
    router, host = _router(demo_snapshot(1_000.0))
    assert router.run_command("delete nope-nope") is False
    assert any("no such run" in m for m in host.messages)
    assert host.pushed == []
    assert router._built == []
    assert router.recents == ()


def test_delete_known_id_routes_the_confirm_dialog() -> None:
    router, host = _router(demo_snapshot(1_000.0))
    assert router.run_command("delete demo-run-live")
    assert router._built == [{"run_id": "demo-run-live"}], (
        "the dialog factory receives exactly the validated run id")
    assert len(host.pushed) == 1
    assert router.recents == ("delete demo-run-live",)
    assert router.run_command("delete") is False, "usage: id required"
    assert any("usage: delete" in m for m in host.messages)
    assert router.run_command("delete a b") is False, "one id only"
    assert router.recents == ("delete demo-run-live",), (
        "failed usage never records")


def test_delete_in_the_shared_vocabulary_listing() -> None:
    assert "delete" in COMMANDS
    assert COMMANDS["delete"].route == "confirm_delete"
    assert COMMANDS["delete"].arg == "run_id"
    router, _ = _router()
    lines = [line for line, _ in router.completions()]
    assert "delete" in lines, "the palette lists the write command"


# ── the M6 absorbed read-only screens: :feeds :ledger :energy
#    :polymarket :sentiment ────────────────────────────────────────────

M6_COMMANDS = ("feeds", "ledger", "energy", "polymarket", "sentiment")


def test_m6_commands_exist_with_descriptions_and_routes() -> None:
    for name in M6_COMMANDS:
        command = COMMANDS[name]
        assert command.route == name
        assert command.arg is None, "read-only screens take no arguments"
        assert len(command.description) > 10, (
            f"{name} carries a palette description")


def test_m6_commands_route_and_record_recents() -> None:
    router, host = _router()
    for name in M6_COMMANDS:
        router.register(name, lambda **kw: SimpleNamespace(route_name=None))
    for name in M6_COMMANDS:
        assert router.run_command(name), name
    assert router.recents == tuple(reversed(M6_COMMANDS)), (
        "MRU order, most recent first")
    assert len(host.pushed) == len(M6_COMMANDS)


def test_m6_commands_reject_arguments() -> None:
    router, host = _router()
    assert router.run_command("feeds now") is False
    assert any("takes no arguments" in m for m in host.messages)
    assert router.recents == (), "bad usage never records"


def test_m6_commands_listed_in_the_palette() -> None:
    router, _ = _router()
    lines = [line for line, _ in router.completions()]
    for name in M6_COMMANDS:
        assert name in lines


def test_command_bar_opens_an_absorbed_screen_end_to_end() -> None:
    """``:feeds`` through the bar: the offline-safe absorbed screen mounts
    on the real stack and Esc pops back to the wall."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.press(":")
            await pilot.press(*"feeds")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, FeedsHealthScreen)
            assert app.router.recents[0] == "feeds"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    asyncio.run(scenario())


def test_recents_are_mru_deduped_and_capped() -> None:
    router, _ = _router()
    for index in range(10):
        router._recents.record(f"cmd-{index}")
    assert len(router.recents) == 8, "capacity 8"
    assert router.recents[0] == "cmd-9", "most recent first"
    router._recents.record("cmd-9")
    assert router.recents.count("cmd-9") == 1, "dedupe, never twice"
    assert router.recents[0] == "cmd-9", "recording moves to front"


def test_completions_put_recents_first_then_vocabulary() -> None:
    router, _ = _router()
    router.run_command("backtest")
    lines = [line for line, _ in router.completions()]
    assert lines[0] == "backtest"
    assert "runs" in lines and "detail" in lines
    assert len(lines) == len(COMMANDS), "recents dedupe against vocabulary"


# ── Pilot: bar + palette drive the real app ────────────────────────────


def _harness(base: float = 1_000.0, step: float = 0.5):
    state = {"t": base}

    def provider() -> WorkstationSnapshot:
        state["t"] += step
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + step / 2

    return provider, clock


def test_command_bar_and_palette_share_one_vocabulary() -> None:
    """``:`` and ⌃P both land in run_command: same routes, same recents."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            # The ``:`` bar routes a workbench tool.
            await pilot.press(":")
            await pilot.pause()
            from interface.app.router import CommandBarScreen
            assert isinstance(app.screen, CommandBarScreen)
            await pilot.press(*"backtest")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, WorkbenchScreen)
            assert app.screen.spec.tool == "backtest"
            assert app.screen.route_name == "workbench"
            assert len(app.screen_stack) == 2, "push, not switch"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            assert len(app.screen_stack) == 1, "esc pops to the wall"
            # The palette routes the same vocabulary.
            await pilot.press("ctrl+p")
            await pilot.pause()
            from interface.app.router import CommandPaletteScreen
            assert isinstance(app.screen, CommandPaletteScreen)
            await pilot.press(*"gates")
            await pilot.pause()
            palette = app.screen
            assert isinstance(palette, CommandPaletteScreen)
            assert palette._lines and palette._lines[0] == "gates", (
                "filtered to the gates command")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, WorkbenchScreen)
            assert app.screen.spec.tool == "gates"
            # ONE recents list shared by both entry points, MRU order.
            assert app.router.recents[0] == "gates"
            assert "backtest" in app.router.recents
            # ``runs`` pops the stack to the root instead of pushing (back
            # to the wall first: a focused workbench Input owns keys).
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press(":")
            await pilot.press(*"runs")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            assert len(app.screen_stack) == 1

    asyncio.run(scenario())


def test_palette_navigation_moves_the_highlight_with_wraparound() -> None:
    """Up/down walk the palette's option list cyclically (typing keeps
    focus in the input); Enter runs whatever is highlighted."""

    async def scenario() -> None:
        from interface.app.router import CommandPaletteScreen

        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            await pilot.press("ctrl+p")
            await pilot.pause()
            palette = app.screen
            assert isinstance(palette, CommandPaletteScreen)
            assert palette._lines[0] == "runs", "recents empty: vocab first"
            n = len(palette._lines)
            assert palette._list.highlighted == 0
            await pilot.press("down")
            assert palette._list.highlighted == 1
            await pilot.press("up")
            assert palette._list.highlighted == 0
            await pilot.press("up")
            assert palette._list.highlighted == n - 1, "wraps upward"
            await pilot.press("down")
            assert palette._list.highlighted == 0, "wraps downward"

    asyncio.run(scenario())


def test_unknown_command_from_the_bar_notifies_and_stays_put() -> None:
    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            await pilot.press(":")
            await pilot.press(*"wibble")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            assert any("unknown command" in n.message
                       for n in app._notifications)
            assert app.router.recents == ()

    asyncio.run(scenario())


def test_detail_command_opens_the_full_screen() -> None:
    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            await pilot.press(":")
            await pilot.press(*"detail demo-run-live")
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunsDetailScreen)
            assert screen.run_id == "demo-run-live"
            await asyncio.sleep(0.2)  # a streaming frame re-renders it
            await pilot.pause()
            assert screen.update_count >= 1
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            # The corrupt run (S4): error prominent, stages never faked.
            await pilot.press(":")
            await pilot.press(*"detail demo-run-corrupt")
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunsDetailScreen)
            await asyncio.sleep(0.2)
            await pilot.pause()
            assert screen.last_run is not None
            assert screen.last_run.state == "corrupt"
            import io

            from rich.console import Console
            buffer = io.StringIO()
            Console(file=buffer, width=140, no_color=True).print(
                panels.runs_detail_body(screen.last_run))
            plain = buffer.getvalue()
            assert "error:" in plain and "digest mismatch" in plain
            assert "(no stages)" in plain
            # Unknown run id: refused before any screen is pushed.
            await pilot.press("escape")
            await pilot.press(":")
            await pilot.press(*"detail no-such-run")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            assert any("no such run" in n.message for n in app._notifications)

    asyncio.run(scenario())
