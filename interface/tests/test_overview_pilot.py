"""Overview wall under a headless Pilot (asyncio.run pattern; no
pytest-asyncio). The demo provider is wrapped with a stepping clock so
frames are deterministic per call while still streaming; the spine clock
tracks that same virtual time so staleness stays "fresh"."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from textual.widgets import DataTable

from interface.app.shell import WorkstationShell
from interface.collectors.demo import demo_snapshot
from interface.screens.overview import OverviewScreen, RunDetailScreen
from interface.snapshot import WorkstationSnapshot


def _harness(base: float = 1_000.0, step: float = 0.5):
    """A stepped demo provider plus a matching spine clock."""
    state = {"t": base}

    def provider() -> WorkstationSnapshot:
        state["t"] += step
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + step / 2  # age stays half a step: fresh

    return provider, clock


def test_overview_wall_mounts_and_streams() -> None:
    """1+ ticks: rows == snapshot runs, feed alive, glance/src, services."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.8)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            table = screen.query_one("#runs", DataTable)
            assert table.row_count == len(screen.frame.runs) == 3
            assert screen.feed_written >= 1, "activity feed received events"
            assert "STAMMTISCH" in screen.banner_plain
            assert "src" in screen.glance_plain, "provenance must survive"
            assert "cached" in screen.glance_plain
            assert "DOWN" in screen.services_plain, "ai down at t~1000"
            assert "stale-crit" not in table.classes, "panels stay fresh"

    asyncio.run(scenario())


def test_runs_table_cursor_survives_refreshes() -> None:
    """Stable row keys: cursor row id is unchanged across many frames."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            table = screen.query_one("#runs", DataTable)
            table.move_cursor(row=2)
            before, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            await asyncio.sleep(0.5)
            await pilot.pause()
            after, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            assert (before.value, after.value) == ("demo-run-corrupt",
                                                   "demo-run-corrupt")
            assert table.row_count == 3, "no clear+re-add churn"

    asyncio.run(scenario())


def test_enter_opens_run_detail_and_escape_closes() -> None:
    """Enter on the cursor row opens the modal; escape returns to the wall."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)
            assert app.screen.run.id == "demo-run-live", "cursor row 0"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    asyncio.run(scenario())
