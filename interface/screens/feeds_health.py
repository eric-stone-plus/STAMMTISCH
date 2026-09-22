"""The FEEDS health screen (``:feeds``; route ``feeds``) — spine-live.

Absorbs the old FeedHealthScreen (``tui/screens/feeds.py:18``, read-only
reference — never imported): per-provider ok/failed/EWMA-latency/last-
error counters plus the lane cache line, rendered as a table with
latency-band and failure marking. The data contract lives in
:mod:`interface.services.feeds_health` (the tracked/all_stats copy);
this screen only renders and polls.

Discipline: the screen polls its service on the spine's slow services
lane (every ``services``-due, the ×15 multiplier) behind its own
:class:`~interface.app.spine.SlowLane` (never-overlap, drop-stale), and
mirrors the spine's services staleness into the border badge. The
service is constructor-injected; the default resolves lazily at call
time so this module imports no transport at import time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from interface.app.spine import SlowLane
from interface.render.stale import stale_badged_title
from interface.render.tokens import token
from interface.snapshot import WorkstationSnapshot

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from interface.services.feeds_health import FeedHealthFrame

__all__ = ["FeedsHealthScreen"]

_MISSING = "—"


class FeedsHealthScreen(Screen[None]):
    """Provider health for the interface's own feed lanes (read-only)."""

    route_name = "feeds"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back to the previous screen"),
        Binding("r", "refresh", "Refresh the provider counters now"),
        Binding("j", "cursor_down", "Move the cursor down one row"),
        Binding("k", "cursor_up", "Move the cursor up one row"),
    ]
    CSS = """
    FeedsHealthScreen { layout: vertical; }
    #fh-header { height: auto; color: $text-primary; padding: 0 1; }
    #fh-table-wrap {
        height: 1fr;
        border: round $panel-border;
        border-title-color: $panel-title;
        background: $surface;
    }
    #fh-cache { height: auto; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, service: Callable[[], Any] | None = None) -> None:
        super().__init__()
        self._service = service
        self._lane = SlowLane()
        #: Test observability: frames landed, last rendered frame.
        self.frames_landed = 0
        self.last_frame: FeedHealthFrame | None = None

    # -- the service seam (injection only; no import-time transport) ----

    def _read_health(self) -> Any:
        if self._service is not None:
            return self._service()
        from interface.services.feeds_health import FeedHealthService

        return FeedHealthService().frame()

    def compose(self):
        yield Static("", id="fh-header")
        with VerticalScroll(), Vertical(id="fh-table-wrap"):
            yield DataTable(id="fh-table", cursor_type="row")
        yield Static("", id="fh-cache")
        yield Footer()

    def on_mount(self) -> None:
        header = Text()
        header.append("FEEDS  ", style=f"bold {token('panel.title')}")
        header.append("provider health for the data lanes",
                      style=token("text.muted"))
        self.query_one("#fh-header", Static).update(header)
        table = self.query_one("#fh-table", DataTable)
        for column in ("PROVIDER", "OK", "FAIL", "AVG ms",
                       "LAST SUCCESS", "LAST ERROR"):
            table.add_column(column)
        self.query_one("#fh-table-wrap", Vertical).border_title = "PROVIDERS"
        self.app.spine.subscribe(self)
        self._refresh()

    def on_unmount(self) -> None:
        self.app.spine.unsubscribe(self)

    # -- spine callbacks (slow services lane) -----------------------------

    def spine_frame(self, frame: WorkstationSnapshot,
                    due: frozenset[str]) -> None:
        if "services" in due:
            self._refresh()

    def spine_staleness(self, levels: dict[str, str]) -> None:
        title = stale_badged_title("PROVIDERS",
                                   levels.get("services", "fresh"))
        try:
            self.query_one("#fh-table-wrap", Vertical).border_title = title
        except Exception:  # noqa: BLE001, S110 - unmount race; cosmetic
            pass

    # -- refresh (SlowLane: never overlap, drop stale) ---------------------

    def _refresh(self) -> None:
        self._lane.run(self, self._read_health, self._deliver,
                       name="feeds-health")

    def _deliver(self, frame: Any) -> None:
        from interface.services.feeds_health import (
            health_summary,
            latency_band,
            provider_row,
        )

        self.frames_landed += 1
        self.last_frame = frame
        table = self.query_one("#fh-table", DataTable)
        anchor = table.cursor_row if table.row_count else 0
        table.clear()
        for provider in frame.providers:
            row = provider_row(provider)
            avg = Text(str(row["avg_ms"]))
            band = latency_band(provider.avg_latency_ms)
            if band == "warn":
                avg.stylize(token("state.warn"))
            elif band == "crit":
                avg.stylize(f"bold {token('state.crit')}")
            fail = Text(str(row["failed"]))
            if provider.failed:
                fail.stylize(token("state.crit"))
            ok = Text(str(row["ok"]))
            if provider.ok:
                ok.stylize(token("state.ok"))
            error = str(row["last_error"])
            cell = (Text(error, style=token("state.crit"))
                    if provider.last_error else Text(error))
            table.add_row(provider.name, ok, fail, avg,
                          str(row["last_success"]), cell,
                          key=provider.name)
        cache = Text(health_summary(frame))
        if frame.cache.stale_entries:
            cache.stylize(token("state.warn"))
        if frame.error is not None:
            cache.append(f"  error: {frame.error}",
                         style=token("state.crit"))
        self.query_one("#fh-cache", Static).update(cache)
        if not frame.providers and frame.error is None:
            table.add_row(Text("no provider has served this process yet",
                               style=token("text.muted")),
                          _MISSING, _MISSING, _MISSING, _MISSING, _MISSING)
        if table.row_count:
            # The cursor survives every slow-lane repaint.
            table.move_cursor(row=min(anchor, table.row_count - 1),
                              animate=False)

    # -- bindings -----------------------------------------------------------

    def action_refresh(self) -> None:
        if self._lane.in_flight:
            self.notify("a refresh is already in flight", severity="warning")
            return
        self._refresh()

    def action_cursor_down(self) -> None:
        table = self.query_one("#fh-table", DataTable)
        if table.row_count:
            table.move_cursor(
                row=min(table.row_count - 1, table.cursor_row + 1))

    def action_cursor_up(self) -> None:
        table = self.query_one("#fh-table", DataTable)
        if table.row_count:
            table.move_cursor(row=max(0, table.cursor_row - 1))

    def action_back(self) -> None:
        self.app.pop_screen()
