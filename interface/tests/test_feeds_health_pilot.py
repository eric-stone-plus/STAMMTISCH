"""FeedsHealth Pilot tests: the absorbed FEEDS screen mounts through the
router, renders the counters registry (seeded read-only — nothing live),
marks the cache split, moves with j/k, and pops with Esc."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from interface.app.shell import WorkstationShell
from interface.collectors.demo import demo_snapshot
from interface.screens.feeds_health import FeedsHealthScreen
from interface.services.feeds_health import (
    note_cache,
    reset_stats,
    tracked,
)
from interface.snapshot import WorkstationSnapshot


def _harness():
    state = {"t": 1_000.0}

    def provider() -> WorkstationSnapshot:
        state["t"] += 0.5
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + 0.25

    return provider, clock


async def _await_frame(screen: FeedsHealthScreen, count: int = 1,
                       timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while screen.frames_landed < count:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(f"no health frame landed within {timeout_s}s")
        await asyncio.sleep(0.02)


def test_feeds_screen_mounts_via_the_router_and_renders_counters() -> None:
    reset_stats()
    tracked("tencent", lambda: "batch")
    try:
        tracked("yahoo", lambda: (_ for _ in ()).throw(
            RuntimeError("feed refused")))
    except RuntimeError:
        pass
    note_cache(2, 1)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.run_command("feeds")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FeedsHealthScreen)
            assert screen.route_name == "feeds"
            await _await_frame(screen)
            table = screen.query_one("#fh-table")
            assert table.row_count == 2, "one row per tracked provider"
            assert screen.last_frame is not None
            assert [p.name for p in screen.last_frame.providers] == [
                "tencent", "yahoo"]
            cache = screen.query_one("#fh-cache")
            assert "2 fresh / 1 stale" in str(cache.render())
            # j moves the cursor (every binding carries a description).
            table.focus()
            row_before = table.cursor_row
            await pilot.press("j")
            assert table.cursor_row == row_before + 1
            await pilot.press("k")
            assert table.cursor_row == row_before
            # Esc pops back to the wall.
            await pilot.press("escape")
            await pilot.pause()
            assert screen not in app.screen_stack

    asyncio.run(scenario())


def test_feeds_screen_is_honest_when_nothing_has_served_yet() -> None:
    reset_stats()
    note_cache(0, 0)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.goto("feeds")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FeedsHealthScreen)
            await _await_frame(screen)
            assert screen.last_frame is not None
            assert screen.last_frame.providers == ()
            table = screen.query_one("#fh-table")
            assert table.row_count == 1, "the honest no-providers row"
            assert "no provider has served" in " ".join(
                str(cell) for cell in table.get_row_at(0))
            cache = screen.query_one("#fh-cache")
            assert "0 fresh / 0 stale" in str(cache.render())

    asyncio.run(scenario())
