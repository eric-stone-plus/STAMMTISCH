"""The Overview wall, its interaction verbs, and the M1 modals.

Layout discipline carried over from the reviewed design:

- RUNS table: stable row keys per run id — matching rows are updated cell
  by cell, gone rows removed, new rows appended; never cleared and re-added
  on unchanged frames, so the cursor position survives every refresh. A
  structural change (sort, filter, membership, frame order) performs ONE
  rebuild that reuses the same keys and re-anchors the cursor by run id.
- ACTIVITY FEED: append-only :class:`RichLog` fed from the followed run's
  ``events_tail`` behind a seq cursor; history lines are never rewritten,
  the log is capped (``max_lines``). An explicit ``f`` follow overrides
  the auto policy (grilling D3); the feed keeps following a filtered-out
  run until it leaves the snapshot (grilling S3).
- GLANCE / SERVICES: static panels re-rendered from pure builders.

The M3 verb grammar (``/`` filter, ``n``/``N`` matches, ``f`` follow,
``s`` sort, ``c`` copy, ``v`` events) lives in the pure
:class:`~interface.app.verbs.RunsView`; this screen only binds it to the
DataTable. Staleness arrives as data (``spine_staleness``); this screen
applies the warn/crit border styles and the STALE title badge. Every
binding carries a description — help surfaces are generated from the
live keymap later.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, RichLog, Static

from interface.app.verbs import RunsView
from interface.render import panels
from interface.render.tokens import token
from interface.snapshot import (
    RunSnapshot,
    WorkstationSnapshot,
    workstation_flags,
)

__all__ = ["EventsModalScreen", "OverviewScreen", "RunDetailScreen"]

_FEED_CAP_LINES = 2000
_PANEL_TITLES = {"runs": "RUNS", "glance": "GLANCE", "services": "SERVICES",
                 "feed": "ACTIVITY"}


class RunDetailScreen(ModalScreen[None]):
    """Minimal run inspector: stages, gates, cost, full event tail."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Close run detail"),
    ]

    def __init__(self, run: RunSnapshot) -> None:
        super().__init__()
        self.run = run

    def compose(self):
        with Container(id="run-detail"):
            yield Static(panels.run_detail(self.run), id="run-detail-body")
        yield Footer()

    def action_dismiss(self) -> None:
        self.dismiss(None)


