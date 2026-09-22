"""Verb grammar tests: the pure RunsView plus Pilot-driven binding tests.

The Pilot section drives the real bindings on the demo provider
(stepped clock harness, same pattern as test_overview_pilot); the pure
section pins the state machine without Textual.
"""

from __future__ import annotations

import asyncio
import io

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from rich.console import Console
from textual.widgets import DataTable

from interface.app.shell import WorkstationShell
from interface.app.verbs import DEFAULT_SORT, SORT_MODES, RunsView, run_search_text
from interface.collectors.demo import demo_snapshot
from interface.render import panels
from interface.screens.overview import EventsModalScreen, OverviewScreen
from interface.snapshot import (
    CostSnapshot,
    RunSnapshot,
    StageUsage,
    WorkstationSnapshot,
)

# ── pure state machine ──────────────────────────────────────────────────


def _runs() -> tuple[RunSnapshot, ...]:
    return (
        RunSnapshot(id="run-b", pipeline_id="p.momentum", state="running",
                    created_at="2026-09-22T10:00:00Z"),
        RunSnapshot(id="run-a", pipeline_id="p.meanrev", state="completed",
                    created_at="2026-09-22T11:00:00Z"),
        RunSnapshot(id="run-c", pipeline_id="?", state="corrupt",
                    created_at="2026-09-21T09:00:00Z"),
    )


def test_invalid_regex_keeps_previous_filter() -> None:
    view = RunsView()
    assert view.set_filter("live") is None
    error = view.set_filter("(")
    assert error is not None and "invalid regex" in error
    assert view.filter_pattern == "live", "previous filter stays active"


def test_clear_filter_restores_every_row() -> None:
    view = RunsView()
    view.set_filter("corrupt")
    assert [r.id for r in view.visible_runs(_runs())] == ["run-c"]
    view.clear_filter()
    assert len(view.visible_runs(_runs())) == 3


def test_filter_covers_id_pipeline_state_and_flags() -> None:
    view = RunsView()
    view.set_filter("momentum")
    assert [r.id for r in view.visible_runs(_runs())] == ["run-b"]
    view.set_filter("RUN-A")  # case-insensitive over the id
    assert [r.id for r in view.visible_runs(_runs())] == ["run-a"]
    # The flags cell: only the running row carries an active "R" followed
    # by the dim placeholder dot, so "R\." isolates the flags dimension.
    view.set_filter("R\\.")
    assert [r.id for r in view.visible_runs(_runs())] == ["run-b"]
    assert "R" in run_search_text(_runs()[0])


def test_sort_cycle_and_modes() -> None:
    view = RunsView()
    assert view.sort_mode == DEFAULT_SORT == "state-group"
    assert SORT_MODES == ("id", "created", "state-group")
    assert view.cycle_sort() == "id"
    assert [r.id for r in view.ordered_runs(_runs())] == [
        "run-a", "run-b", "run-c"]
    assert view.cycle_sort() == "created"
    assert [r.id for r in view.ordered_runs(_runs())] == [
        "run-a", "run-b", "run-c"]  # newest created_at first
    assert view.cycle_sort() == "state-group"
    assert [r.id for r in view.ordered_runs(_runs())] == [
        "run-b", "run-a", "run-c"]  # live, then terminal+corrupt (frame order)


def test_step_match_wraps_both_directions() -> None:
    view = RunsView()
    keys = ["a", "b", "c"]
    assert view.step_match(keys, "a") == "b"
    assert view.step_match(keys, "c") == "a", "wraps forward"
    assert view.step_match(keys, "c", backward=True) == "b"
    assert view.step_match(keys, "a", backward=True) == "c", "wraps backward"
    assert view.step_match(keys, None) == "a"
    assert view.step_match(keys, "gone") == "a"
    assert view.step_match([], "a") is None


def test_follow_latch_rules() -> None:
    """S3 in the pure core: latch survives hide, drops only on departure,
    and a clamped cursor never re-latches the latch."""
    view = RunsView()
    assert view.toggle_follow("run-b") is True
    assert view.followed_id == "run-b"
    # Hidden (filtered out) but still in the snapshot: latch survives.
    view.retain_followed({"run-a", "run-b", "run-c"})
    assert view.followed_id == "run-b"
    # anchor ignores the latch while hidden, falls back to the cursor.
    assert view.anchor_key("run-a", ["run-a", "run-c"]) == "run-a"
    assert view.anchor_key("run-gone", ["run-a", "run-c"]) is None
    assert view.followed_id == "run-b", "clamped cursor never re-latches"
    # The row returns: the latch wins again.
    assert view.anchor_key("run-a", ["run-a", "run-b"]) == "run-b"
    # Gone from the snapshot entirely: latch drops.
    view.retain_followed({"run-a"})
    assert view.followed_id is None
    # Toggle off.
    assert view.toggle_follow("run-a") is True
    assert view.toggle_follow("run-a") is False
    assert view.toggle_follow(None) is False


