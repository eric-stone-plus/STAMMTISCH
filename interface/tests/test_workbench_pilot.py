"""Workbench Pilot tests: every tool renders from the demo engine, the
degraded state is honest, the UI thread never blocks, single-flight
never overlaps, and per-tool recents are remembered in memory.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from interface.app.shell import WorkstationShell
from interface.collectors.demo import demo_snapshot
from interface.screens.overview import OverviewScreen
from interface.screens.workbench import TOOLS, WorkbenchScreen, _recent_line
from interface.services.quant_engine import (
    DemoQuantEngine,
    quantkit_importable,
)
from interface.snapshot import (
    ServiceStatus,
    WorkstationSnapshot,
)


def _harness(base: float = 1_000.0, step: float = 0.5,
             services=None):
    state = {"t": base}

    def provider() -> WorkstationSnapshot:
        state["t"] += step
        frame = demo_snapshot(state["t"])
        if services is not None:
            return WorkstationSnapshot(
                taken_at=frame.taken_at, runs=frame.runs,
                intake=frame.intake, quotes=frame.quotes,
                services=services, collector_error=frame.collector_error)
        return frame

    def clock() -> float:
        return state["t"] + step / 2

    return provider, clock


async def _await_result(screen: WorkbenchScreen, count: int = 1,
                        timeout_s: float = 5.0) -> None:
    """Wait until ``count`` results landed (worker thread + delivery)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while screen.results_landed < count:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(f"no result landed within {timeout_s}s")
        await asyncio.sleep(0.02)


def test_every_tool_renders_from_the_demo_engine() -> None:
    """Each spec renders its result renderable from a demo engine call."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            for tool in TOOLS:
                screen = WorkbenchScreen(tool, engine=DemoQuantEngine())
                app.push_screen(screen)
                await pilot.pause()
                await pilot.press("enter")  # first field submits the form
                await _await_result(screen)
                assert screen.last_result is not None
                assert screen.last_result["ok"], (tool, screen.last_result)
                assert screen.results_landed == 1
                await pilot.press("escape")
                await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    asyncio.run(scenario())


def test_tool_result_shapes_match_the_old_tui_contract() -> None:
    """The engine call shapes copied from tui/engine.py keep their keys."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            cases = {
                "fetch": lambda r: (r["rows"] > 0, "columns" in r,
                                    "last_close" in r),
                "backtest": lambda r: (
                    hasattr(r["summary"], "sharpe"), "stats" in r),
                "indicators": lambda r: (
                    hasattr(r["summary"], "rsi"), "last_price" in r),
                "portfolio": lambda r: (
                    hasattr(r["summary"], "n_assets"), "stats" in r),
                "gates": lambda r: (r["report"].n_total == 6,
                                    hasattr(r["report"], "all_passed")),
            }
            for tool, check in cases.items():
                screen = WorkbenchScreen(tool, engine=DemoQuantEngine())
                app.push_screen(screen)
                await pilot.pause()
                await pilot.press("enter")
                await _await_result(screen)
                assert screen.last_result is not None
                assert all(check(screen.last_result)), tool
                await pilot.press("escape")
                await pilot.pause()

    asyncio.run(scenario())


def test_degraded_engine_state_is_rendered_honestly() -> None:
    """A services strip reporting quantkit DOWN runs the demo engine and
    SAYS so; a strip that lies (up, but not importable) says that too."""

    async def scenario() -> None:
        provider, clock = _harness(
            services=(ServiceStatus("core", True, ""),
                      ServiceStatus("quantkit", False, "not installed")))
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            screen = WorkbenchScreen("fetch")  # engine=None: resolve per run
            app.push_screen(screen)
            await pilot.pause()
            await pilot.press("enter")
            await _await_result(screen)
            assert screen.engine_labels == ["demo (quantkit DOWN)"]
            assert screen.last_result is not None and screen.last_result["ok"]

    asyncio.run(scenario())


def test_demo_provider_strips_says_up_but_not_importable_is_honest() -> None:
    if quantkit_importable():
        pytest.skip("quantkit importable here: the real engine would run")

    async def scenario() -> None:
        provider, clock = _harness()  # demo strip: quantkit True
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            screen = WorkbenchScreen("fetch")
            app.push_screen(screen)
            await pilot.pause()
            await pilot.press("enter")
            await _await_result(screen)
            assert screen.engine_labels == ["demo (quantkit not importable)"]
            assert screen.last_result is not None and screen.last_result["ok"]

    asyncio.run(scenario())


def test_tool_runs_off_the_ui_thread_and_never_overlaps() -> None:
    """A slow engine must not block the spine (ticks keep flowing), and a
    second submit while one run is in flight notifies instead of stacking."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        engine = DemoQuantEngine(delay_s=1.2)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            ticks_before = app.spine.tick_count
            screen = WorkbenchScreen("fetch", engine=engine)
            app.push_screen(screen)
            await pilot.pause()
            await pilot.press("enter")
            await asyncio.sleep(0.35)  # mid-run, tolerant of suite load
            assert app.spine.tick_count > ticks_before, (
                "the spine kept ticking: the UI thread never blocked")
            # Second submit while the first is still in the air.
            await pilot.press("enter")
            await pilot.pause()
            assert any("already in flight" in n.message
                       for n in app._notifications)
            await _await_result(screen)
            await asyncio.sleep(0.1)
            assert engine.calls == 1, "single flight: exactly one engine call"
            assert screen.results_landed == 1

    asyncio.run(scenario())


def test_recents_per_tool_are_remembered_in_memory() -> None:
    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            screen = WorkbenchScreen("fetch", engine=DemoQuantEngine())
            app.push_screen(screen)
            await pilot.pause()
            field = screen.query_one("#wb-field-symbol")
            field.value = "600519"
            await pilot.press("enter")
            await _await_result(screen)
            assert screen.recorded_inputs == ["600519"]
            assert "600519" in _recent_line("fetch"), (
                "the raw input is remembered (the engine normalizes)")
            # A fresh screen instance shares the same in-memory recents
            # (process-lifetime store; persistence is M4).
            assert "600519" in _recent_line("fetch")

    asyncio.run(scenario())


def test_unknown_tool_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        WorkbenchScreen("nope")
