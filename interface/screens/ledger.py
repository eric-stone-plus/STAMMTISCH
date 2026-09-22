"""The LEDGER screen (``:ledger``; route ``ledger``) — one-shot, read-only.

Absorbs the old LedgerScreen (``tui/screens/ledger.py:27`` + the
``tui/portfolio.py`` FIFO fold, read-only references — never imported):
fills fold into per-(broker, symbol) positions with realized P&L and
mark-to-live through the feeds lane. The data contract lives in
:mod:`interface.services.ledger`.

M6 scope — READ ONLY, deliberately: the old write ops (log fill /
delete fill) stay in the old tree because they need the audited confirm
lane ``:delete`` established (one write path at a time; see
REVIEWS/RETIREMENT.md Option B). A missing mark renders an honest
em dash, never a fabricated price. Refresh is manual (``r``): the
ledger changes only when someone writes it, and M6 has no write lane —
no live poll to run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from interface.app.spine import SlowLane
from interface.render.tokens import token

__all__ = ["LedgerScreen"]

_MISSING = "—"


class LedgerScreen(Screen[None]):
    """FIFO positions + the fill list, read-only."""

    route_name = "ledger"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back to the previous screen"),
        Binding("r", "refresh", "Reload the ledger and re-mark now"),
        Binding("j", "cursor_down", "Move the cursor down one row"),
        Binding("k", "cursor_up", "Move the cursor up one row"),
    ]
    CSS = """
    LedgerScreen { layout: vertical; }
    #lg-status { height: auto; padding: 0 1; color: $text-muted; }
    #lg-positions-wrap, #lg-fills-wrap {
        height: 1fr;
        border: round $panel-border;
        border-title-color: $panel-title;
        background: $surface;
    }
    """

    def __init__(self, state_root: str | Path | None = None,
                 service: Callable[[str | Path | None], Any] | None = None
                 ) -> None:
        super().__init__()
        self.state_root = state_root
        self._service = service
        self._lane = SlowLane()
        #: Test observability: frames landed, last rendered frame.
        self.frames_landed = 0
        self.last_frame: Any = None

    # -- the service seam (injection only; no import-time transport) ----

    def _read_ledger(self) -> Any:
        if self._service is not None:
            return self._service(self.state_root)
        from interface.services.ledger import LedgerService

        return LedgerService().snapshot(self.state_root)

    def compose(self):
        yield Static("", id="lg-status")
        with VerticalScroll():
            with Vertical(id="lg-positions-wrap"):
                yield DataTable(id="lg-positions", cursor_type="row")
            with Vertical(id="lg-fills-wrap"):
                yield DataTable(id="lg-fills", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        for widget_id, title, columns in (
            ("lg-positions", "POSITIONS (SIGNED FIFO)",
             ("BROKER", "SYMBOL", "NET QTY", "AVG COST", "LAST",
              "UNREALIZED", "REALIZED")),
            ("lg-fills", "FILLS (LOG ORDER)",
             ("WHEN", "BROKER", "SIDE", "SYMBOL", "QTY", "PRICE", "FEE",
              "ID")),
        ):
            table = self.query_one(f"#{widget_id}", DataTable)
            for column in columns:
                table.add_column(column)
            self.query_one(f"#{widget_id}-wrap",
                           Vertical).border_title = title
        self.query_one("#lg-positions", DataTable).focus()
        self._refresh()

    # -- refresh (SlowLane: never overlap, drop stale) ---------------------

    def _refresh(self) -> None:
        self._lane.run(self, self._read_ledger, self._deliver,
                       name="ledger-read")

    def _deliver(self, frame: Any) -> None:
        self.frames_landed += 1
        self.last_frame = frame
        positions = self.query_one("#lg-positions", DataTable)
        pos_anchor = positions.cursor_row if positions.row_count else 0
        positions.clear()
        for row in frame.positions:
            last = (_MISSING if row.last is None else f"{row.last:,.4g}")
            unrealized = (_MISSING if row.unrealized is None
                          else f"{row.unrealized:+,.2f}")
            realized = Text(f"{row.realized_pnl:+,.2f}")
            realized.stylize(token("market.up") if row.realized_pnl >= 0
                             else token("market.down"))
            positions.add_row(row.broker or _MISSING, row.symbol,
                              f"{row.net_qty:+,.6g}", f"{row.avg_cost:,.4g}",
                              last, unrealized, realized,
                              key=f"{row.broker}:{row.symbol}")
        fills = self.query_one("#lg-fills", DataTable)
        fills.clear()
        for fill in frame.fills:
            fills.add_row(fill.ts or _MISSING, fill.broker or _MISSING,
                          fill.side or "?", fill.symbol or "?",
                          f"{fill.qty:,.6g}", f"{fill.price:,.4g}",
                          f"{fill.fee:,.2f}", fill.id,
                          key=fill.id or None)
        if positions.row_count:
            # The cursor survives every re-read.
            positions.move_cursor(
                row=min(pos_anchor, positions.row_count - 1), animate=False)
        status = Text()
        if frame.error is not None:
            status.append(f"ledger error: {frame.error}",
                          style=token("state.crit"))
        else:
            status.append(
                f"{len(frame.positions)} position(s) · "
                f"{len(frame.fills)} fill(s) · "
                f"ledger: {self.state_root or '(no state root)'}"
                "/intel/portfolio")
        self.query_one("#lg-status", Static).update(status)

    # -- bindings -----------------------------------------------------------

    def action_refresh(self) -> None:
        if self._lane.in_flight:
            self.notify("a refresh is already in flight", severity="warning")
            return
        self._refresh()

    def _focused_table(self) -> DataTable:
        focused = self.focused
        return focused if isinstance(focused, DataTable) else (
            self.query_one("#lg-positions", DataTable))

    def action_cursor_down(self) -> None:
        table = self._focused_table()
        if table.row_count:
            table.move_cursor(
                row=min(table.row_count - 1, table.cursor_row + 1))

    def action_cursor_up(self) -> None:
        table = self._focused_table()
        if table.row_count:
            table.move_cursor(row=max(0, table.cursor_row - 1))

    def action_back(self) -> None:
        self.app.pop_screen()
