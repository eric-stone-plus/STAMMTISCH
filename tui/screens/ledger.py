"""LEDGER screen — sandbox trade bookkeeping with FIFO positions.

Fills are logged by hand (or by the broker flow) into the state-root
ledger; the screen folds them into per-(broker, symbol) positions with
realized P&L and marks them against live quotes where the feed chains
know the symbol. Bookkeeping, not evidence: corrections delete a fill
with an explicit confirm.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from rich.text import Text
from textual.widgets import DataTable, Footer, Static

from ..analysis import _run_async
from .. import portfolio
from ..screens.broker import route_symbol
from ..widgets import DIM, GREEN, GRAY, RED


class LedgerScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("l", "log_fill", "Log fill"),
        Binding("x", "delete_fill", "Delete fill"),
    ]
    CSS = """
    LedgerScreen { layout: vertical; }
    #ledger-status { height: 1; padding: 0 1; color: #808080; }
    #ledger-positions-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #ledger-fills-wrap { height: 1fr; border: solid #505050; background: #000000; }
    .ledger-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
    """

    def __init__(self, driver: Any, config: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.driver = driver
        self.config = config
        self._fills: list[dict[str, Any]] = []

    def _state_root(self) -> str:
        return str(getattr(self.driver, "state_root", "") or "")

    def compose(self) -> ComposeResult:
        yield Static(
            "  LEDGER  |  FIFO positions from logged fills  |  "
            "[L] Log fill  [X] Delete fill  [R] Refresh  [Esc] Back",
            classes="header-bar",
        )
        yield Static("", id="ledger-status")
        with Vertical(id="ledger-positions-wrap"):
            yield Static("  Positions (signed FIFO)", classes="ledger-label")
            yield DataTable(id="ledger-positions", cursor_type="row")
        with Vertical(id="ledger-fills-wrap"):
            yield Static("  Fills (newest last)", classes="ledger-label")
            yield DataTable(id="ledger-fills", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        positions = self.query_one("#ledger-positions", DataTable)
        positions.add_columns("BROKER", "SYMBOL", "NET QTY", "AVG COST",
                              "LAST", "UNREALIZED", "REALIZED")
        fills = self.query_one("#ledger-fills", DataTable)
        fills.add_columns("WHEN", "BROKER", "SIDE", "SYMBOL", "QTY", "PRICE",
                          "FEE", "ID")
        self.action_refresh()

    def action_refresh(self) -> None:
        state_root = self._state_root()

        def _work() -> dict[str, Any]:
            fills = portfolio.load(state_root) if state_root else []
            rows = portfolio.positions(fills)
            symbols = [row["symbol"] for row in rows]
            quotes: dict[str, dict[str, Any]] = {}
            if symbols:
                try:
                    from .. import livefeed

                    quotes = livefeed.fetch_batch(symbols)
                except Exception:
                    quotes = {}
            return {"fills": fills, "rows": rows, "quotes": quotes,
                    "error": ""}

        def _deliver(result: Any) -> None:
            if isinstance(result, dict) and not result.get("ok", True):
                self._set_status(f"ledger error: {result.get('error')}")
                return
            self._fills = result.get("fills") or []
            self._render_ledger(result.get("rows") or [], result.get("quotes") or {})

        _run_async(self, _work, _deliver, dedup_key="ledger-refresh")

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#ledger-status", Static).update(f"  {text}")
        except Exception:
            pass

    def _render_ledger(self, rows: list[dict[str, Any]],
                quotes: dict[str, dict[str, Any]]) -> None:
        positions = self.query_one("#ledger-positions", DataTable)
        positions.clear()
        for row in rows:
            quote = quotes.get(row["symbol"])
            last = float(quote["last"]) if quote and quote.get("last") else None
            unrealized = ("" if last is None else
                          f"{(last - row['avg_cost']) * row['net_qty']:+,.2f}")
            realized = row["realized_pnl"]
            positions.add_row(
                row["broker"] or "—",
                row["symbol"],
                f"{row['net_qty']:+,.6g}",
                f"{row['avg_cost']:,.4g}",
                "—" if last is None else f"{last:,.4g}",
                unrealized,
                Text(f"{realized:+,.2f}", style=GREEN if realized >= 0 else RED),
                key=f"{row['broker']}:{row['symbol']}",
            )
        fills = self.query_one("#ledger-fills", DataTable)
        fills.clear()
        for fill in self._fills:
            fills.add_row(
                str(fill.get("ts") or ""),
                str(fill.get("broker") or "—"),
                str(fill.get("side") or "?"),
                str(fill.get("symbol") or "?"),
                f"{float(fill.get('qty') or 0):,.6g}",
                f"{float(fill.get('price') or 0):,.4g}",
                f"{float(fill.get('fee') or 0):,.2f}",
                str(fill.get("id") or ""),
                key=str(fill.get("id") or ""),
            )
        self._set_status(
            f"{len(rows)} position(s) · {len(self._fills)} fill(s) · "
            f"ledger: {self._state_root() or '(no state root)'}/intel/portfolio")

    # ── mutations ────────────────────────────────────────────────────
    def action_log_fill(self) -> None:
        if not self._state_root():
            self.notify("No state root — initialize first.", severity="warning")
            return
        from .modals import SymbolInputScreen

        self.app.push_screen(SymbolInputScreen(
            "Log fill — format: SIDE SYMBOL QTY PRICE (e.g. buy AAPL 1 332.00)",
            self._confirm_fill))

    def _confirm_fill(self, value: str) -> None:
        from .modals import ConfirmScreen

        parts = str(value).split()
        if len(parts) != 4:
            self.notify("Format: SIDE SYMBOL QTY PRICE", severity="error")
            return
        side, symbol, qty, price = parts
        broker = route_symbol(symbol)
        self.app.push_screen(ConfirmScreen(
            f"  Log fill: {side.upper()} {qty} {symbol} @ {price}\n\n"
            f"  routed broker: {broker.upper()}",
            lambda: self._add_fill(side, symbol, qty, price, broker)))

    def _add_fill(self, side: str, symbol: str, qty: str, price: str,
                  broker: str) -> None:
        def _work() -> dict[str, Any]:
            fill = portfolio.add_fill(self._state_root(), side, symbol,
                                      float(qty), float(price), broker=broker)
            return {"ok": True, "fill": fill}

        def _on_result(result: Any) -> None:
            if isinstance(result, dict) and not result.get("ok", True):
                self.notify(f"Fill rejected: {result.get('error')}",
                            severity="error")
                return
            fill = (result or {}).get("fill") or {}
            self.notify(f"Logged {fill.get('side')} {fill.get('symbol')} "
                        f"(id {fill.get('id')})")
            self.action_refresh()

        _run_async(self, _work, _on_result, dedup_key="ledger-add")

    def action_delete_fill(self) -> None:
        table = self.query_one("#ledger-fills", DataTable)
        if table.row_count == 0:
            self.notify("No fills to delete.", severity="warning")
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        fill_id = str(row_key.value or "")
        if not fill_id:
            self.notify("Select a fill row first.", severity="warning")
            return
        from .modals import ConfirmScreen

        self.app.push_screen(ConfirmScreen(
            f"  Delete fill {fill_id} from the ledger?", 
            lambda: self._delete(fill_id)))

    def _delete(self, fill_id: str) -> None:
        def _work() -> dict[str, Any]:
            removed = portfolio.remove_fill(self._state_root(), fill_id)
            return {"ok": removed, "error": "" if removed else "fill not found"}

        def _on_result(result: Any) -> None:
            if isinstance(result, dict) and not result.get("ok", True):
                self.notify(f"Delete failed: {result.get('error')}",
                            severity="error")
                return
            self.notify(f"Deleted fill {fill_id}")
            self.action_refresh()

        _run_async(self, _work, _on_result, dedup_key="ledger-delete")

    def action_back(self) -> None:
        self.app.pop_screen()
