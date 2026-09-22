"""The ENERGY screen (``:energy``; route ``energy``) — spine-live, read-only.

Absorbs the old EnergyScreen (``tui/energy.py:579``, read-only reference
— never imported): the EIA v2 desk watchlist tape (crude / gas / coal /
world / outlook) with a per-series detail pane carrying recent
observations and the EIA attribution line. The data contract (series
specs, pinned-proxy egress, row collapse, formatters) lives in
:mod:`interface.services.energy`.

Discipline: the screen polls its service on the spine's slow services
lane (the ×15 multiplier) behind its own
:class:`~interface.app.spine.SlowLane` (never-overlap, drop-stale), and
mirrors the spine's services staleness into the border badge. The fetch
is constructor-injected; the default resolves lazily at call time (env:
``EIA_API_KEY`` + ``STAMMTISCH_ENERGY_PROXY``, fail-closed when absent
— the screen renders the honest error, never fabricated rows).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from interface.app.spine import SlowLane
from interface.render.stale import stale_badged_title
from interface.render.tokens import token

__all__ = ["EnergyScreen"]

_MISSING = "—"


class EnergyScreen(Screen[None]):
    """Terminal watchlist of EIA energy series. Read-only."""

    route_name = "energy"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back to the previous screen"),
        Binding("r", "reload", "Reload the watchlist now"),
        Binding("j", "cursor_down", "Move the cursor down one row"),
        Binding("k", "cursor_up", "Move the cursor up one row"),
    ]
    CSS = """
    EnergyScreen { layout: vertical; }
    #eg-status { height: auto; padding: 0 1; color: $text-muted; }
    #eg-table-wrap {
        height: 1fr;
        border: round $panel-border;
        border-title-color: $panel-title;
        background: $surface;
    }
    #eg-detail {
        height: 6;
        border: round $panel-border;
        border-title-color: $panel-title;
        padding: 0 1;
        color: $text-primary;
    }
    """

    def __init__(self, fetch: Callable[[], Any] | None = None) -> None:
        super().__init__()
        self._fetch = fetch
        self._lane = SlowLane()
        self._rows: tuple[Any, ...] = ()
        #: Test observability: frames landed, last rendered frame.
        self.frames_landed = 0
        self.last_frame: Any = None

    # -- the service seam (injection only; no import-time transport) ----

    def _fetch_watchlist(self) -> Any:
        if self._fetch is not None:
            return self._fetch()
        from interface.services.energy import fetch_watchlist

        return fetch_watchlist()

    def compose(self):
        yield Static("loading energy watchlist…", id="eg-status")
        with VerticalScroll(), Vertical(id="eg-table-wrap"):
            yield DataTable(id="eg-table", cursor_type="row")
        yield Static("select a row to inspect recent observations",
                     id="eg-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#eg-table", DataTable)
        for column in ("GROUP", "SERIES", "PERIOD", "LAST", "CHANGE", "UNIT"):
            table.add_column(column)
        self.query_one("#eg-table-wrap", Vertical).border_title = "WATCHLIST"
        self.app.spine.subscribe(self)
        self._reload()

    def on_unmount(self) -> None:
        self.app.spine.unsubscribe(self)

    # -- spine callbacks (slow services lane) -----------------------------

    def spine_frame(self, frame: Any, due: frozenset[str]) -> None:
        if "services" in due:
            self._reload()

    def spine_staleness(self, levels: dict[str, str]) -> None:
        title = stale_badged_title("WATCHLIST",
                                   levels.get("services", "fresh"))
        try:
            self.query_one("#eg-table-wrap", Vertical).border_title = title
        except Exception:  # noqa: BLE001, S110 - unmount race; cosmetic
            pass

    # -- reload (SlowLane: never overlap, drop stale) ----------------------

    def _reload(self) -> None:
        self._lane.run(self, self._fetch_watchlist, self._deliver,
                       name="energy-watchlist")

    def _deliver(self, frame: Any) -> None:
        self.frames_landed += 1
        self.last_frame = frame
        status = self.query_one("#eg-status", Static)
        via = frame.via or "proxy unavailable"
        if not frame.ok and not frame.rows:
            status.update(Text(f"[ERROR] {frame.error or 'fetch failed'} · "
                               "check EIA key/proxy, then reload",
                               style=token("state.crit")))
            self._rows = ()
            self._paint()
            return
        self._rows = tuple(frame.rows)
        if not frame.ok:
            status.update(Text(f"[ERROR] {frame.error} · {via}",
                               style=token("state.crit")))
        else:
            landed = sum(1 for row in self._rows if row.error is None)
            status.update(Text(
                f"{landed}/{len(self._rows)} series · EIA Open Data v2 · "
                f"{via}"))
        self._paint()
        if self._rows:
            self.query_one("#eg-table", DataTable).focus()

    def _paint(self) -> None:
        from interface.services.energy import format_change, format_value

        table = self.query_one("#eg-table", DataTable)
        anchor = table.cursor_row if table.row_count else 0
        table.clear()
        for row in self._rows:
            if row.error is not None:
                table.add_row(row.group or "?", row.label or "?", _MISSING,
                              Text("ERR", style=token("state.crit")),
                              _MISSING, row.unit or _MISSING, key=row.key)
                continue
            change = Text(format_change(row))
            if row.change is not None:
                change.stylize(token("market.up") if row.change >= 0
                               else token("market.down"))
            table.add_row(
                row.group or "?", row.label or "?", row.period or _MISSING,
                format_value(row.value, row.decimals), change,
                row.unit or _MISSING, key=row.key,
            )
        if table.row_count:
            # The cursor survives every slow-lane repaint (the overview
            # wall's stable-row discipline, tape edition).
            table.move_cursor(row=min(anchor, table.row_count - 1),
                              animate=False)
        if not self._rows:
            self.query_one("#eg-detail", Static).update("no series loaded")

    # -- events / bindings ---------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted
                                      ) -> None:
        self._show_detail(event.row_key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_detail(event.row_key)

    def _row_for(self, row_key: Any) -> Any | None:
        if row_key is None or row_key.value is None:
            return None
        return next((row for row in self._rows
                     if row.key == str(row_key.value)), None)

    def _show_detail(self, row_key: Any) -> None:
        from interface.services.energy import format_detail

        row = self._row_for(row_key)
        if row is not None:
            self.query_one("#eg-detail", Static).update(format_detail(row))

    def action_reload(self) -> None:
        if self._lane.in_flight:
            self.notify("a reload is already in flight", severity="warning")
            return
        self._reload()

    def action_cursor_down(self) -> None:
        table = self.query_one("#eg-table", DataTable)
        if table.row_count:
            table.move_cursor(
                row=min(table.row_count - 1, table.cursor_row + 1))

    def action_cursor_up(self) -> None:
        table = self.query_one("#eg-table", DataTable)
        if table.row_count:
            table.move_cursor(row=max(0, table.cursor_row - 1))

    def action_back(self) -> None:
        self.app.pop_screen()
