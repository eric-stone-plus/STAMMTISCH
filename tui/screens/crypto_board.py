"""COINS screen — crypto board over the free-data chains.

CoinGecko markets with Binance public tickers as the fallback (the same
service.crypto_board the health stats name), 7-day sparklines rendered
as text ramp characters, and a jump into the in-terminal candle chart.
Provenance stays explicit: the status line names the serving source and
the BTC dominance figure is computed over the fetched page only.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ..analysis import _run_async
from ..widgets import CYAN, DIM, GREEN, GRAY, RED, WHITE

_SPARK_RAMP = "▁▂▃▄▅▆▇█"


def spark_chars(values: list[float], width: int = 10) -> str:
    """Render a value series as text-ramp sparkline characters."""
    clean = [float(v) for v in values if v is not None]
    if len(clean) < 2:
        return ""
    low, high = min(clean), max(clean)
    if high <= low:
        return _SPARK_RAMP[3] * min(width, len(clean))
    step = max(1, len(clean) // width)
    sampled = clean[::step][:width]
    if clean[-1] not in sampled:
        sampled.append(clean[-1])
    return "".join(
        _SPARK_RAMP[min(7, int((v - low) / (high - low) * 7))]
        for v in sampled)


class CryptoBoardScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("v", "chart", "Chart"),
        Binding("question_mark", "show_help", "Keys"),
    ]
    CSS = """
    CryptoBoardScreen { layout: vertical; }
    #coins-status { height: 1; padding: 0 1; color: #808080; }
    #coins-wrap { height: 1fr; border: solid #505050; background: #000000; }
    """

    def __init__(self, engine: Any, config: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.engine = engine
        self.config = config
        self._rows: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static(
            "  COINS  |  free-data chains: coingecko -> binance  |  "
            "[V] Chart  [R] Refresh  [Esc] Back",
            classes="header-bar",
        )
        yield Static("  loading…", id="coins-status")
        with Vertical(id="coins-wrap"):
            yield DataTable(id="coins-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#coins-table", DataTable)
        table.add_columns("COIN", "NAME", "PRICE", "24H%", "7D", "MCAP", "VOL", "SRC")
        self.action_refresh()

    def action_refresh(self) -> None:
        def _work() -> dict[str, Any]:
            from ..datafeeds import service

            return service.crypto_board(limit=30)

        def _deliver(result: Any) -> None:
            if isinstance(result, dict) and not result.get("ok", True):
                self._apply([], None, str(result.get("error") or "failed"))
                return
            self._apply(result.get("rows") or [], result.get("btc_dominance"),
                        (result.get("rows") or [{}])[0].get("source", ""))

        _run_async(self, _work, _deliver, dedup_key="coins-refresh")

    def _apply(self, rows: list[dict[str, Any]], dominance: float | None,
               source: str) -> None:
        self._rows = rows
        try:
            table = self.query_one("#coins-table", DataTable)
            table.clear()
            for row in rows:
                chg = float(row.get("chg_24h") or 0)
                table.add_row(
                    str(row.get("symbol") or "?"),
                    str(row.get("name") or ""),
                    f"{float(row.get('last') or 0):,.4g}",
                    Text(f"{chg:+.2f}%", style=GREEN if chg >= 0 else RED),
                    Text(spark_chars(row.get("spark") or []), style=CYAN),
                    _compact(float(row.get("market_cap") or 0)),
                    _compact(float(row.get("volume") or 0)),
                    str(row.get("source") or "").split()[0],
                    key=str(row.get("symbol") or ""),
                )
            dominance_text = (f"  BTC dominance {dominance:.1f}%"
                              if dominance else "")
            stamp = f" · src: {source}" if source else ""
            self.query_one("#coins-status", Static).update(
                f"  {len(rows)} coins{dominance_text}{stamp}")
        except Exception:
            pass

    def _current_row(self) -> dict[str, Any] | None:
        table = self.query_one("#coins-table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        symbol = str(row_key.value or "")
        return next((row for row in self._rows
                     if row.get("symbol") == symbol), None)

    def action_chart(self) -> None:
        from ..charts import TerminalChartScreen

        row = self._current_row()
        if row is None:
            self.notify("No coin row selected.", severity="warning")
            return
        self.app.push_screen(TerminalChartScreen(
            self.engine, self.config, str(row.get("symbol"))))

    def action_show_help(self) -> None:
        from .modals import KeyHelpScreen

        self.app.push_screen(KeyHelpScreen("COINS — KEYS", [
            ("r", "refresh the board"),
            ("v", "in-terminal candle chart for the highlighted coin"),
            ("data", "coingecko markets, binance public tickers fallback"),
        ]))

    def action_back(self) -> None:
        self.app.pop_screen()


def _compact(value: float) -> str:
    if value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f}T"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"
