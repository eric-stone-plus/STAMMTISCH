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
        Binding("b", "backtest", "Backtest"),
        Binding("s", "screener", "Screener"),
        Binding("question_mark", "show_help", "Keys"),
    ]
    CSS = """
    CryptoBoardScreen { layout: vertical; }
    #coins-status { height: 1; padding: 0 1; color: #808080; }
    #coins-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #coins-bt-wrap { height: 14; border: solid #505050; background: #000000; display: none; }
    .coins-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
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
        with Vertical(id="coins-bt-wrap"):
            yield Static("  crypto_backtest engine (operator command)",
                         classes="coins-label")
            yield DataTable(id="coins-bt", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#coins-table", DataTable)
        table.add_columns("COIN", "NAME", "PRICE", "24H%", "7D", "MCAP", "VOL", "SRC")
        self.query_one("#coins-bt", DataTable).add_columns(
            "STRATEGY", "RET%", "MAXDD%", "SHARPE", "SORTINO", "CALMAR",
            "EXPO%", "WIN%", "TRADES", "PF")
        self.action_refresh()

    def action_refresh(self) -> None:
        def _work() -> dict[str, Any]:
            from services.datafeeds import service

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

    # ── batch screener (hundreds of pairs) ──────────────────────────
    def action_screener(self) -> None:
        from services import batch_screener

        self._set_status("  screener running (hundreds of pairs, ~2-5 min)…")

        def _work() -> dict[str, Any]:
            return batch_screener.crypto_screen(self.config)

        def _deliver(result: Any) -> None:
            if isinstance(result, dict) and not result.get("ok", True):
                self._set_status(f"  screener FAILED — {result.get('error')}")
                return
            root = getattr(self.config, "state_root", None)
            path = batch_screener.persist(root, "crypto", result) if root else None
            tiers = result.get("fee_tiers") or {}
            summary = " · ".join(
                f"{fee}:{tier['median_net']:+.3%}" for fee, tier in tiers.items())
            self._set_status(
                f"  screener: {result.get('evaluated')}/{result.get('universe')} pairs"
                f"  net/trade median {summary}"
                + (f"  · saved {path.name}" if path else ""))

        _run_async(self, _work, _deliver, dedup_key="coins-screener")

    # ── crypto_backtest engine bridge (P1a) ─────────────────────────
    def action_backtest(self) -> None:
        from .. import cryptobacktest

        try:
            cryptobacktest.build_args(self.config, symbol="BTC/USDT",
                                      timeframe="1d", start="2024-01-01")
        except Exception as exc:
            self.notify(str(exc), severity="warning")
            return
        from .modals import SymbolInputScreen

        self.app.push_screen(SymbolInputScreen(
            "Engine backtest — SYMBOL TIMEFRAME STRATEGY [START] "
            "(e.g. BTC/USDT 1d all 2024-01-01)",
            self._run_engine_backtest))

    def _run_engine_backtest(self, value: str) -> None:
        from .. import cryptobacktest

        parts = str(value).split()
        if not 2 <= len(parts) <= 4:
            self.notify("Format: SYMBOL TIMEFRAME STRATEGY [START]",
                        severity="error")
            return
        symbol, timeframe = parts[0], parts[1]
        strategy = parts[2] if len(parts) > 2 else "all"
        start = parts[3] if len(parts) > 3 else "2024-01-01"

        def _work() -> Any:
            return cryptobacktest.run(self.config, symbol=symbol,
                                      timeframe=timeframe, start=start,
                                      strategy=strategy)

        def _deliver(result: Any) -> None:
            if isinstance(result, dict) and not result.get("ok", True):
                self._apply_backtest(None, str(result.get("error") or "failed"))
                return
            self._apply_backtest(result, None)

        self._set_status(f"  engine running: {symbol} {timeframe} {strategy}…")
        _run_async(self, _work, _deliver, dedup_key="coins-backtest")

    def _apply_backtest(self, payload: dict[str, Any] | None,
                        error: str | None) -> None:
        try:
            wrap = self.query_one("#coins-bt-wrap")
            wrap.styles.display = "block" if payload else "none"
            table = self.query_one("#coins-bt", DataTable)
            table.clear()
            if error:
                self._set_status(f"  engine FAILED — {error[:150]}")
                return
            if payload is None:  # defensive: never rely on asserts
                self._set_status("  engine returned no payload")
                return
            for row in payload.get("results") or []:
                table.add_row(
                    str(row.get("strategy") or "?"),
                    f"{float(row.get('total_return_pct') or 0):+.2f}%",
                    f"{float(row.get('max_drawdown_pct') or 0):.2f}%",
                    f"{float(row.get('sharpe_ratio') or 0):.2f}",
                    f"{float(row.get('sortino_ratio') or 0):.2f}",
                    f"{float(row.get('calmar_ratio') or 0):.2f}",
                    f"{float(row.get('exposure_pct') or 0):.1f}%",
                    f"{float(row.get('win_rate') or 0):.1f}%",
                    f"{int(row.get('total_trades') or 0)}",
                    f"{float(row.get('profit_factor') or 0):.2f}",
                )
            cfg = (payload.get("config") or {})
            self._set_status(
                f"  engine: crypto.backtest.v1 · {cfg.get('symbol')} "
                f"{cfg.get('timeframe')} {cfg.get('start_date')}→"
                f"{cfg.get('end_date') or 'latest'} · "
                f"SL {cfg.get('stop_loss_pct')}/TP {cfg.get('take_profit_pct')}"
                f"/trail {cfg.get('trailing_stop_pct')}")
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#coins-status", Static).update(text)
        except Exception:
            pass

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
