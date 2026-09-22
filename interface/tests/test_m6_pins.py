"""Pin tests for the D-M6 self-reported fixes (previously unpinned).

Review D (interface/REVIEWS/D-M6-standards.md finding 1): the M6
self-reported UI fixes were real in code but pinned by no shipped test
(the README states them as contract). These pilots drive the three
spine-live screens through a SECOND spine-delivered frame — no manual
refresh key, the ×15 services-due wiring itself re-fetches — and pin:

1. the table cursor survives the slow-lane repaint, re-anchored to the
   SAME ROW KEY (feeds_health / energy / polymarket), with the second
   frame carrying genuinely different data (a stale no-op would not
   pass);
2. the polymarket ``_autofocused`` latch: a later slow-lane
   re-delivery never steals focus back from the filter input;
3. the staleness badge (extracted to render/stale.py by this round)
   mirrors the spine's services level into each screen's border title.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from textual.containers import Vertical
from textual.widgets import DataTable, Input

from interface.app.shell import WorkstationShell
from interface.collectors.demo import demo_snapshot
from interface.render.stale import stale_badged_title
from interface.screens.energy import EnergyScreen
from interface.screens.feeds_health import FeedsHealthScreen
from interface.screens.polymarket import PolymarketScreen
from interface.services.energy import EnergyFrame, SeriesRow
from interface.services.feeds_health import (
    FeedHealthService,
    note_cache,
    reset_stats,
    tracked,
)
from interface.services.polymarket import MarketRow, MarketTape
from interface.snapshot import WorkstationSnapshot


def _harness():
    state = {"t": 1_000.0}

    def provider() -> WorkstationSnapshot:
        state["t"] += 0.5
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + 0.25

    return provider, clock


async def _await_frame(screen, count: int = 2, timeout_s: float = 8.0) -> None:
    """Wait until the screen landed ``count`` slow-lane frames."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while screen.frames_landed < count:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(f"only {screen.frames_landed}/{count} frames "
                        f"landed within {timeout_s}s")
        await asyncio.sleep(0.02)


def _second_frame_gate() -> dict:
    """Block the SECOND service call until the test releases it.

    The spine polls services every 15 ticks (0.75s at interval 0.05);
    without the gate, frame 2 can land while the test is still asserting
    on frame 1. The gate pins WHICH data frame 2 carries, so the cursor
    assertions see a genuine clear/re-add/re-anchor with different rows.
    """
    return {"gate": threading.Event(), "calls": 0}


def _assert_badge_mirrors_into_border(screen, wrap_id: str,
                                      base_title: str) -> None:
    """The spine's services level -> the shared badge -> the border."""
    screen.spine_staleness({"services": "warn"})
    title = str(screen.query_one(f"#{wrap_id}", Vertical).border_title)
    assert base_title in title and "stale?" in title
    screen.spine_staleness({"services": "crit"})
    title = str(screen.query_one(f"#{wrap_id}", Vertical).border_title)
    assert "STALE" in title
    screen.spine_staleness({"services": "fresh"})
    title = str(screen.query_one(f"#{wrap_id}", Vertical).border_title)
    assert base_title in title and "STALE" not in title and (
        "stale?" not in title)


def test_stale_badged_title_levels() -> None:
    """The extracted badge helper: one token-driven mapping, closed
    vocabulary (unknown levels render fresh)."""
    fresh = stale_badged_title("PROVIDERS", "fresh")
    assert str(fresh) == "PROVIDERS"
    assert "stale?" in str(stale_badged_title("PROVIDERS", "warn"))
    crit = str(stale_badged_title("MARKETS", "crit"))
    assert "STALE" in crit
    assert str(stale_badged_title("WATCHLIST", "nonsense")) == "WATCHLIST"