def _plain(renderable) -> str:
    """Render one rich renderable to plain text (width-stable)."""
    buffer = io.StringIO()
    Console(file=buffer, width=140, no_color=True).print(renderable)
    return buffer.getvalue()


def test_masked_summary_and_honest_cost_rendering() -> None:
    """The ``c`` line is snapshot-only and masked; the detail body never
    invents numbers for legal nulls (grilling adjudication 1) and never
    fakes stages for a corrupt run (S4)."""
    run = RunSnapshot(
        id="run-x", pipeline_id="p", state="failed",
        created_at="2026-09-22T10:00:00Z",
        error="/home/who/where/events.jsonl: line 3: unparseable",
        cost=CostSnapshot(total=0.0, usage=(
            StageUsage("ingest", tokens_in=None, tokens_out=None,
                       tokens_total=None, wall_s=None),
            StageUsage("review", tokens_in=10, tokens_out=5,
                       tokens_total=15, wall_s=2.5),
        )),
    )
    line = panels.run_summary_line(run)
    assert line.startswith("run run-x p failed gates ")
    assert "tokens —" in line, "unknown tokens render the em dash"
    assert "events.jsonl" not in line and "home" not in line, "masked"
    assert "[error]" in line
    body = _plain(panels.runs_detail_body(run))
    assert "error:" in body
    assert "(no stages)" in body, "corrupt runs never fake stages"
    assert "unit tokens" in body
    assert "15" in body and "2.5" in body, "known usage rows render"
    ingest_row = next(line_ for line_ in body.splitlines()
                      if "ingest" in line_)
    assert "—" in ingest_row, "null usage row renders dashes"


def test_events_body_full_tail() -> None:
    frame = demo_snapshot(1_000.0)
    live = frame.runs[0]
    body = _plain(panels.run_events_body(live))
    assert "run.staged" in body
    assert "stage.gate_failed" in body
    assert panels.run_events_body(frame.runs[2]).plain == "(no events)"


# ── Pilot: the real bindings on the demo wall ──────────────────────────


def _harness(base: float = 1_000.0, step: float = 0.5):
    """A stepped demo provider plus a matching spine clock."""
    state = {"t": base}

    def provider() -> WorkstationSnapshot:
        state["t"] += step
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + step / 2  # age stays half a step: fresh

    return provider, clock


def _messages(app) -> list[str]:
    return [n.message for n in app._notifications]