class EventsModalScreen(ModalScreen[None]):
    """The ``v`` modal: the CURSOR row's full event tail, scrollable."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Close the events view"),
    ]
    CSS = """
    #events-modal { align: center middle; }
    #events-body {
        width: 96; max-height: 80%;
        border: round $panel-border;
        border-title-color: $panel-title;
        padding: 0 2;
        background: $surface;
        color: $text-primary;
    }
    """

    def __init__(self, run: RunSnapshot) -> None:
        super().__init__()
        self.run = run

    def compose(self):
        with Container(id="events-modal"), VerticalScroll(id="events-body"):
            yield Static(panels.run_events_body(self.run))
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#events-body", VerticalScroll).border_title = (
            f"EVENTS · {self.run.id}")

    def action_dismiss(self) -> None:
        self.dismiss(None)


class OverviewScreen(Screen[None]):
    """The wall + the M3 verbs: runs table, feed, glance, services strip."""

    route_name = "runs"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "run_detail", "Open run detail for the selected run"),
        Binding("slash", "filter", "Filter runs by regex (Esc clears)"),
        Binding("n", "next_match", "Next filter match (wraps)"),
        Binding("N", "prev_match", "Previous filter match (wraps)"),
        Binding("f", "toggle_follow", "Follow the cursor's run (toggle)"),
        Binding("s", "cycle_sort", "Cycle sort: id / created / state-group"),
        Binding("c", "copy_summary", "Copy a one-line run summary"),
        Binding("v", "events_modal", "Show the cursor run's full events"),
        Binding("escape", "cancel_filter", "Clear the filter and close its box"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._frame: WorkstationSnapshot | None = None
        self._view = RunsView()
        self._row_keys: list[str] = []
        self._feed_followed_id: str | None = None
        self._feed_seq = 0
        self._feed_cursors: dict[str, int] = {}
        self._runs_stale_level = "fresh"
        # Test observability: what actually hit each surface.
        self.update_counts: dict[str, int] = {p: 0 for p in _PANEL_TITLES}
        self.update_counts["banner"] = 0
        self.feed_written = 0
        self.glance_plain = ""
        self.services_plain = ""
        self.banner_plain = ""

    def compose(self):
        yield Static(id="banner")
        yield Static(id="collector-error")
        yield Input(placeholder="filter regex over run/pipeline/state/flags "
                    "(empty clears, Esc cancels)", id="filter")
        with Horizontal(id="wall"):
            yield DataTable(id="runs")
            with Vertical(id="side"):
                yield Static(id="glance")
                yield Static(id="services")
        yield RichLog(id="feed", max_lines=_FEED_CAP_LINES, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self._runs_table
        table.cursor_type = "row"
        for column in panels.RUNS_COLUMNS:
            table.add_column(column.upper(), key=column)
        for widget_id, title in _PANEL_TITLES.items():
            widget = self.query_one(f"#{widget_id}")
            widget.border_title = title
        self.query_one("#collector-error", Static).display = False
        self.query_one("#filter", Input).display = False
        table.focus()
        self.app.spine.subscribe(self)

    def on_unmount(self) -> None:
        self.app.spine.unsubscribe(self)

    @property
    def frame(self) -> WorkstationSnapshot | None:
        """The last applied frame (None before the first one lands)."""
        return self._frame

    @property
    def view(self) -> RunsView:
        """The verb state machine (filter/sort/follow) — tests read this."""
        return self._view

    # -- spine callbacks (UI thread; the single mutation path calls these) --

    def spine_frame(self, frame: WorkstationSnapshot, due: frozenset[str]) -> None:
        """Apply one frame to exactly the panels the spine marked due."""
        self._frame = frame
        if "banner" in due:
            banner = panels.banner(frame)
            self.banner_plain = banner.plain
            self.query_one("#banner", Static).update(banner)
            error = self.query_one("#collector-error", Static)
            if frame.collector_error:
                error.update(Text(frame.collector_error, style=token("state.crit")))
                error.display = True
            else:
                error.display = False
        if "runs" in due:
            self._sync_runs(frame)
        if "glance" in due:
            glance = panels.glance_block(frame.quotes)
            self.glance_plain = glance.plain
            self.query_one("#glance", Static).update(glance)
        if "services" in due:
            services = panels.services_strip(frame.services)
            self.services_plain = services.plain
            self.query_one("#services", Static).update(services)
        if "feed" in due:
            self._append_feed(frame)
        for panel in due:
            if panel in self.update_counts:
                self.update_counts[panel] += 1

    def spine_staleness(self, levels: dict[str, str]) -> None:
        """Apply staleness data as styles: borders + STALE title badges."""
        for panel, title in _PANEL_TITLES.items():
            level = levels.get(panel, "fresh")
            widget = self.query_one(f"#{panel}")
            widget.remove_class("stale-warn", "stale-crit")
            if panel == "runs":
                self._runs_stale_level = level
                self._refresh_runs_title()
                continue
            if level == "warn":
                widget.add_class("stale-warn")
                widget.border_title = title
            elif level == "crit":
                widget.add_class("stale-crit")
                widget.border_title = f"{title} · STALE"
            else:
                widget.border_title = title

    # -- panel sync -------------------------------------------------------

    @property
    def _runs_table(self) -> DataTable:
        return self.query_one("#runs", DataTable)

    def _refresh_runs_title(self) -> None:
        """The runs border title: STALE badge + the active filter pattern."""
        title = _PANEL_TITLES["runs"]
        if self._view.filter_pattern is not None:
            title += f" · filter: {self._view.filter_pattern}"
        if self._runs_stale_level == "crit":
            title += " · STALE"
        widget = self.query_one("#runs")
        widget.remove_class("stale-warn", "stale-crit")
        if self._runs_stale_level == "warn":
            widget.add_class("stale-warn")
        elif self._runs_stale_level == "crit":
            widget.add_class("stale-crit")
        widget.border_title = title

    def _cursor_key(self) -> str | None:
        """The run id under the table cursor (None when there is none)."""
        table = self._runs_table
        if not table.row_count:
            return None
        try:
            key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:  # noqa: BLE001 - no cell at the coordinate
            return None
        return key.value if key is not None else None

    def _cursor_run(self) -> RunSnapshot | None:
        """Cursor-first resolution: the run under the cursor, if any."""
        key = self._cursor_key()
        if key is None or self._frame is None:
            return None
        return next((r for r in self._frame.runs if r.id == key), None)

    def _sync_runs(self, frame: WorkstationSnapshot) -> None:
        """View-model sync: in-place cells, ONE keyed rebuild on change.

        Hidden rows stay hidden across refreshes (the filter re-applies
        every sync); the rebuild reuses row keys and re-anchors the cursor
        by run id, so the follow latch survives structural changes without
        ever re-latching a clamped cursor (grilling S3).
        """
        table = self._runs_table
        flags = workstation_flags(frame)
        runs = self._view.visible_runs(
            frame.runs, feed_stale=flags["F"], degraded=flags["D"])
        keys = [run.id for run in runs]
        self._view.retain_followed({run.id for run in frame.runs})
        cells = panels.runs_table_rows(frame, runs)
        if self._row_keys == keys:
            for run, row in zip(runs, cells):
                for column, cell in zip(panels.RUNS_COLUMNS, row):
                    table.update_cell(run.id, column, cell, update_width=True)
            return
        cursor_before = self._cursor_key()
        table.clear()
        self._row_keys = []
        for run, row in zip(runs, cells):
            table.add_row(*row, key=run.id)
            self._row_keys.append(run.id)
        anchor = self._view.anchor_key(cursor_before, keys)
        if anchor is not None:
            table.move_cursor(row=keys.index(anchor))

    # -- audit chrome (the :delete write trail lands here) ----------------

    def audit_line(self, line: str) -> None:
        """One audit line into the activity feed (append-only chrome).

        Write-path audit lines share the feed's discipline with the
        follow marker: never a run event, never rewritten, capped by the
        RichLog's own ``max_lines``.
        """
        self.query_one("#feed", RichLog).write(
            Text(f"[audit] {line}", style=token("provenance.src")))

    def _append_feed(self, frame: WorkstationSnapshot) -> None:
        """Append-only feed from the followed run's tail behind a seq cursor."""
        run = panels.followed_run(frame, explicit_id=self._view.followed_id)
        if run is None:
            return
        if run.id != self._feed_followed_id:
            # Review B: stashing per-run cursors means switching A -> B -> A
            # resumes after A's already-seen seqs instead of re-appending
            # A's whole tail (history is never rewritten either way).
            if self._feed_followed_id is not None:
                self._feed_cursors[self._feed_followed_id] = self._feed_seq
            self._feed_followed_id = run.id
            self._feed_seq = self._feed_cursors.get(run.id, 0)
            marker = Text(f"── following {run.id} ──", style=token("text.muted"))
            self.query_one("#feed", RichLog).write(marker)
        feed = self.query_one("#feed", RichLog)
        for event in run.events_tail:
            if event.seq <= self._feed_seq:
                continue
            feed.write(panels.feed_line(event))
            self._feed_seq = event.seq
            self.feed_written += 1

    # ── the verb actions ────────────────────────────────────────────────

    def action_filter(self) -> None:
        """``/``: open the filter box (prefilled with the active pattern)."""
        box = self.query_one("#filter", Input)
        box.display = True
        if self._view.filter_pattern is not None:
            box.value = self._view.filter_pattern
        box.focus()

    def action_cancel_filter(self) -> None:
        """Esc: clear the filter and hide its box (open or not)."""
        box = self.query_one("#filter", Input)
        was_open = box.display
        box.value = ""
        box.display = False
        self._runs_table.focus()
        if self._view.filter_pattern is None and not was_open:
            return  # nothing to clear and the box was closed: plain no-op
        self._view.clear_filter()
        if self._frame is not None:
            self._sync_runs(self._frame)
        self._refresh_runs_title()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter box applies (or clears) the pattern."""
        if event.input.id != "filter":
            return
        self._set_filter(event.value.strip() or None)

    def _set_filter(self, pattern: str | None) -> None:
        if pattern is None:
            self._view.clear_filter()
            self.notify("filter cleared")
        else:
            error = self._view.set_filter(pattern)
            if error is not None:
                # Invalid regex: the previous filter stays active.
                self.notify(error, severity="error")
        self.query_one("#filter", Input).display = False
        self._runs_table.focus()
        if self._frame is not None:
            self._sync_runs(self._frame)
        self._refresh_runs_title()

    def _step_match(self, *, backward: bool) -> None:
        if not self._view.has_filter:
            self.notify("no filter — press / to set one", severity="warning")
            return
        keys = self._row_keys
        target = self._view.step_match(keys, self._cursor_key(),
                                        backward=backward)
        if target is not None:
            self._runs_table.move_cursor(row=keys.index(target))

    def action_next_match(self) -> None:
        """``n``: next match with wraparound."""
        self._step_match(backward=False)

    def action_prev_match(self) -> None:
        """``N``: previous match with wraparound."""
        self._step_match(backward=True)

    def action_toggle_follow(self) -> None:
        """``f``: latch the cursor's run (toggle); the feed follows it too."""
        cursor = self._cursor_key()
        if cursor is None:
            self.notify("no run under the cursor", severity="warning")
            return
        if self._view.toggle_follow(cursor):
            self.notify(f"following {cursor}")
        else:
            self.notify(f"unfollowed {cursor}")
        if self._frame is not None:
            self._append_feed(self._frame)

    def action_cycle_sort(self) -> None:
        """``s``: cycle the snapshot-side sort; ONE keyed rebuild."""
        mode = self._view.cycle_sort()
        self.notify(f"sort: {mode}")
        if self._frame is not None:
            self._sync_runs(self._frame)

    def action_copy_summary(self) -> None:
        """``c``: copy the cursor run's one-line masked summary."""
        run = self._cursor_run()
        if run is None:
            self.notify("no run under the cursor", severity="warning")
            return
        line = panels.run_summary_line(run)
        self.app.copy_to_clipboard(line)
        self.notify(f"copied: {line}")

    def action_events_modal(self) -> None:
        """``v``: the cursor row's full event tail (not the followed run)."""
        run = self._cursor_run()
        if run is None:
            self.notify("no run under the cursor", severity="warning")
            return
        if not isinstance(self.app.screen, OverviewScreen):
            return
        self.app.push_screen(EventsModalScreen(run))

    # -- interaction --------------------------------------------------------

    def action_run_detail(self) -> None:
        """Open the RunDetail modal for the run under the table cursor."""
        table = self._runs_table
        if not table.row_count:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        run_id = row_key.value if row_key is not None else None
        if run_id:
            self._open_run_detail(run_id)

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        """Enter on a focused table row opens the detail modal."""
        if event.row_key is not None and event.row_key.value:
            self._open_run_detail(event.row_key.value)

    def _open_run_detail(self, run_id: str) -> None:
        if not isinstance(self.app.screen, OverviewScreen):
            return  # modal already open; never stack detail on detail
        if self._frame is None:
            return
        run = next((r for r in self._frame.runs if r.id == run_id), None)
        if run is not None:
            self.app.push_screen(RunDetailScreen(run))