def test_feeds_cursor_survives_second_spine_frame(monkeypatch) -> None:
    """README:121 "Table cursors survive every repaint" — pinned by a
    real SECOND spine-delivered frame (the on-mount refresh is frame 1;
    the spine's services-due poll is frame 2), not by a helper call."""
    reset_stats()
    tracked("alpha", lambda: "batch")
    tracked("beta", lambda: "batch")
    note_cache(2, 1)
    state = _second_frame_gate()

    class _GatedService:
        """The screen resolves its service lazily from the module, so a
        gated replacement rides the REAL call-time seam."""

        def frame(self):
            state["calls"] += 1
            if state["calls"] > 1:
                state["gate"].wait(10.0)
            return FeedHealthService().frame()

    monkeypatch.setattr("interface.services.feeds_health.FeedHealthService",
                        _GatedService)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.run_command("feeds")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FeedsHealthScreen)
            await _await_frame(screen, count=1)
            table = screen.query_one("#fh-table", DataTable)
            table.focus()
            await pilot.press("j")
            await pilot.pause()
            anchor_row = table.cursor_row
            assert anchor_row == 1
            assert str(table.get_row_at(anchor_row)[0]) == "beta"
            assert str(table.get_row_at(0)[1]) == "1", "alpha ok=1"
            # Bump a counter BETWEEN frames: frame 2 must carry different
            # data (proves a real re-poll + repaint, not a cached no-op).
            tracked("alpha", lambda: "batch")
            state["gate"].set()
            await _await_frame(screen, count=2)
            await pilot.pause()
            assert table.cursor_row == anchor_row, (
                "the cursor row survives the slow-lane repaint")
            assert str(table.get_row_at(anchor_row)[0]) == "beta", (
                "re-anchored to the same row KEY (provider name)")
            assert str(table.get_row_at(0)[1]) == "2", (
                "frame 2 really re-rendered (alpha ok 1 -> 2)")
            _assert_badge_mirrors_into_border(screen, "fh-table-wrap",
                                              "PROVIDERS")

    try:
        asyncio.run(scenario())
    finally:
        # The registry + lane cache are module-global; leave them at zero
        # for whichever module-level test runs next.
        reset_stats()
        note_cache(0, 0)


def test_energy_cursor_survives_second_spine_frame(monkeypatch) -> None:
    """Same pin, energy edition: frame 2 changes a value AND appends a
    row, so only a true clear/re-add/re-anchor keeps the cursor on
    ``brent_spot``."""
    def _row(key, label, value, **kw) -> SeriesRow:
        base = {"key": key, "group": "CRUDE", "label": label,
                "unit": "$/bbl", "decimals": 2, "frequency": "daily",
                "period": "2026-09-21", "value": value,
                "change": kw.pop("change", 1.0), "route": "r",
                "history": (("2026-09-21", value),
                            ("2026-09-20", value - 1.0)),
                "description": "desc"}
        base.update(kw)
        return SeriesRow(**base)

    err_row = SeriesRow(key="henry_hub_spot", group="GAS",
                        label="Henry Hub spot", unit="$/MMBtu",
                        decimals=2, frequency="daily", route="r",
                        error="configured proxy is unavailable")
    frames = [
        EnergyFrame(ok=True, rows=(
            _row("wti_spot", "WTI Cushing spot", 71.5),
            _row("brent_spot", "Brent spot", 75.25, change=-0.5),
            err_row), via="configured proxy"),
        EnergyFrame(ok=True, rows=(
            _row("wti_spot", "WTI Cushing spot", 72.0),  # value moved
            _row("brent_spot", "Brent spot", 75.25, change=-0.5),
            err_row,
            _row("us_crude_stocks", "US crude stocks ex-SPR", 321.0)),
            via="configured proxy"),  # ...and a series appended
    ]
    state = _second_frame_gate()

    def fake_fetch(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            return frames[0]
        state["gate"].wait(10.0)
        return frames[1]

    monkeypatch.setattr("interface.services.energy.fetch_watchlist",
                        fake_fetch)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.run_command("energy")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, EnergyScreen)
            await _await_frame(screen, count=1)
            table = screen.query_one("#eg-table", DataTable)
            table.focus()
            await pilot.press("j")
            await pilot.pause()
            anchor_row = table.cursor_row
            assert anchor_row == 1
            assert "Brent spot" in str(
                screen.query_one("#eg-detail").render())
            assert str(table.get_row_at(0)[3]) == "71.50"
            state["gate"].set()
            await _await_frame(screen, count=2)
            await pilot.pause()
            assert table.row_count == 4, "frame 2 appended a series"
            assert str(table.get_row_at(0)[3]) == "72.00", (
                "frame 2 really re-rendered (WTI 71.50 -> 72.00)")
            assert table.cursor_row == anchor_row
            assert "Brent spot" in str(
                screen.query_one("#eg-detail").render()), (
                "cursor re-anchored to the same row KEY (brent_spot): "
                "the detail pane still follows the highlighted row")
            _assert_badge_mirrors_into_border(screen, "eg-table-wrap",
                                              "WATCHLIST")

    asyncio.run(scenario())


