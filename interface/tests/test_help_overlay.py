"""The ``?`` key sheet is GENERATED from the live binding table.

Pins (M3 acceptance: "help contains every bound key", blueprint §3):

- ``help_rows`` is a pure projection: deterministic sort, description
  fallback to the action name, the enabled flag survives.
- On the wall, the sheet is the FAITHFUL projection of the live binding
  table and covers every declared key the table carries — the drift-free
  guarantee: no hand-written list can go stale, because there is no
  hand-written list. (The live table is what the dispatcher would
  actually run: a nearer binding may shadow a screen-level one — the
  DataTable binds ``enter`` itself, and Enter still opens the detail via
  RowSelected, pinned in test_overview_pilot — and a focused Input
  strips keys it consumes. The footer and the sheet always agree.)
- On a pushed modal (run detail), the sheet describes THAT screen's keys
  (per-screen generation, one source of truth each).
- The regex filter Input never loses ``?`` to the overlay: Textual
  strips Input-consumed keys from the binding chain before dispatch
  (priority included), so ``?`` stays typeable text.

Same headless-Pilot pattern as ``test_overview_pilot.py`` (asyncio.run;
stepped demo provider + matching spine clock).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from textual.binding import Binding
from textual.screen import ActiveBinding
from textual.widgets import Input

from interface.app.help_overlay import HelpOverlay, help_rows
from interface.app.router import CommandBarScreen, CommandPaletteScreen
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


# ── the pure projection ────────────────────────────────────────────────


def test_help_rows_projects_sorts_and_keeps_the_enabled_flag() -> None:
    active = {
        "n": ActiveBinding(
            "OverviewScreen",
            Binding("n", "next_match", "Next filter match (wraps)"),
            True, ""),
        "escape": ActiveBinding(
            "WorkstationShell", Binding("escape", "cancel_filter", ""),
            False, ""),
        "ctrl+p": ActiveBinding(
            "WorkstationShell",
            Binding("ctrl+p", "palette", "Command palette (recents first)"),
            True, ""),
    }
    rows = help_rows(active, key_display=lambda binding: binding.key)
    # Deterministic order by display key, case-insensitive.
    assert [row.key for row in rows] == ["ctrl+p", "escape", "n"]
    escape = next(row for row in rows if row.key == "escape")
    # No description -> the action name, never a blank row.
    assert escape.description == "cancel_filter"
    assert escape.enabled is False
    match = next(row for row in rows if row.key == "n")
    assert match.description == "Next filter match (wraps)"
    assert match.enabled is True


# ── generated coverage on the wall ─────────────────────────────────────


def test_sheet_is_the_faithful_projection_of_the_live_table() -> None:
    """THE drift pin, in two clauses:

    (1) FIDELITY — the sheet's rows are exactly ``help_rows()`` over the
        screen's ``active_bindings`` at press time: nothing hand-listed,
        nothing filtered after the fact.
    (2) COVERAGE — every binding of the screen and the shell that the
        live table carries for itself must appear. A declared binding
        may legitimately be absent when the live table no longer carries
        IT for that key: the focused widget consumes the key (Textual
        strips it from the chain) or shadows it with a nearer binding —
        the one known case here is ``enter``, bound by the DataTable
        itself; Enter still opens the run detail via RowSelected (pinned
        in test_overview_pilot). The footer agrees with the sheet by
        construction: both read the same live table.
    """

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            before = dict(screen.active_bindings)
            await pilot.press("question_mark")
            await pilot.pause()
            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            # (1) fidelity: the sheet IS the projection, byte for byte.
            assert overlay.rows == help_rows(before, app.get_key_display)
            sheet = {row.description for row in overlay.rows}
            # (2) coverage: nothing the live table carries may drop.
            live = {entry.binding.description or entry.binding.action
                    for entry in before.values()}
            assert live <= sheet
            declared = [*OverviewScreen.BINDINGS, *WorkstationShell.BINDINGS]
            for binding in declared:
                if not binding.description:
                    continue
                entry = before.get(binding.key)
                # A declared binding may legitimately miss the sheet when
                # the live table no longer carries IT for that key: the
                # focused widget consumes the key (stripped from the
                # chain) or shadows it with a nearer binding of its own.
                shadowed = entry is not None and (
                    entry.binding.action != binding.action)
                assert (binding.description in sheet
                        or binding.key not in before
                        or shadowed), (
                    f"{binding.key!r} is neither on the sheet nor consumed/"
                    "shadowed by the focused widget — help has drifted")
            # The known shadowed key: the DataTable binds `enter` itself
            # (Enter still opens the detail via RowSelected — pinned in
            # test_overview_pilot).
            assert before["enter"].binding.action != "run_detail"
            # The dispatcher's own key vocabulary renders (get_key_display:
            # ctrl modifiers become carets, e.g. ctrl+p -> ^p).
            keys = {row.key for row in overlay.rows}
            assert "?" in keys and "^p" in keys
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    asyncio.run(scenario())


def test_sheet_on_a_modal_describes_that_modal() -> None:
    """Per-screen generation: over the run-detail modal the sheet carries
    the modal's own bindings, and ``?`` (its own binding) closes it."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)
            await pilot.press("question_mark")
            await pilot.pause()
            overlay = app.screen
            assert isinstance(overlay, HelpOverlay)
            descriptions = {row.description for row in overlay.rows}
            for binding in RunDetailScreen.BINDINGS:
                assert binding.description in descriptions
            # `?` toggles: the priority app action dismisses the sheet it
            # opened (priority dispatch beats the overlay's own binding).
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    asyncio.run(scenario())


def test_filter_input_keeps_the_question_mark_as_text() -> None:
    """The Input-consumes-key rule is load-bearing BY CONTRACT: a focused
    Input strips ``?`` from the binding chain before dispatch (priority
    included), so a regex filter can always contain ``?`` and no overlay
    opens."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press("slash")
            await pilot.pause()
            box = app.screen.query_one("#filter", Input)
            assert box.has_focus
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen), (
                "the filter Input must consume ? as text, never open help")
            assert box.value == "?"

    asyncio.run(scenario())


def test_palette_and_command_bar_keep_the_question_mark_as_text() -> None:
    """The README names THREE Inputs that keep ``?`` as text (filter,
    command bar, palette) — all three are pinned, so the documented
    claim never outruns the tests (round F, S-11)."""

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.4)
            await pilot.pause()
            # ⌃P palette: the focused search Input keeps `?`.
            await pilot.press("ctrl+p")
            await pilot.pause()
            palette = app.screen
            assert isinstance(palette, CommandPaletteScreen)
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.screen is palette, (
                "the palette Input must consume ? as text, never open help")
            assert palette.query_one(Input).value.endswith("?")
            await pilot.press("escape")
            await pilot.pause()
            # : command bar: same contract.
            await pilot.press("colon")
            await pilot.pause()
            bar = app.screen
            assert isinstance(bar, CommandBarScreen)
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.screen is bar, (
                "the command-bar Input must consume ? as text")
            assert bar.query_one(Input).value.endswith("?")
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    asyncio.run(scenario())
