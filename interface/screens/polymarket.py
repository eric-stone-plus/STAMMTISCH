"""The POLYMARKET screen (``:polymarket``; route ``polymarket``) — spine-live.

Absorbs the old PolymarketScreen (``tui/polymarket.py:338``, read-only
reference — never imported): the Gamma prediction-market tape ranked by
24h volume, with a ``/``-style LOCAL filter (view state stays
screen-side, per the seam rule) and a per-market detail pane. The data
contract (pinned-proxy egress, market collapse, formatters) lives in
:mod:`interface.services.polymarket`.

Discipline: the screen polls its service on the spine's slow services
lane (the ×15 multiplier) behind its own
:class:`~interface.app.spine.SlowLane` (never-overlap, drop-stale), and
mirrors the spine's services staleness into the border badge. The fetch
is constructor-injected; the default resolves lazily at call time (env:
``STAMMTISCH_POLYMARKET_PROXY``, fail-closed when absent — the honest
error renders, never fabricated markets). One deviation from the old
screen: selecting a row does NOT launch an external browser — this
surface is read-only and stays inside the terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from interface.app.spine import SlowLane
from interface.render.stale import stale_badged_title
from interface.render.tokens import token

__all__ = ["PolymarketScreen"]

_MISSING = "—"


def _matches(row: Any, query: str) -> bool:
    """The local ``/`` filter (old polymarket.py:329-335, screen-side)."""
    if not query:
        return True
    blob = f"{row.question} {row.slug} {row.category}".casefold()
    return query.casefold() in blob


class PolymarketScreen(Screen[None]):
    """Terminal tape of active Polymarket contracts. Read-only."""

    route_name = "polymarket"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back to the previous screen"),
        Binding("r", "reload", "Reload the tape now"),
        Binding("slash", "focus_filter", "Filter by question, slug, category"),
        Binding("j", "cursor_down", "Move the cursor down one row"),
        Binding("k", "cursor_up", "Move the cursor up one row"),
    ]
    CSS = """
    PolymarketScreen { layout: vertical; }
    #pm-filter { height: 3; }
    #pm-status { height: auto; padding: 0 1; color: $text-muted; }
    #pm-table-wrap {
        height: 1fr;
        border: round $panel-border;
        border-title-color: $panel-title;
        background: $surface;
    }
    #pm-detail {
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
        self._query = ""
        self._autofocused = False
        #: Test observability: frames landed, last rendered frame.
        self.frames_landed = 0
        self.last_frame: Any = None

    # -- the service seam (injection only; no import-time transport) ----

    def _fetch_markets(self) -> Any:
        if self._fetch is not None:
            return self._fetch()
        from interface.services.polymarket import fetch_markets

        return fetch_markets()

    def compose(self):
        yield Input(placeholder="filter by question, slug, or category…",
                    id="pm-filter")
        yield Static("loading active markets…", id="pm-status")
        with VerticalScroll(), Vertical(id="pm-table-wrap"):
            yield DataTable(id="pm-table", cursor_type="row")
        yield Static("select a row to inspect it", id="pm-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#pm-table", DataTable)
        for column in ("YES", "24H VOL", "END", "QUESTION"):
            table.add_column(column)
        self.query_one("#pm-table-wrap", Vertical).border_title = "MARKETS"
        self.query_one("#pm-filter", Input).focus()
        self.app.spine.subscribe(self)
        self._reload()

    def on_unmount(self) -> None:
        self.app.spine.unsubscribe(self)

    # -- spine callbacks (slow services lane) -----------------------------

    def spine_frame(self, frame: Any, due: frozenset[str]) -> None:
        if "services" in due:
            self._reload()

    def spine_staleness(self, levels: dict[str, str]) -> None:
        title = stale_badged_title("MARKETS",
                                   levels.get("services", "fresh"))
        try:
            self.query_one("#pm-table-wrap", Vertical).border_title = title
        except Exception:  # noqa: BLE001, S110 - unmount race; cosmetic
            pass

    # -- reload (SlowLane: never overlap, drop stale) ----------------------

    def _reload(self) -> None:
        self._lane.run(self, self._fetch_markets, self._deliver,
                       name="polymarket-tape")

    def _deliver(self, frame: Any) -> None:
        self.frames_landed += 1
        self.last_frame = frame
        status = self.query_one("#pm-status", Static)
        via = frame.via or "proxy unavailable"
        if not frame.ok:
            status.update(Text(
                f"[ERROR] {frame.error or 'fetch failed'} · {via}",
                style=token("state.crit")))
            self._rows = ()
            self._paint()
            return
        self._rows = tuple(frame.markets)
        status.update(Text(
            f"{len(self._rows)} active markets · ranked by 24h volume · "
            f"{via}"))
        self._paint()
        if self._rows and not self._autofocused:
            # Focus the table once, on the first landed tape; later
            # slow-lane re-deliveries never steal focus back from the
            # filter input (or wherever the operator moved it).
            self._autofocused = True
            self.query_one("#pm-table", DataTable).focus()

    def _visible(self) -> tuple[Any, ...]:
        return tuple(row for row in self._rows
                     if _matches(row, self._query))

    def _paint(self) -> None:
        from interface.services.polymarket import format_vol, format_yes

        table = self.query_one("#pm-table", DataTable)
        anchor = table.cursor_row if table.row_count else 0
        table.clear()
        visible = self._visible()
        for row in visible:
            yes = format_yes(row.yes)
            table.add_row(
                (Text(yes, style=token("state.info"))
                 if row.yes is not None else Text(yes)),
                format_vol(row.volume24hr),
                row.end or _MISSING,
                row.question or "?",
                key=row.id or row.slug or row.question,
            )
        if table.row_count:
            # The cursor survives every repaint AND every filter change.
            table.move_cursor(row=min(anchor, table.row_count - 1),
                              animate=False)
        if self._query:
            status = self.query_one("#pm-status", Static)
            status.update(Text(
                f'{len(visible)}/{len(self._rows)} markets match '
                f'"{self._query}"'))
        if not visible:
            detail = self.query_one("#pm-detail", Static)
            detail.update("no matching markets" if self._rows
                          else "no markets loaded")

    # -- events / bindings ---------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "pm-filter":
            return
        self._query = event.value.strip()
        self._paint()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "pm-filter":
            self.query_one("#pm-table", DataTable).focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted
                                      ) -> None:
        self._show_detail(event.row_key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_detail(event.row_key)

    def _row_for(self, row_key: Any) -> Any | None:
        if row_key is None or row_key.value is None:
            return None
        key = str(row_key.value)
        return next((row for row in self._visible()
                     if str(row.id or row.slug or row.question) == key), None)

    def _show_detail(self, row_key: Any) -> None:
        from interface.services.polymarket import format_detail

        row = self._row_for(row_key)
        if row is not None:
            self.query_one("#pm-detail", Static).update(format_detail(row))

    def action_reload(self) -> None:
        if self._lane.in_flight:
            self.notify("a reload is already in flight", severity="warning")
            return
        self._reload()

    def action_focus_filter(self) -> None:
        self.query_one("#pm-filter", Input).focus()

    def action_cursor_down(self) -> None:
        table = self.query_one("#pm-table", DataTable)
        if table.row_count:
            table.move_cursor(
                row=min(table.row_count - 1, table.cursor_row + 1))

    def action_cursor_up(self) -> None:
        table = self.query_one("#pm-table", DataTable)
        if table.row_count:
            table.move_cursor(row=max(0, table.cursor_row - 1))

    def action_back(self) -> None:
        self.app.pop_screen()