def test_polymarket_cursor_survives_second_spine_frame(monkeypatch) -> None:
    """Same pin, polymarket edition."""
    def _row(id_, question, slug, yes) -> MarketRow:
        return MarketRow(id=id_, question=question, slug=slug, yes=yes,
                         volume24hr=1_234_567.0, volume=None,
                         end="2026-12-31", fee_rate=0.02, category="Politics")

    bill_a = _row("7", "Will the bill pass by March?", "bill-march", 0.65)
    bill_b = _row("7", "Will the bill pass by March?", "bill-march", 0.70)
    rocket = _row("8", "Will the rocket reach orbit?", "rocket-orbit", 0.42)
    summit = _row("9", "Will the summit end early?", "summit-early", 0.10)
    extra = _row("10", "Will the fix land today?", "fix-lands", 0.90)
    frames = [
        MarketTape(ok=True, markets=(bill_a, rocket, summit),
                   via="configured proxy"),
        MarketTape(ok=True, markets=(bill_b, rocket, summit, extra),
                   via="configured proxy"),
    ]
    state = _second_frame_gate()

    def fake_fetch(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            return frames[0]
        state["gate"].wait(10.0)
        return frames[1]

    monkeypatch.setattr("interface.services.polymarket.fetch_markets",
                        fake_fetch)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.run_command("polymarket")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, PolymarketScreen)
            await _await_frame(screen, count=1)
            table = screen.query_one("#pm-table", DataTable)
            table.focus()
            await pilot.press("j")
            await pilot.pause()
            anchor_row = table.cursor_row
            assert anchor_row == 1
            assert "Will the rocket reach orbit?" in str(
                screen.query_one("#pm-detail").render())
            assert "65.0%" in str(table.get_row_at(0)[0])
            state["gate"].set()
            await _await_frame(screen, count=2)
            await pilot.pause()
            assert table.row_count == 4, "frame 2 appended a market"
            assert "70.0%" in str(table.get_row_at(0)[0]), (
                "frame 2 really re-rendered (bill 65% -> 70%)")
            assert table.cursor_row == anchor_row
            assert "Will the rocket reach orbit?" in str(
                screen.query_one("#pm-detail").render()), (
                "cursor re-anchored to the same row KEY (id 8)")
            _assert_badge_mirrors_into_border(screen, "pm-table-wrap",
                                              "MARKETS")

    asyncio.run(scenario())


def test_polymarket_redelivery_never_steals_filter_focus(
        monkeypatch) -> None:
    """The ``_autofocused`` latch (screens/polymarket.py): the table
    auto-focuses once, on the FIRST landed tape; a later slow-lane
    re-delivery must never steal focus back from the filter input."""
    def _row(id_, question, slug, yes) -> MarketRow:
        return MarketRow(id=id_, question=question, slug=slug, yes=yes,
                         volume24hr=1_234_567.0, volume=None,
                         end="2026-12-31", fee_rate=0.02, category="Politics")

    tape = MarketTape(
        ok=True,
        markets=(
            _row("7", "Will the bill pass by March?", "bill-march", 0.65),
            _row("8", "Will the rocket reach orbit?", "rocket-orbit", 0.42),
            _row("9", "Will the summit end early?", "summit-early", 0.10),
        ),
        via="configured proxy",
    )
    monkeypatch.setattr("interface.services.polymarket.fetch_markets",
                        lambda *a, **kw: tape)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.run_command("polymarket")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, PolymarketScreen)
            await _await_frame(screen, count=1)
            table = screen.query_one("#pm-table", DataTable)
            assert screen.focused is table, (
                "the FIRST landed tape auto-focuses the table")
            assert screen._autofocused
            # The operator moves to the filter and types into it.
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press(*"rocket")
            await pilot.pause()
            filter_input = screen.query_one("#pm-filter", Input)
            assert screen.focused is filter_input
            assert filter_input.value == "rocket"
            # A later spine-driven re-delivery repaints the tape...
            await _await_frame(screen, count=2)
            await pilot.pause()
            assert screen.frames_landed >= 2, "a re-delivery really landed"
            # ...without stealing focus from the filter.
            assert screen.focused is filter_input, (
                "later re-deliveries never steal focus back")
            assert filter_input.value == "rocket", (
                "screen-side view state survives the re-delivery")
            assert table.row_count == 1, "the filter still narrows the tape"

    asyncio.run(scenario())
