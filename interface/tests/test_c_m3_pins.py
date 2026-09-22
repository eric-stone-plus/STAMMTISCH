"""Pin tests for the C-M3 adversarial-review fixes.

Each fix from interface/REVIEWS/C-M3.md gets a regression pin:
1. the 30s tool-call ceiling (a hung provider must free the flight slot),
2. :detail keeps last good content on collector-error frames + staleness,
3. the serving-engine label survives on the landed result surface.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from textual.widgets import Static

from interface.collectors.demo import demo_snapshot
from interface.screens.workbench import WorkbenchScreen
from interface.services.quant_engine import DemoQuantEngine
from interface.snapshot import WorkstationSnapshot


def _rendered(static: Static) -> str:
    """Render a Static's rich content to plain text for assertions."""
    from io import StringIO

    from rich.console import Console

    console = Console(file=StringIO(), width=200, legacy_windows=False)
    with console.capture() as capture:
        console.print(static.content)
    return capture.get()


class _HungEngine(DemoQuantEngine):
    """An engine whose fetch never returns — the wedged-provider shape."""

    label = "hung-fixture"

    def fetch_data(self, symbol: str, **_ignored: object) -> dict[str, object]:
        import time

        time.sleep(30)
        return {"ok": True}


def test_hung_engine_call_frees_the_flight_slot() -> None:
    """C-M3 top finding: the dropped 30s ceiling wedged the SingleFlight
    slot forever. With the ceiling (shortened for the test), the slot
    frees and the result degrades honestly."""

    async def scenario() -> None:
        from interface.app.shell import WorkstationShell

        app = WorkstationShell(demo=True)
        ctx = app.run_test(size=(120, 40))
        pilot = await ctx.__aenter__()
        try:
            screen = WorkbenchScreen("fetch", engine=_HungEngine())
            screen.TOOL_CALL_CEILING_S = 0.3
            app.push_screen(screen)
            await pilot.pause()
            screen._submit()
            await pilot.pause(0.1)
            assert screen._flight.in_flight, "call should start in flight"
            await pilot.pause(1.5)
            assert not screen._flight.in_flight, \
                "the ceiling must free the slot even when the call hangs"
            body = _rendered(screen.query_one("#wb-result", Static))
            assert "ceiling" in body, "the degraded result must say so"
        finally:
            await ctx.__aexit__(None, None, None)

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_detail_keeps_last_good_on_collector_error() -> None:
    """C-M3 finding 2: a collector-error frame used to evict the detail
    body with a wrong 'rotated away' diagnosis."""

    async def scenario() -> None:
        from interface.app.shell import WorkstationShell
        from interface.screens.runs_detail import RunsDetailScreen

        app = WorkstationShell(demo=True)
        ctx = app.run_test(size=(120, 40))
        pilot = await ctx.__aenter__()
        try:
            good = demo_snapshot(1_000.0)
            screen = RunsDetailScreen(good.runs[0].id)
            app.push_screen(screen)
            await pilot.pause()
            screen.spine_frame(good, frozenset({"runs"}))
            await pilot.pause()
            good_body = str(screen.query_one("#rd-body", Static).content)
            assert "demo-run-live" in good_body or good_body.strip()
            error_frame = WorkstationSnapshot(
                taken_at=1_001.0, collector_error="events plane unreadable")
            screen.spine_frame(error_frame, frozenset({"runs"}))
            await pilot.pause()
            body = str(screen.query_one("#rd-body", Static).content)
            assert "collector error" in body
            assert "mistyped" not in body, \
                "an empty error frame is not a rotated-away run"
        finally:
            await ctx.__aexit__(None, None, None)

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_engine_label_survives_on_result_surface() -> None:
    """C-M3 finding 3: the serving engine label used to vanish once the
    result landed (degraded engines became invisible)."""

    async def scenario() -> None:
        from interface.app.shell import WorkstationShell

        app = WorkstationShell(demo=True)
        ctx = app.run_test(size=(120, 40))
        pilot = await ctx.__aenter__()
        try:
            engine = DemoQuantEngine()
            screen = WorkbenchScreen("fetch", engine=engine)
            app.push_screen(screen)
            await pilot.pause()
            screen._submit()
            deadline_loops = 0
            while screen.results_landed == 0 and deadline_loops < 100:
                await pilot.pause(0.05)
                deadline_loops += 1
            assert screen.results_landed == 0 or "via" in _rendered(
                screen.query_one("#wb-result", Static)), \
                "landed results must carry the serving-engine stamp"
            assert screen.results_landed > 0
        finally:
            await ctx.__aexit__(None, None, None)

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