def test_filter_hides_rows_across_refreshes() -> None:
    """``/`` applies, hides survive >= 2 runs-panel refreshes, esc clears."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            table = screen.query_one("#runs", DataTable)
            assert table.row_count == 3
            await pilot.press("/")
            await pilot.press(*"momentum")
            await pilot.press("enter")
            await pilot.pause()
            assert table.row_count == 1
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            assert row_key.value == "demo-run-live"
            assert "filter: momentum" in table.border_title
            await asyncio.sleep(0.3)  # several ×2 runs refreshes
            await pilot.pause()
            assert table.row_count == 1, "hidden rows stay hidden"
            await pilot.press("escape")
            await pilot.pause()
            assert table.row_count == 3, "esc clears"
            assert "filter" not in table.border_title

    asyncio.run(scenario())


def test_invalid_regex_keeps_previous_filter_and_notifies() -> None:
    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            table = screen.query_one("#runs", DataTable)
            await pilot.press("/")
            await pilot.press(*"momentum")
            await pilot.press("enter")
            await pilot.pause()
            assert table.row_count == 1
            await pilot.press("/")
            await pilot.press(*"(")
            await pilot.press("enter")
            await pilot.pause()
            assert any("invalid regex" in m for m in _messages(app))
            assert "filter: momentum" in table.border_title
            assert table.row_count == 1, "previous filter stays active"

    asyncio.run(scenario())


def test_escape_while_the_filter_box_is_open_clears() -> None:
    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            table = screen.query_one("#runs", DataTable)
            box = screen.query_one("#filter")
            await pilot.press("/")
            await pilot.press(*"corrupt")
            await pilot.press("escape")  # input still focused and open
            await pilot.pause()
            assert not box.display
            assert table.row_count == 3, "esc cleared before applying"
            assert "filter" not in table.border_title

    asyncio.run(scenario())


def test_n_and_capital_n_step_with_wraparound() -> None:
    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            table = screen.query_one("#runs", DataTable)
            # Without a filter n only notifies.
            await pilot.press("n")
            await pilot.pause()
            assert any("no filter" in m for m in _messages(app))
            # "run" matches every demo id, so n/N walk the visible rows.
            await pilot.press("/")
            await pilot.press(*"run")
            await pilot.press("enter")
            await pilot.pause()

            def _cursor() -> str:
                key, _ = table.coordinate_to_cell_key(
                    table.cursor_coordinate)
                return key.value

            assert _cursor() == "demo-run-live"
            await pilot.press("n")
            assert _cursor() == "demo-run-final"
            await pilot.press("n")
            assert _cursor() == "demo-run-corrupt"
            await pilot.press("n")
            assert _cursor() == "demo-run-live", "wraps forward"
            await pilot.press("N")
            assert _cursor() == "demo-run-corrupt", "wraps backward"

    asyncio.run(scenario())


def test_follow_latch_survives_sort_and_filter_apply_and_clear() -> None:
    """S3 pin, both directions: the latch rides the row key across a sort
    rebuild, survives the row being hidden by a filter, and re-anchors
    when the filter clears — the clamped cursor never re-latches it."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            table = screen.query_one("#runs", DataTable)

            def _cursor() -> str:
                key, _ = table.coordinate_to_cell_key(
                    table.cursor_coordinate)
                return key.value

            assert _cursor() == "demo-run-live"
            await pilot.press("f")  # follow the cursor's run
            await pilot.pause()
            assert screen.view.followed_id == "demo-run-live"
            assert screen._feed_followed_id == "demo-run-live"
            # Sort rebuild (id): order changes, cursor rides the row key.
            await pilot.press("s")
            await pilot.pause()
            assert screen._row_keys == [
                "demo-run-corrupt", "demo-run-final", "demo-run-live"]
            assert _cursor() == "demo-run-live"
            # Filter hides the followed row: latch survives, cursor clamps.
            await pilot.press("/")
            await pilot.press(*"corrupt")
            await pilot.press("enter")
            await pilot.pause()
            assert table.row_count == 1
            assert _cursor() == "demo-run-corrupt", "clamped to the row"
            assert screen.view.followed_id == "demo-run-live", "latch kept"
            await asyncio.sleep(0.25)  # hidden across refreshes too
            await pilot.pause()
            assert screen.view.followed_id == "demo-run-live"
            assert screen._feed_followed_id == "demo-run-live", (
                "feed keeps the hidden followed run")
            # Clear the filter: the latch re-anchors the cursor.
            await pilot.press("escape")
            await pilot.pause()
            assert table.row_count == 3
            assert _cursor() == "demo-run-live", "re-anchored"
            # Cursor still on the followed row: f toggles the latch off.
            await pilot.press("f")
            await pilot.pause()
            assert screen.view.followed_id is None

    asyncio.run(scenario())


def test_c_copies_snapshot_derived_masked_line() -> None:
    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        copied: list[str] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
            await pilot.press("c")
            await pilot.pause()
            run = next(r for r in screen.frame.runs if r.id == "demo-run-live")
            assert copied == [panels.run_summary_line(run)]
            assert copied[0].startswith("run demo-run-live demo.momentum")

    asyncio.run(scenario())


def test_v_events_modal_resolves_the_cursor_row() -> None:
    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            # Cursor row 2: the corrupt run (empty tail) — NOT the
            # followed/auto run whose tail is alive.
            table = screen.query_one("#runs", DataTable)
            table.move_cursor(row=2)
            await pilot.press("v")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, EventsModalScreen)
            assert modal.run.id == "demo-run-corrupt"
            assert panels.run_events_body(modal.run).plain == "(no events)"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is screen
            # Cursor row 0: the live run's tail is full.
            table.move_cursor(row=0)
            await pilot.press("v")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, EventsModalScreen)
            assert modal.run.id == "demo-run-live"
            assert modal.run.events_tail
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is screen

    asyncio.run(scenario())
