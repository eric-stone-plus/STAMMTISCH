"""Domain boards: futures, shipping, security, plugin browser."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, OptionList, Select, Static, TextArea
from rich.text import Text
from textual.widgets.option_list import Option

from ..driver import StammtischDriver
from ..ai_driver import AIDriver, ChatResponse
from ..engine import QuantEngine
from ..analysis import DataFetchScreen, BacktestScreen, IndicatorsScreen, PortfolioScreen, GatesScreen
from ..analysis import _run_async
from ..widgets import (
    GRAY, DIM, GREEN, AMBER, RED, CYAN, WHITE,
    status_badge, DigestWidget, EventTimeline, GateCard,
    StageFlowWidget, SystemHud,
)

import logging

logger = logging.getLogger(__name__)

class DomainBrowserScreen(Screen):
    """Read-only listing of one domain plugin's root directory.

    Plugins are operator-local config entries ({"label", "root"}); the TUI
    only ever reads the directory. A missing or unreadable root renders an
    explicit notice instead of raising.
    """

    BINDINGS = [Binding("escape", "back", "Back")]
    CSS = """
    DomainBrowserScreen { layout: vertical; }
    #domain-body { height: 1fr; border: solid #505050; background: #000000; padding: 1 2; }
    """

    def __init__(self, label: str, root: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.label = label
        self.root = root

    def compose(self) -> ComposeResult:
        yield Static(
            f"  {self.label}  |  {self.root}  |  Esc back",
            classes="header-bar",
        )
        yield ScrollableContainer(
            # Entry names are operator data: render as plain text, never as
            # markup.
            Static(self._listing(), id="domain-text", markup=False),
            id="domain-body",
        )
        yield Footer()

    def _listing(self) -> str:
        root = Path(self.root).expanduser()
        if not root.exists():
            return f"  Domain root does not exist:\n  {root}\n"
        if not root.is_dir():
            return f"  Domain root is not a directory:\n  {root}\n"
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            return f"  Domain root is not readable:\n  {root}\n  {exc}\n"
        directories = sorted(
            (entry for entry in entries if entry.is_dir()),
            key=lambda entry: entry.name.casefold(),
        )
        files = sorted(
            (entry for entry in entries if not entry.is_dir()),
            key=lambda entry: entry.name.casefold(),
        )
        if not directories and not files:
            return f"  {root}\n\n  (empty)\n"
        lines = [f"  {root}", ""]
        lines += [f"  {entry.name}/" for entry in directories]
        lines += [f"  {entry.name}" for entry in files]
        return "\n".join(lines) + "\n"

    def action_back(self) -> None:
        self.app.pop_screen()
# Display names for well-known continuous tickers; anything else renders
# its raw symbol. Names are public product facts, never operator data.
FUTURES_NAMES = {
    "BZ=F": "ICE Brent Crude, front-month continuous",
    "CL=F": "NYMEX WTI Crude, front-month continuous",
    "NG=F": "NYMEX Henry Hub Gas, front-month continuous",
    "GC=F": "COMEX Gold, front-month continuous",
    "SI=F": "COMEX Silver, front-month continuous",
}
# Category assignment for provider-backed tickers. Exchange-settled adapter
# instruments carry their own ``group`` from the board payload.
FUTURES_CATEGORIES = {
    "BZ=F": "ENERGY",
    "CL=F": "ENERGY",
    "NG=F": "ENERGY",
    "GC=F": "METALS",
    "SI=F": "METALS",
}
class _RowTable(DataTable):
    """DataTable that leaves ←/→ to the screen.

    The default bindings consume the arrows as horizontal cell movement or
    scrolling whenever the table overflows its width, which varies with
    terminal size; with a row cursor they carry no meaning. Stripping them
    lets the screen's category bindings fire at any width.
    """

    BINDINGS = [
        binding
        for binding in DataTable.BINDINGS
        if getattr(binding, "key", None) not in ("left", "right")
    ]
def _open_browser_chart(screen: Any, config: Any, symbol: str) -> None:
    """Start the local chart server (first use) and open /chart/<symbol>."""
    import webbrowser

    from ..chart_server import DEFAULT_PORT, ensure_running

    configured_port = config.get("chart_port", DEFAULT_PORT) if config else DEFAULT_PORT

    def _deliver(port: Any) -> None:
        if not isinstance(port, int) or port <= 0:
            screen.notify("chart server failed to start", severity="error")
            return
        url = f"http://127.0.0.1:{port}/chart/{symbol}"
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if opened:
            screen.notify(f"Chart: {url}", severity="information")
        else:
            screen.notify(f"Chart ready at {url} (no browser could be opened)",
                          severity="warning")

    _run_async(screen, lambda: ensure_running(configured_port), _deliver)
class FuturesScreen(Screen):
    """Futures board with category switching (←/→).

    Two data paths feed one row model: provider-backed continuous tickers
    (quantkit/Yahoo — full OHLCV, browser K-line on `k`) and exchange-settled
    contracts from the configured ``futures_cmd`` adapter (settle, curve,
    open interest). A fetch failure renders per-row, never as a crash.
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("k", "chart", "K-line"),
        Binding("b", "backtest", "Backtest"),
        Binding("f", "fetch", "Fetch"),
        Binding("t", "indicators", "Indicators"),
        Binding("p", "portfolio", "Portfolio"),
        # Priority: the board table would otherwise consume the arrows as
        # horizontal scrolling whenever its rows overflow the terminal.
        Binding("left", "prev_category", "Prev Cat", priority=True),
        Binding("right", "next_category", "Next Cat", priority=True),
    ]
    CSS = """
    FuturesScreen { layout: vertical; }
    #fut-cats { height: 1; padding: 0 1; background: #202020; }
    #fut-board-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #fut-detail { height: 16; layout: horizontal; }
    .fut-side { width: 1fr; border: solid #505050; background: #000000; }
    .fut-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
    """

    def __init__(self, engine: Any, config: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.engine = engine
        self.config = config
        self._symbols: list[str] = list(config.futures_symbols) if config else []
        self._cats: list[str] = []
        self._by_cat: dict[str, list[dict[str, Any]]] = {}
        self._cat_idx = 0
        self._detail_source: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            "  FUTURES  |  [←→] Category  [R] Refresh  [K] K-line  "
            "[B] Backtest  [F] Fetch  [T] Indicators  [P] Portfolio  [Esc] Back",
            classes="header-bar",
        )
        yield Static("", id="fut-cats")
        with Vertical(id="fut-board-wrap"):
            yield Static("  Continuous contracts & exchange settlements (daily)",
                         classes="fut-label")
            yield _RowTable(id="fut-board", cursor_type="row")
        with Horizontal(id="fut-detail"):
            with Vertical(classes="fut-side"):
                yield Static("  Recent", classes="fut-label")
                yield DataTable(id="fut-recent", cursor_type="row")
            with Vertical(classes="fut-side"):
                yield Static("  Forward curve", classes="fut-label")
                yield DataTable(id="fut-curve", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        board = self.query_one("#fut-board", DataTable)
        board.add_columns("CODE", "NAME", "RECO", "LAST", "CHG%", "5D%", "20D%", "VOL", "UNIT")
        self._cci_timer = self.set_interval(5.0, self._cci_tick)
        self.query_one("#fut-curve", DataTable).add_columns("MONTH", "SETTLE")
        self._render_strip()
        self.action_refresh()

    # ── data loading (worker thread) ───────────────────────────────

    def action_refresh(self) -> None:
        if not self._symbols and not (
            self.config and self.config.futures_argv
        ):
            self.notify(
                "No futures sources configured (futures_symbols, futures_cmd).",
                severity="warning",
            )
            return
        _run_async(self, self._load, self._apply, dedup_key="futures-board")
        _run_async(self, self._load_quotes, self._apply_quotes,
                   dedup_key="futures-quotes")

    def _load(self) -> dict[str, Any]:
        """Fast board pass: CCI parquets + exchange adapters, no network
        round-trip on the critical path (provider quotes stream in via
        _load_quotes and merge on arrival)."""
        from ..engine import _missing_ohlcv, _normalize_symbol

        quotes: dict[str, dict[str, Any]] = {}
        if self._symbols:
            try:
                from quantkit.data import fetch_ohlcv
            except ImportError:
                quotes = {symbol: {"error": "quantkit is not installed"}
                          for symbol in self._symbols}
            else:
                for symbol in self._symbols:
                    quotes[symbol] = {"error": "pending"}
        return self._assemble(quotes)

    def _load_quotes(self) -> dict[str, Any]:
        from ..engine import _missing_ohlcv, _normalize_symbol

        quotes: dict[str, dict[str, Any]] = {}
        if self._symbols:
            try:
                from quantkit.data import fetch_ohlcv
            except ImportError:
                quotes = {symbol: {"error": "quantkit is not installed"}
                          for symbol in self._symbols}
            else:
                start = (date.today() - timedelta(days=400)).isoformat()
                for raw in self._symbols:
                    symbol = _normalize_symbol(raw)
                    try:
                        df = fetch_ohlcv(
                            symbol, market="auto", start=start,
                            data_dir=str(self.config.data_dir),
                        )
                    except Exception as exc:
                        quotes[symbol] = {"error": str(exc)[:120]}
                        continue
                    if _missing_ohlcv(df):
                        quotes[symbol] = {"error": "no data"}
                        continue
                    df = df.dropna(subset=["close"])
                    if df.empty:
                        quotes[symbol] = {"error": "no data"}
                        continue
                    closes = [float(v) for v in df["close"]]
                    last = closes[-1]

                    def _pct(days: int, _closes: list[float] = closes,
                             _last: float = last) -> float | None:
                        if len(_closes) > days and _closes[-1 - days] > 0:
                            return round((_last / _closes[-1 - days] - 1) * 100, 2)
                        return None

                    prev = closes[-2] if len(closes) > 1 else None
                    recent = []
                    for ts, row in df.tail(12).iterrows():
                        recent.append({
                            "date": str(ts)[:10],
                            "open": round(float(row["open"]), 4),
                            "high": round(float(row["high"]), 4),
                            "low": round(float(row["low"]), 4),
                            "close": round(float(row["close"]), 4),
                            "volume": float(row.get("volume", 0) or 0),
                        })
                    quotes[symbol] = {
                        "last": last,
                        "chg_pct": round((last / prev - 1) * 100, 2) if prev else None,
                        "pct5": _pct(5),
                        "pct20": _pct(20),
                        "volume": float(df["volume"].iloc[-1] or 0),
                        "recent": recent,
                    }
        return self._assemble(quotes)

    def _assemble(self, quotes: dict[str, dict[str, Any]]) -> dict[str, Any]:
        # SGX shipping settlements (FFA) moved onto the futures board.
        shipping_board = None
        shipping_argv = tuple(self.config.shipping_argv) if self.config else ()
        if shipping_argv:
            from ..domaindata import DomainDriver

            shipped = DomainDriver(shipping_argv).board()
            if shipped.get("ok"):
                shipping_board = shipped
        # CCI indices (all categories) from the cached daily parquets.
        cci_rows = []
        try:
            from .. import signals as _sig

            cci_rows = _sig.cci_daily_board(
                Path(getattr(self.config, "data_dir", "") or ""))
        except Exception:
            cci_rows = []
        adapter_board = None
        adapter_error = None
        argv = tuple(self.config.futures_argv) if self.config else ()
        if argv:
            from ..domaindata import DomainDriver

            board = DomainDriver(argv).board()
            if board.get("ok"):
                adapter_board = board
            else:
                adapter_error = str(board.get("error", "adapter failed"))
        return {
            "ok": True,
            "quotes": quotes,
            "adapter": adapter_board,
            "adapter_error": adapter_error,
            "shipping": shipping_board,
            "cci": cci_rows,
        }

    # ── row model ──────────────────────────────────────────────────

    @staticmethod
    def _pct_from_recent(recent: list[dict[str, Any]], days: int) -> float | None:
        """recent is newest-first; [0] is the latest settle."""
        if len(recent) > days and recent[days].get("settle"):
            base = float(recent[days]["settle"])
            if base > 0:
                return round((float(recent[0]["settle"]) / base - 1) * 100, 2)
        return None

    def _build_items(self, result: dict[str, Any]) -> None:
        items: list[dict[str, Any]] = []
        for symbol in self._symbols:
            quote = result.get("quotes", {}).get(symbol)
            category = FUTURES_CATEGORIES.get(symbol, "OTHER")
            if quote is None:
                continue
            if "error" in quote:
                items.append({
                    "key": f"yahoo:{symbol}", "source": "yahoo", "code": symbol,
                    "category": category, "name": FUTURES_NAMES.get(symbol, ""),
                    "error": quote["error"],
                })
                continue
            items.append({
                "key": f"yahoo:{symbol}", "source": "yahoo", "code": symbol,
                "category": category,
                "name": FUTURES_NAMES.get(symbol, ""),
                "unit": "",
                "last": quote["last"], "chg_pct": quote["chg_pct"],
                "pct5": quote["pct5"], "pct20": quote["pct20"],
                "volume": quote["volume"], "recent": quote["recent"],
                "curve": [],
            })
        adapter = result.get("adapter") or {}
        for inst in adapter.get("instruments") or []:
            recent = inst.get("recent") or []
            items.append({
                "key": f"sgx:{inst['code']}", "source": "sgx",
                "code": inst["code"],
                "category": str(inst.get("group") or "EXCHANGE").upper(),
                "name": inst.get("name", ""),
                "unit": inst.get("unit", ""),
                "last": inst.get("settle"),
                "chg_pct": inst.get("change_pct"),
                "pct5": self._pct_from_recent(recent, 5),
                "pct20": self._pct_from_recent(recent, 20),
                "volume": inst.get("volume") or 0,
                "recent": recent,
                "curve": inst.get("curve") or [],
            })
        shipping = result.get("shipping") or {}
        for inst in shipping.get("instruments") or []:
            recent = inst.get("recent") or []
            group = str(inst.get("group") or "").upper()
            if not group.startswith("FFA"):
                continue
            items.append({
                "key": f"sgx:{inst['code']}", "source": "sgx",
                "code": inst["code"], "category": "FFA",
                "name": inst.get("name", ""), "unit": inst.get("unit", ""),
                "last": inst.get("settle"), "chg_pct": inst.get("change_pct"),
                "pct5": self._pct_from_recent(recent, 5),
                "pct20": self._pct_from_recent(recent, 20),
                "volume": inst.get("volume") or 0,
                "recent": recent, "curve": inst.get("curve") or [],
            })
        # CCI (local parquet) leads so the default category is the one
        # that renders instantly; provider-backed categories load later.
        items = list(result.get("cci") or []) + items
        cats: list[str] = []
        by_cat: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            category = item["category"]
            if category not in by_cat:
                by_cat[category] = []
                cats.append(category)
            by_cat[category].append(item)
        self._cats = cats
        self._by_cat = by_cat
        if self._cat_idx >= len(cats):
            self._cat_idx = 0

    # ── rendering ──────────────────────────────────────────────────

    @staticmethod
    def _fmt_pct(value: float | None) -> str:
        if value is None:
            return "—"
        return f"{value:+.2f}%"

    def _strip_text(self) -> Text:
        strip = Text()
        for index, category in enumerate(self._cats):
            if index:
                strip.append(" ")
            label = f"  {category}  "
            strip.append(label, style="bold reverse" if index == self._cat_idx
                         else "color(160)")
        return strip

    def _render_strip(self) -> None:
        self.query_one("#fut-cats", Static).update(self._strip_text())

    def _render_board(self) -> None:
        from .. import signals as signals_mod

        board = self.query_one("#fut-board", DataTable)
        board.clear()
        items = self._by_cat.get(self._cats[self._cat_idx], []) if self._cats else []
        for item in items:
            if item.get("source") == "cci":
                reco, why = signals_mod.cci_reco(item)
            else:
                reco, why = signals_mod.futures_reco(item)
            if "error" in item:
                board.add_row(item["code"], item["name"], "—", item["error"],
                              "", "", "", "", "", key=item["key"])
                continue
            board.add_row(
                item["code"],
                item["name"],
                reco,
                "—" if item["last"] is None else f"{item['last']:,.2f}",
                self._fmt_pct(item["chg_pct"]),
                self._fmt_pct(item["pct5"]),
                self._fmt_pct(item["pct20"]),
                f"{item['volume']:.0f}",
                item["unit"],
                key=item["key"],
            )
        if board.row_count:
            board.move_cursor(row=0)
            self._render_detail(items[0])

    def _render_detail(self, item: dict[str, Any]) -> None:
        recent_table = self.query_one("#fut-recent", DataTable)
        if item["source"] != self._detail_source:
            recent_table.clear(columns=True)
            if item["source"] == "sgx":
                recent_table.add_columns("DATE", "SETTLE", "VOL", "OI", "MONTH")
            else:
                recent_table.add_columns("DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME")
            self._detail_source = item["source"]
        else:
            recent_table.clear()
        # Newest dates first (descending)
        for bar in reversed(item.get("recent") or []):
            if item["source"] == "sgx":
                recent_table.add_row(
                    bar["date"], f"{bar['settle']:,.2f}",
                    f"{bar.get('volume', 0):.0f}",
                    f"{bar.get('open_interest', 0):.0f}",
                    str(bar.get("month", "")),
                )
            else:
                # Row shapes vary by source (CCI daily rows carry close
                # only); every cell degrades to a dash instead of raising.
                def _cell(field: str, fmt: str = "{:.2f}") -> str:
                    value = bar.get(field)
                    return "—" if value is None else fmt.format(float(value))

                recent_table.add_row(
                    bar.get("date", "—"), _cell("open"), _cell("high"),
                    _cell("low"), _cell("close"), _cell("volume", "{:.0f}"),
                )
        curve_table = self.query_one("#fut-curve", DataTable)
        curve_table.clear()
        for point in item.get("curve") or []:
            curve_table.add_row(point["month"], f"{point['settle']:,.2f}")

    def _apply_quotes(self, result: dict[str, Any]) -> None:
        """Second pass: merge provider quotes into the standing board."""
        if not self.is_mounted or not result.get("ok"):
            return
        quotes = result.get("quotes") or {}
        changed = False
        for items in self._by_cat.values():
            for item in items:
                if item.get("source") != "yahoo":
                    continue
                quote = quotes.get(item.get("code"))
                if not quote:
                    continue
                if "error" in quote:
                    if item.get("error") != quote["error"]:
                        item["error"] = quote["error"]
                        changed = True
                    continue
                for field in ("last", "chg_pct", "pct5", "pct20", "volume", "recent"):
                    if field in quote:
                        item[field] = quote[field]
                item.pop("error", None)
                changed = True
        if changed:
            self._render_board()

    def _apply(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.notify(f"Futures board failed: {result.get('error')}", severity="error")
            return
        self._build_items(result)
        self._render_strip()
        self._render_board()
        adapter_error = result.get("adapter_error")
        if adapter_error:
            self.notify(f"futures_cmd adapter failed: {adapter_error}",
                        severity="warning")

    def on_unmount(self) -> None:
        timer = getattr(self, "_cci_timer", None)
        if timer is not None:
            timer.stop()

    def _cci_tick(self) -> None:
        """Overlay realtime ccidx quotes onto CCI rows while mounted."""
        if not self.is_mounted:
            return
        from .. import ccifeed

        category = self._cats[self._cat_idx] if self._cats else ""
        cci_items = [i for i in self._by_cat.get(category, [])
                     if i.get("source") == "cci"]
        if not cci_items:
            return
        feed = ccifeed.feed()
        snap = feed.snapshot
        if not snap or not feed.updated_at:
            return
        board = self.query_one("#fut-board", DataTable)
        for item in cci_items:
            live = snap.get(item["code"])
            if not live or live.get("last") is None:
                continue
            try:
                board.update_cell(item["key"], "LAST", f"{live['last']:,.2f}")
                if live.get("chg_pct") is not None:
                    board.update_cell(item["key"], "CHG%", f"{float(live['chg_pct']):+.2f}%")
            except Exception:
                continue
        try:
            self.query_one("#fut-cats", Static).update(
                self._strip_text()
                + Text(f"   CCI realtime {feed.updated_at} · {ccifeed.FEED_SOURCE}",
                       style="color(70)"))
        except Exception:
            pass

    # ── interaction ────────────────────────────────────────────────

    def _current_item(self) -> dict[str, Any] | None:
        board = self.query_one("#fut-board", DataTable)
        if board.row_count == 0 or not self._cats:
            return None
        row_key = board.coordinate_to_cell_key(board.cursor_coordinate).row_key
        if row_key is None:
            return None
        for item in self._by_cat.get(self._cats[self._cat_idx], []):
            if item["key"] == str(row_key.value):
                return item
        return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "fut-board" or event.row_key is None:
            return
        key = str(event.row_key.value)
        for item in self._by_cat.get(self._cats[self._cat_idx], []):
            if item["key"] == key and "error" not in item:
                self._render_detail(item)
                return

    def action_prev_category(self) -> None:
        if len(self._cats) > 1:
            self._cat_idx = (self._cat_idx - 1) % len(self._cats)
            self._render_strip()
            self._render_board()

    def action_next_category(self) -> None:
        if len(self._cats) > 1:
            self._cat_idx = (self._cat_idx + 1) % len(self._cats)
            self._render_strip()
            self._render_board()

    def action_chart(self) -> None:
        item = self._current_item()
        if item is None:
            self.notify("No futures row selected.", severity="warning")
            return
        if item["source"] == "yahoo":
            _open_browser_chart(self, self.config, item["code"])
            return
        if not (self.config and self.config.external_bars_root):
            self.notify(
                "Exchange-settled charts need external_bars_root configured.",
                severity="warning",
            )
            return
        _open_browser_chart(self, self.config, f"SGX:{item['code']}")

    def action_back(self) -> None:
        self.app.pop_screen()

    # Quant workbench functions, pushed directly (engine + config are all
    # the analysis screens need; the dashboard is not involved).
    def action_backtest(self) -> None:
        self.app.push_screen(BacktestScreen(self.engine, self.config))

    def action_fetch(self) -> None:
        self.app.push_screen(DataFetchScreen(self.engine, self.config))

    def action_indicators(self) -> None:
        self.app.push_screen(IndicatorsScreen(self.engine, self.config))

    def action_portfolio(self) -> None:
        self.app.push_screen(PortfolioScreen(self.engine, self.config))

class ShippingScreen(Screen):
    """Freight boards with category switching (←/→).

    FFA renders the ``mktdaily.sgx-board.v1`` settlement board from config
    ``shipping_cmd``; S&P VALUATION renders the ``stammtisch.spval-board.v1``
    board from config ``spval_cmd``. Each category only renders its own
    adapter's JSON; a fetch failure renders as a notice, never a crash.
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
        # K-line exists on the FFA board only; hidden from the footer so the
        # S&P VALUATION board never offers it (see _render_strip header).
        Binding("k", "chart", "K-line", show=False),
        # Priority: the board table would otherwise consume the arrows as
        # horizontal scrolling whenever its rows overflow the terminal.
        Binding("left", "prev_category", "Prev Board", priority=True),
        Binding("right", "next_category", "Next Board", priority=True),
    ]
    CSS = """
    ShippingScreen { layout: vertical; }
    #ship-cats { height: 1; padding: 0 1; background: #202020; }
    #ffa-wrap { layout: vertical; }
    #spval-wrap { layout: vertical; display: none; }
    #mkt-wrap { layout: vertical; display: none; }
    #risk-wrap { layout: vertical; display: none; }
    #ship-board-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #ship-detail { height: 18; layout: horizontal; }
    .ship-side { width: 1fr; border: solid #505050; background: #000000; }
    .ship-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
    #spval-top { height: 13; layout: horizontal; }
    #spval-grid-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #spval-bottom { height: 10; layout: horizontal; }
    #mkt-top { height: 1fr; layout: horizontal; }
    #mkt-right { width: 44; layout: vertical; }
    #mkt-route-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #mkt-stats-wrap { height: 12; border: solid #505050; background: #000000; }
    #risk-tail-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #risk-bottom { height: 12; layout: horizontal; }
    """

    CATEGORIES = ("FFA", "S&P VALUATION", "MARKET", "RISK")

    def __init__(self, config: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.config = config
        self._instruments: dict[str, dict[str, Any]] = {}
        self._cat_idx = 0
        self._spval_loaded = False

    def compose(self) -> ComposeResult:
        yield Static("", id="ship-header", classes="header-bar")
        yield Static("", id="ship-cats")
        with Vertical(id="ffa-wrap"):
            with Vertical(id="ship-board-wrap"):
                yield Static("  Exchange daily settlements", classes="ship-label")
                yield DataTable(id="ship-board", cursor_type="row")
            with Horizontal(id="ship-detail"):
                with Vertical(classes="ship-side"):
                    yield Static("  Forward curve", classes="ship-label")
                    yield DataTable(id="ship-curve", cursor_type="row")
                with Vertical(classes="ship-side"):
                    yield Static("  Recent front-month settles", classes="ship-label")
                    yield DataTable(id="ship-recent", cursor_type="row")
        with Vertical(id="spval-wrap"):
            with Horizontal(id="spval-top"):
                with Vertical(classes="ship-side"):
                    yield Static("  Baseline KPIs (B high-entry @ $12.00M)",
                                 classes="ship-label")
                    yield DataTable(id="spval-kpi")
                with Vertical(classes="ship-side"):
                    yield Static("  MAX-BID discipline (S3-03)", classes="ship-label")
                    yield Static("", id="spval-maxbid")
            with Vertical(id="spval-grid-wrap"):
                yield Static("  Scenario × price grid (8y: MED/P5/P-LOSS/ES10/IRR)",
                             classes="ship-label")
                yield DataTable(id="spval-grid", cursor_type="row")
            with Horizontal(id="spval-bottom"):
                with Vertical(classes="ship-side"):
                    yield Static("  Greeks (±, 8y pp)", classes="ship-label")
                    yield Static("", id="spval-greeks")
                with Vertical(classes="ship-side"):
                    yield Static("  Baseline params", classes="ship-label")
                    yield Static("", id="spval-params")
        with Vertical(id="mkt-wrap"):
            with Horizontal(id="mkt-top"):
                with Vertical(classes="ship-side"):
                    yield Static("  Charter cycle (annual TC $/d)", classes="ship-label")
                    yield DataTable(id="mkt-cycle", cursor_type="row")
                with Vertical(id="mkt-right"):
                    with Vertical(id="mkt-route-wrap"):
                        yield Static("  Route TCE 24M ($/d)", classes="ship-label")
                        yield DataTable(id="mkt-route", cursor_type="row")
                    with Vertical(id="mkt-stats-wrap"):
                        yield Static("  Key levels", classes="ship-label")
                        yield Static("", id="mkt-stats")
        with Vertical(id="risk-wrap"):
            with Vertical(id="risk-tail-wrap"):
                yield Static("  Tail matrix (8y: MED/P5/P95/P-LOSS/ES10/IRR)",
                             classes="ship-label")
                yield DataTable(id="risk-tail", cursor_type="row")
            with Horizontal(id="risk-bottom"):
                with Vertical(classes="ship-side"):
                    yield Static("  Counterfactual (8y ret)", classes="ship-label")
                    yield Static("", id="risk-cf")
                with Vertical(classes="ship-side"):
                    yield Static("  Sensitivity price x TCE (8y ret %)",
                                 classes="ship-label")
                    yield DataTable(id="risk-sens")
        yield Footer()

    def on_mount(self) -> None:
        board = self.query_one("#ship-board", DataTable)
        board.add_columns("GROUP", "CODE", "NAME", "FRONT", "SETTLE", "CHG", "CHG%", "UNIT")
        self.query_one("#ship-curve", DataTable).add_columns("MONTH", "SETTLE")
        self.query_one("#ship-recent", DataTable).add_columns("DATE", "SETTLE")
        self.query_one("#spval-kpi", DataTable).add_columns("METRIC", "VALUE")
        self.query_one("#spval-grid", DataTable).add_columns(
            "SCEN", "MED", "P5", "P-LOSS", "ES10", "IRR")
        self.query_one("#mkt-cycle", DataTable).add_columns("YEAR", "TC", "BAR")
        self.query_one("#mkt-route", DataTable).add_columns("MONTH", "TCE", "BAR")
        self.query_one("#risk-tail", DataTable).add_columns(
            "SCEN", "MED", "P5", "P95", "P-LOSS", "ES10", "IRR")
        self.query_one("#risk-sens", DataTable).add_columns(
            "P\\TCE", "7k", "10k", "13k", "16k", "20k")
        self._render_strip()
        self.action_refresh()

    # ── category switching (←/→) ────────────────────────────────────

    def _render_strip(self) -> None:
        keys = "[R] Refresh  [K] K-line  " if self._cat_idx == 0 else "[R] Refresh  "
        self.query_one("#ship-header", Static).update(
            f"  SHIPPING  |  [←→] Board  {keys}[Esc] Back")
        strip = Text()
        for index, category in enumerate(self.CATEGORIES):
            if index:
                strip.append(" ")
            label = f"  {category}  "
            strip.append(label, style="bold reverse" if index == self._cat_idx
                         else "color(160)")
        self.query_one("#ship-cats", Static).update(strip)

    WRAPS = ("#ffa-wrap", "#spval-wrap", "#mkt-wrap", "#risk-wrap")

    def _switch_category(self) -> None:
        for index, wrap in enumerate(self.WRAPS):
            self.query_one(wrap).styles.display = (
                "block" if index == self._cat_idx else "none")
        if self._cat_idx != 0 and not self._spval_loaded:
            self.action_refresh()

    def action_prev_category(self) -> None:
        if len(self.CATEGORIES) > 1:
            self._cat_idx = (self._cat_idx - 1) % len(self.CATEGORIES)
            self._render_strip()
            self._switch_category()

    def action_next_category(self) -> None:
        if len(self.CATEGORIES) > 1:
            self._cat_idx = (self._cat_idx + 1) % len(self.CATEGORIES)
            self._render_strip()
            self._switch_category()

    # ── data loading (worker thread) ───────────────────────────────

    def action_refresh(self) -> None:
        from ..domaindata import DomainDriver, validate_spval_board_v2

        if self._cat_idx == 0:
            argv = tuple(self.config.shipping_argv) if self.config else ()
            if not argv:
                self.notify("Shipping adapter is not configured (shipping_cmd).",
                            severity="warning")
                return
            _run_async(self, DomainDriver(argv).board, self._apply,
                       dedup_key="shipping-board")
        else:
            argv = tuple(self.config.spval_argv) if self.config else ()
            if not argv:
                self.notify(
                    "S&P valuation adapter is not configured (spval_cmd).",
                    severity="warning")
                return
            _run_async(self,
                       DomainDriver(argv, validator=validate_spval_board_v2).board,
                       self._apply_spval, dedup_key="spval-board")

    def _apply_spval(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.notify(f"S&P valuation board failed: {result.get('error')}",
                        severity="error")
            return
        self._spval_loaded = True
        kpis = result.get("kpis", {})
        kpi_rows = [
            ("MED RET", f"{kpis['ret_med']:+.1f}%"),
            ("P5", f"{kpis['ret_p5']:+.1f}%"),
            ("P95", f"{kpis['ret_p95']:+.1f}%"),
            ("P(LOSS)", f"{kpis['p_loss']:.1f}%"),
            ("ES10", f"{kpis['es10']:+.1f}%"),
            ("IRR MED", f"{kpis['irr_med_pct']:+.1f}%"),
            ("PAYBACK", f"{kpis['payback_med_yr']:.1f}y"),
            ("EBITDA Y1", f"${kpis['ebitda_y1_med']:,.0f}"),
        ]
        table = self.query_one("#spval-kpi", DataTable)
        table.clear()
        for metric, value in kpi_rows:
            table.add_row(metric, value)

        grid = self.query_one("#spval-grid", DataTable)
        grid.clear()
        for row in result.get("grid", []):
            grid.add_row(
                row["key"],
                f"{row['ret_med']:+.1f}",
                f"{row['ret_p5']:+.1f}",
                f"{row['p_loss']:.1f}",
                f"{row['es10']:+.1f}",
                f"{row['irr_med_pct']:+.1f}",
                key=row["key"],
            )

        mb = result.get("maxbid", {})
        self.query_one("#spval-maxbid", Static).update(
            f"  M1 {mb['m1']:.2f}  M2 {mb['m2']:.2f}  M3 {mb['m3']:.2f}  "
            f"M4 {mb['m4']:.2f}  M5 {mb['m5']:.2f}\n"
            f"  PiM = {mb['pim']:.6f}\n"
            f"  $18.000M x PiM - $0.50M(RS) - $2.89M(301)\n"
            f"  MAX-BID  ${mb['value']:,.0f}\n"
            "  walk away above +$10k | $3.00M cash floor veto"
        )

        greeks = result.get("greeks", [])
        vmax = max((max(abs(g["down"]), abs(g["up"])) for g in greeks),
                   default=1.0)
        lines = []
        for g in greeks:
            span = int(round(abs(g["down"]) / vmax * 10))
            lines.append(
                f"  {g['factor']:<14}{'#' * span:<10} "
                f"{g['down']:+.1f} / {g['up']:+.1f}")
        self.query_one("#spval-greeks", Static).update("\n".join(lines))

        base = result.get("baseline", {})
        self.query_one("#spval-params", Static).update(
            f"  price   ${base.get('price', 0) / 1e6:.2f}M\n"
            f"  scen    {base.get('scenario', '?')}\n"
            f"  OPEX    {base.get('opex', 0):,.0f}/d\n"
            f"  UTIL    {base.get('util', 0):.0f}d\n"
            f"  FFA27   {base.get('ffa', 0):,.0f}/d\n"
            f"  cover   {base.get('cover', 0) * 100:.0f}%\n"
            f"  N/seed  {base.get('n', 0):,}/{base.get('seed', '?')}"
        )

        market = result.get("market", {})
        cycle = market.get("cycle_annual_tc", {})
        cmax = max(cycle.values(), default=1.0)
        cycle_table = self.query_one("#mkt-cycle", DataTable)
        cycle_table.clear()
        for year, tc in cycle.items():
            bar = "#" * max(1, int(round(tc / cmax * 24)))
            cycle_table.add_row(str(year), f"{tc:,.0f}", bar, key=str(year))
        route = market.get("route_last24m", {})
        rmax = max(route.values(), default=1.0)
        route_table = self.query_one("#mkt-route", DataTable)
        route_table.clear()
        for month, tce in route.items():
            bar = "#" * max(1, int(round(tce / rmax * 14)))
            route_table.add_row(month[2:], f"{tce:,.0f}", bar, key=month)
        ts = market.get("tc_stats", {})
        scrap = market.get("scrap_premium", {})
        vol = market.get("vol_by_year", {})
        ou = market.get("ou", {})
        self.query_one("#mkt-stats", Static).update(
            f"  TC MED   ${ts.get('med', 0):,.0f}/d\n"
            f"  TC MAX   ${ts.get('max', 0):,.0f} {ts.get('max_date', '')}\n"
            f"  TC MIN   ${ts.get('min', 0):,.0f} {ts.get('min_date', '')}\n"
            f"  SCRAP    {scrap.get('last', 0):.2f}x (mean {scrap.get('mean', 0):.2f})\n"
            f"  VOL 26   {vol.get('2026', 0):,}\n"
            f"  VOL 21   {vol.get('2021', 0):,}\n"
            f"  OU HL    {ou.get('half_life_weeks', '?')}w\n"
            f"  LR MEAN  ${ou.get('long_run_mean_tce', 0):,}/d"
        )

        risk = result.get("risk", {})
        tail_table = self.query_one("#risk-tail", DataTable)
        tail_table.clear()
        for row in risk.get("tail_matrix", []):
            tail_table.add_row(
                row["key"],
                f"{row['ret_med']:+.1f}", f"{row['ret_p5']:+.1f}",
                f"{row['ret_p95']:+.1f}", f"{row['p_loss']:.1f}",
                f"{row['es10']:+.1f}", f"{row['irr_med_pct']:+.1f}",
                key=row["key"])
        counterfactual = risk.get("counterfactual", {})
        cfmax = max((abs(v) for v in counterfactual.values()), default=1.0)
        cf_lines = []
        for label, value in counterfactual.items():
            span = int(round(abs(value) / cfmax * 12))
            cf_lines.append(
                f"  {label[:30]:<32}{'#' * span:<12} {value:+.0f}%")
        self.query_one("#risk-cf", Static).update("\n".join(cf_lines))
        sens = risk.get("sens_matrix", {})
        sens_table = self.query_one("#risk-sens", DataTable)
        sens_table.clear()
        for price in sorted(sens, key=int):
            cols = sens[price]
            sens_table.add_row(
                f"${price}M",
                *[f"{cols[c]:+.0f}" for c in
                  ("7000", "10000", "13000", "16000", "20000")],
                key=str(price))

        asof = result.get("asof", "?")
        self.notify(f"S&P valuation board as of {asof}", severity="information")

    def _apply(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.notify(f"Shipping board failed: {result.get('error')}", severity="error")
            return
        # FFA settlements moved to the futures board (same SGX source);
        # shipping keeps only the non-FFA groups.
        instruments = [inst for inst in result.get("instruments", [])
                       if isinstance(inst, dict)
                       and not str(inst.get("group") or "").upper().startswith("FFA")]
        self._instruments = {
            inst["code"]: inst for inst in instruments
        }
        board = self.query_one("#ship-board", DataTable)
        board.clear()
        first = None
        for inst in instruments:
            code = inst["code"]
            first = first or code
            change = inst.get("change")
            change_pct = inst.get("change_pct")
            board.add_row(
                inst["group"],
                code,
                inst["name"],
                inst["front_month"],
                f"{inst['settle']:,.2f}",
                "—" if change is None else f"{change:+,.2f}",
                "—" if change_pct is None else f"{change_pct:+.2f}%",
                inst["unit"],
                key=code,
            )
        if first is not None:
            board.move_cursor(row=0)
            self._show_detail(first)
        asof = result.get("asof", "?")
        warnings = result.get("warnings") or []
        suffix = f"  |  {'; '.join(str(w) for w in warnings)}" if warnings else ""
        self.notify(f"Settlements as of {asof}{suffix}", severity="information")

    def _show_detail(self, code: str) -> None:
        inst = self._instruments.get(code) or {}
        curve = self.query_one("#ship-curve", DataTable)
        curve.clear()
        for point in inst.get("curve") or []:
            curve.add_row(point["month"], f"{point['settle']:,.2f}")
        recent = self.query_one("#ship-recent", DataTable)
        recent.clear()
        for point in inst.get("recent") or []:
            recent.add_row(point["date"], f"{point['settle']:,.2f}")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "ship-board" and event.row_key is not None:
            self._show_detail(str(event.row_key.value))

    def action_chart(self) -> None:
        if self._cat_idx != 0:
            self.notify("K-line is available on the FFA board.",
                        severity="warning")
            return
        board = self.query_one("#ship-board", DataTable)
        if board.row_count == 0:
            self.notify("No shipping row selected.", severity="warning")
            return
        row_key = board.coordinate_to_cell_key(board.cursor_coordinate).row_key
        if row_key is None or str(row_key.value) not in self._instruments:
            self.notify("No shipping row selected.", severity="warning")
            return
        if not (self.config and self.config.external_bars_root):
            self.notify(
                "Freight charts need external_bars_root configured.",
                severity="warning",
            )
            return
        _open_browser_chart(self, self.config, f"SGX:{row_key.value}")

    def action_back(self) -> None:
        self.app.pop_screen()
# Market zones in display order. Symbols classify by exchange suffix;
# anything unmatched lands in OTHER.
SECURITY_ZONES = ("A-SHARE", "HK", "US", "OTHER")
_DECISION_VERSION = 1


def _decision_symbols(config: Any, state_root: str | None = None) -> list[str]:
    """Non-cut symbols from the latest supported daily decision, if any."""
    try:
        effective_root = str(
            state_root or getattr(config, "state_root", None) or ""
        ).strip()
        base = Path(effective_root).expanduser() if effective_root else (
            Path.home() / ".local/share/stammtisch")
        payload = json.loads(
            (base / "decisions" / "latest.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict) or payload.get("decision_version") != _DECISION_VERSION:
        return []
    zones = payload.get("zones") or {}
    if not isinstance(zones, dict):
        return []
    out: list[str] = []
    for zone_data in zones.values():
        if not isinstance(zone_data, dict):
            continue
        positions = zone_data.get("positions")
        if not isinstance(positions, list):
            continue
        for position in positions:
            if not isinstance(position, dict):
                continue
            if str(position.get("action") or "").strip().lower() == "cut":
                continue
            symbol = str(position.get("symbol") or "").strip()
            if symbol:
                out.append(symbol)
    return out


def _reports_root(config: Any) -> Path | None:
    try:
        raw = str(getattr(config, "reports_root", "") or "").strip()
        return Path(raw).expanduser() if raw else None
    except Exception:
        return None


def security_watchlist(config: Any, state_root: str | None = None) -> list[str]:
    """Board names: manual watchlist, else today's decision, else recents.

    ``security_symbols`` empty is a decide.py full-market-screen signal, not
    an empty SECURITY board. Falling back keeps the TUI loadable.
    """
    from ..engine import _normalize_symbol

    if config is None:
        return []
    manual = [
        str(s).strip()
        for s in (getattr(config, "security_symbols", None) or [])
        if str(s).strip()
    ]
    seen: list[str] = []
    recents = getattr(config, "recent_symbols", None)
    recents = recents if isinstance(recents, list) else []
    source = manual if manual else _decision_symbols(
        config, state_root
    ) + [str(s) for s in recents]
    for raw in source:
        text = str(raw).strip()
        if not text:
            continue
        symbol = _normalize_symbol(text.upper())
        if symbol not in seen:
            seen.append(symbol)
    return seen


def security_zone(symbol: str) -> str:
    """Classify a provider symbol into a market zone by exchange suffix."""
    text = symbol.strip().upper()
    if text.endswith((".SS", ".SZ", ".BJ")):
        return "A-SHARE"
    if text.endswith(".HK"):
        return "HK"
    if "." not in text:
        return "US"
    return "OTHER"
class SecurityScreen(Screen):
    """Equity watchlist board with market-zone switching (←/→).

    Provider-backed symbols (config ``security_symbols``, quantkit/Yahoo
    OHLCV) group into market zones — A-SHARE / HK / US / OTHER by
    exchange suffix. A fetch failure renders per-row, never as a crash.
    The quant and daily-report hotkeys live here too, proxied to the
    dashboard actions.
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("k", "chart", "K-line"),
        Binding("g", "strategy_scan", "Strategy scan"),
        Binding("question_mark", "show_help", "Keys"),
        # Priority: the board table would otherwise consume the arrows as
        # horizontal scrolling whenever its rows overflow the terminal.
        Binding("left", "prev_zone", "Prev Zone", priority=True),
        Binding("right", "next_zone", "Next Zone", priority=True),
    ]
    CSS = """
    SecurityScreen { layout: vertical; }
    #sec-cats { height: 1; padding: 0 1; background: #202020; }
    #sec-fetch { height: 1; padding: 0 1; color: #808080; }
    #sec-board-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #sec-bottom { height: 16; layout: horizontal; }
    #sec-intel-wrap { width: 1fr; border: solid #505050; background: #000000; }
    #sec-intel { padding: 0 1; color: #c0c0c0; }
    #sec-recent-wrap { width: 1fr; border: solid #505050; background: #000000; }
    .sec-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
    """

    def __init__(self, driver, ai, engine, config, path, dashboard, **kwargs):
        super().__init__(**kwargs)
        self.driver, self.ai, self.engine, self.config = driver, ai, engine, config
        self.path = path
        self._dash = dashboard
        self._symbols: list[str] = self._resolve_symbols()
        self._zones: list[str] = []
        self._by_zone: dict[str, list[dict[str, Any]]] = {}
        self._zone_idx = 0

    def _resolve_symbols(self) -> list[str]:
        return security_watchlist(
            self.config, getattr(self.driver, "state_root", None)
        )

    def compose(self) -> ComposeResult:
        yield Static(
            "  SECURITY  |  [←→] Zone  [R] Refresh  [K] K-line  "
            "[G] Strategy scan  [?] Keys  [Esc] Back",
            classes="header-bar",
        )
        yield Static("", id="sec-cats")
        yield Static("  ", id="sec-fetch")
        with Vertical(id="sec-board-wrap"):
            yield Static("  Recommended board (daily bars + live quotes)", classes="sec-label")
            yield _RowTable(id="sec-board", cursor_type="row")
        with Horizontal(id="sec-bottom"):
            with Vertical(id="sec-intel-wrap"):
                yield Static("  Signals · sources annotated", classes="sec-label")
                yield Static("  …", id="sec-intel")
            with Vertical(id="sec-recent-wrap"):
                yield Static("  Recent", classes="sec-label")
                yield DataTable(id="sec-recent", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        from .. import signals as signals_mod

        self._signals_mod = signals_mod
        self._decision: dict[str, dict[str, Any]] = {}
        self._live: dict[str, dict[str, Any]] = {}
        board = self.query_one("#sec-board", DataTable)
        board.add_columns("CODE", "RECO", "LAST", "CHG%", "5D%", "20D%", "VOL")
        self.query_one("#sec-recent", DataTable).add_columns(
            "DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"
        )
        self._live_timer = self.set_interval(5.0, self._live_tick)
        cached = self._cached_board()
        if cached is not None:
            self._zone_idx = int(cached.get("zone_idx") or 0)
            self._restore_selected = cached.get("selected")
            self._apply({"ok": True, "quotes": cached.get("quotes") or {}}, persist=False)
            self._set_fetch_status("cached — [R] refresh")
            return
        self.action_refresh()

    # ── live quote layer (batch, one request per tick) ─────────────

    def on_unmount(self) -> None:
        timer = getattr(self, "_live_timer", None)
        if timer is not None:
            timer.stop()

    def _live_tick(self) -> None:
        """Poll live quotes for the visible zone; refreshes in-place."""
        if not self.is_mounted:
            return
        from .. import livefeed

        zone = self._zones[self._zone_idx] if self._zones else ""
        if not zone or zone not in ("A-SHARE", "US"):
            return
        symbols = [item["key"] for item in self._by_zone.get(zone, [])
                   if "error" not in item]
        if not symbols:
            return

        def _work() -> dict[str, Any]:
            return {"zone": zone,
                    "quotes": livefeed.fetch_batch(symbols),
                    "phase": livefeed.market_phase(zone),
                    "at": datetime.now().strftime("%H:%M:%S")}

        _run_async(self, _work, self._apply_live, dedup_key="security-live")

    def _apply_live(self, result: dict[str, Any]) -> None:
        from .. import livefeed

        quotes = result.get("quotes") or {}
        if not quotes:
            return
        self._live.update(quotes)
        zone = result.get("zone")
        board = self.query_one("#sec-board", DataTable)
        items = self._by_zone.get(zone, []) if zone else []
        try:
            for item in items:
                symbol = item.get("key")
                live = quotes.get(symbol)
                if not live or "error" in item:
                    continue
                row_key = item.get("key")
                last = live.get("last")
                prev = live.get("prev_close")
                if last is None or not prev:
                    continue
                board.update_cell(row_key, "LAST", f"{last:,.2f}")
                board.update_cell(row_key, "CHG%", f"{(last / prev - 1) * 100:+.2f}%")
                if live.get("volume") is not None:
                    board.update_cell(row_key, "VOL", f"{live['volume']:.0f}")
            phase = "盘中" if result.get("phase") == "open" else "闭市"
            self._set_fetch_status(
                f"行情截至 {result.get('at')} ({phase}) · {livefeed.QT_SOURCE}")
        except Exception:
            pass

    def _board_state(self) -> dict[str, Any]:
        state = getattr(self.app, "_security_board", None)
        if not isinstance(state, dict):
            state = {}
            setattr(self.app, "_security_board", state)
        return state

    def _cached_board(self) -> dict[str, Any] | None:
        state = self._board_state()
        if tuple(self._symbols) != tuple(state.get("symbols") or ()):
            return None
        quotes = state.get("quotes")
        return state if isinstance(quotes, dict) and quotes else None

    def _set_fetch_status(self, text: str) -> None:
        try:
            self.query_one("#sec-fetch", Static).update(f"  {text}")
        except Exception:
            pass

    def _post_progress(self, text: str) -> None:
        def _ui() -> None:
            if self.is_mounted:
                self._set_fetch_status(text)

        try:
            self.app.call_from_thread(_ui)
        except Exception:
            pass

    def _selected_key(self) -> str | None:
        item = self._current_item()
        return None if item is None else str(item.get("key") or "")

    # ── data loading (worker thread) ───────────────────────────────

    def action_refresh(self) -> None:
        refreshed = self._resolve_symbols()
        if refreshed != self._symbols:
            self._symbols = refreshed
            self._zone_idx = 0
        if not self._symbols:
            self.notify(
                "No configured, decided, or recent security symbols are available.",
                severity="warning",
            )
        self._set_fetch_status("fetching…")
        _run_async(self, self._load, self._apply, dedup_key="security-board")

    def _load(self) -> dict[str, Any]:
        from ..engine import _missing_ohlcv, _normalize_symbol

        quotes: dict[str, dict[str, Any]] = {}
        if not self._symbols:
            return {"ok": True, "quotes": quotes}
        engine = self.engine if isinstance(self.engine, QuantEngine) else QuantEngine.from_config(self.config)
        if not engine.available:
            quotes = {symbol: {"error": "quantkit is not installed"}
                      for symbol in self._symbols}
            return {"ok": True, "quotes": quotes}
        start = (date.today() - timedelta(days=400)).isoformat()
        total = len(self._symbols)
        for index, raw in enumerate(self._symbols, 1):
            symbol = _normalize_symbol(raw)
            self._post_progress(f"fetching {index}/{total}  {symbol}")
            result = engine.fetch_data(symbol, start=start)
            if not result.get("ok"):
                quotes[symbol] = {"error": str(result.get("error") or "no bars")[:120]}
                continue
            df = result.get("df")
            if _missing_ohlcv(df):
                quotes[symbol] = {"error": "no bars"}
                continue
            df = df.dropna(subset=["close"])
            if df.empty:
                quotes[symbol] = {"error": "no bars"}
                continue
            closes = [float(v) for v in df["close"]]
            last = closes[-1]

            def _pct(days: int, _closes: list[float] = closes,
                     _last: float = last) -> float | None:
                if len(_closes) > days and _closes[-1 - days] > 0:
                    return round((_last / _closes[-1 - days] - 1) * 100, 2)
                return None

            prev = closes[-2] if len(closes) > 1 else None
            recent = []
            for ts, row in df.tail(12).iterrows():
                recent.append({
                    "date": str(ts)[:10],
                    "open": round(float(row["open"]), 4),
                    "high": round(float(row["high"]), 4),
                    "low": round(float(row["low"]), 4),
                    "close": round(float(row["close"]), 4),
                    "volume": float(row.get("volume", 0) or 0),
                })
            quotes[symbol] = {
                "last": last,
                "chg_pct": round((last / prev - 1) * 100, 2) if prev else None,
                "pct5": _pct(5),
                "pct20": _pct(20),
                "volume": float(df["volume"].iloc[-1] or 0),
                "recent": recent,
            }
        return {"ok": True, "quotes": quotes}

    # ── row model + rendering ──────────────────────────────────────

    def _apply(self, result: dict[str, Any], persist: bool = True) -> None:
        quotes = result.get("quotes", {})
        by_zone: dict[str, list[dict[str, Any]]] = {}
        for symbol in self._symbols:
            quote = quotes.get(symbol)
            if quote is None:
                continue
            item: dict[str, Any] = {
                "key": symbol,
                "code": symbol,
                "zone": security_zone(symbol),
            }
            if "error" in quote:
                item["error"] = quote["error"]
            else:
                item.update(quote)
            by_zone.setdefault(item["zone"], []).append(item)
        self._by_zone = by_zone
        self._zones = [zone for zone in SECURITY_ZONES if zone in by_zone]
        if self._zone_idx >= len(self._zones):
            self._zone_idx = 0
        if persist:
            state = self._board_state()
            state["quotes"] = quotes
            state["symbols"] = tuple(self._symbols)
            state["zone_idx"] = self._zone_idx
            state["selected"] = getattr(self, "_restore_selected", None)
            self._set_fetch_status(f"{len(quotes)} names  ·  [R] refresh")
        self._load_decision()
        self._render_strip()
        self._render_board()

    @staticmethod
    def _fmt_pct(value: float | None) -> str:
        if value is None:
            return "—"
        return f"{value:+.2f}%"

    def _render_intel(self, zone: str, sig: dict[str, list[dict]]) -> None:
        """Fill the intel panel: zone stance + announcement signals."""
        signals_mod = getattr(self, "_signals_mod", None)
        lines: list[str] = []
        if signals_mod is not None:
            market = {"A-SHARE": "ashare", "HK": "hk", "US": "us"}.get(zone, "")
            stance = None
            try:
                reports_root = Path(getattr(self.config, "reports_root", "") or "")
                stance = signals_mod.sentiment_stance(
                    market, reports_root if str(reports_root) else None)
            except Exception:
                stance = None
            if stance:
                lines.append(
                    f"sent[{zone}] {stance.get('stance')} {stance.get('score'):+} "
                    f"({stance.get('items')}条) · 来源 {stance.get('source')} @{stance.get('date')}")
        for symbol, rows in list(sig.items())[:4]:
            latest = rows[0]
            lines.append(
                f"{latest['date']} {symbol} — {latest['title'][:46]} "
                f"[{'/'.join(latest['signals'])}]")
            lines.append(
                f"    {signals_mod.szse_search_link(symbol)} · {latest['source']}")
        if zone == "US":
            for item in (signals_mod.us_headlines(
                    _reports_root(self.config), limit=4) if signals_mod else []):
                lines.append(f"US — {item['title']}")
                lines.append(f"    {item['url']} · {item['source']}")
        if not lines:
            lines.append("(no stable source wired for this zone — nothing fabricated)")
        try:
            self.query_one("#sec-intel", Static).update(
                Text("\n".join("  " + l for l in lines)))
        except Exception:
            pass

    def _load_decision(self) -> None:
        """Latest daily decision positions keyed by symbol (RECO/WHY)."""
        try:
            base = Path(getattr(self.driver, "state_root", None)
                        or Path.home() / ".local/share/stammtisch")
            payload = json.loads(
                (base / "decisions" / "latest.json").read_text(encoding="utf-8"))
            out: dict[str, dict[str, Any]] = {}
            for zone_data in (payload.get("zones") or {}).values():
                for position in zone_data.get("positions", []):
                    out[str(position.get("symbol", "")).upper()] = {
                        "action": str(position.get("action") or "—"),
                        "thesis": str(position.get("thesis")
                                      or position.get("reason") or "")[:120],
                    }
            self._decision = out
        except Exception:
            self._decision = {}

    def _render_strip(self) -> None:
        strip = Text()
        for index, zone in enumerate(self._zones):
            if index:
                strip.append(" ")
            label = f"  {zone}  "
            strip.append(label, style="bold reverse" if index == self._zone_idx
                         else "color(160)")
        self.query_one("#sec-cats", Static).update(strip)

    def _render_board(self) -> None:
        board = self.query_one("#sec-board", DataTable)
        board.clear()
        zone = self._zones[self._zone_idx] if self._zones else ""
        items = self._by_zone.get(zone, []) if zone else []
        signals_mod = getattr(self, "_signals_mod", None)
        sig: dict[str, list[dict]] = {}
        if signals_mod is not None and zone == "A-SHARE" and items:
            try:
                sig = signals_mod.signals_for(
                    [i["key"] for i in items],
                    getattr(self.driver, "state_root", None))
            except Exception:
                sig = {}
        for item in items:
            decision = self._decision.get(item["key"]) or {}
            reco = decision.get("action") or "—"
            if sig.get(item["key"]):
                tags = {s for row in sig[item["key"]]
                        for s in row.get("signals", [])}
                if "risk" in tags and reco in ("keep", "add"):
                    reco = "trim"
            if "error" in item:
                board.add_row(item["code"], reco, item["error"], "", "", "", "",
                              key=item["key"])
                continue
            board.add_row(
                item["code"],
                reco,
                "—" if item["last"] is None else f"{item['last']:,.2f}",
                self._fmt_pct(item["chg_pct"]),
                self._fmt_pct(item["pct5"]),
                self._fmt_pct(item["pct20"]),
                f"{item['volume']:.0f}",
                key=item["key"],
            )
        self._render_intel(zone, sig)
        if board.row_count:
            restore = getattr(self, "_restore_selected", None)
            row = 0
            if restore:
                for index, item in enumerate(items):
                    if item["key"] == restore:
                        row = index
                        break
            self._restore_selected = None
            board.move_cursor(row=row)
            self._render_detail(items[row])
            state = getattr(self.app, "_security_board", None)
            if isinstance(state, dict):
                state["selected"] = items[row]["key"]
                state["zone_idx"] = self._zone_idx

    def _render_detail(self, item: dict[str, Any]) -> None:
        recent_table = self.query_one("#sec-recent", DataTable)
        recent_table.clear()
        # Newest dates first (descending)
        for bar in reversed(item.get("recent") or []):
            recent_table.add_row(
                bar["date"], f"{bar['open']:,.2f}", f"{bar['high']:,.2f}",
                f"{bar['low']:,.2f}", f"{bar['close']:,.2f}",
                f"{bar['volume']:.0f}",
            )

    def _current_item(self) -> dict[str, Any] | None:
        items = self._by_zone.get(self._zones[self._zone_idx], []) if self._zones else []
        if not items:
            return None
        board = self.query_one("#sec-board", DataTable)
        row_key = board.coordinate_to_cell_key(board.cursor_coordinate).row_key
        key = str(row_key.value) if row_key.value is not None else ""
        for item in items:
            if item["key"] == key:
                return item
        return items[0]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "sec-board":
            return
        item = self._current_item()
        if item is not None:
            self._render_detail(item)

    # ── zone switching + chart ─────────────────────────────────────

    def _step_zone(self, delta: int) -> None:
        if not self._zones:
            return
        self._zone_idx = (self._zone_idx + delta) % len(self._zones)
        state = getattr(self.app, "_security_board", None)
        if isinstance(state, dict):
            state["zone_idx"] = self._zone_idx
        self._render_strip()
        self._render_board()

    def action_prev_zone(self) -> None:
        self._step_zone(-1)

    def action_next_zone(self) -> None:
        self._step_zone(1)

    def action_chart(self) -> None:
        item = self._current_item()
        if item is None:
            self.notify("No security row selected.", severity="warning")
            return
        _open_browser_chart(self, self.config, item["code"])

    # ── workbench hotkeys proxied to the dashboard ─────────────────

    def action_back(self) -> None:
        state = getattr(self.app, "_security_board", None)
        if isinstance(state, dict):
            state["zone_idx"] = self._zone_idx
            state["selected"] = self._selected_key()
        self.app.pop_screen()

    def action_chat(self) -> None: self._dash.action_open_chat()
    def action_strategy_scan(self) -> None:
        symbols = [
            str(item.get("key") or "")
            for item in self._by_zone.get(self._zones[self._zone_idx], [])
            if item.get("key")
        ] if self._zones else []
        if not symbols:
            self.notify("This zone has no symbols to scan.", severity="warning")
            return
        self.app.push_screen(
            StrategyScanScreen(self.engine, self.config, self._zones[self._zone_idx], symbols)
        )

    def action_show_help(self) -> None:
        from .modals import KeyHelpScreen

        self.app.push_screen(KeyHelpScreen("SECURITY — KEYS", [
            ("← →", "switch market zone (A-SHARE / HK / US / OTHER)"),
            ("r", "refresh quotes"),
            ("g", "strategy scan: run the default strategy across this zone"),
            ("k", "browser K-line chart for the highlighted row"),
            ("b / f / t / p", "quant screens: backtest / fetch / indicators / portfolio"),
            ("s", "sentiment board (daily report)"),
            ("a", "ask GALAHAD"),
            ("e", "edit config"),
            ("Esc", "back to the dashboard"),
        ]))

    def action_backtest(self) -> None: self._dash.action_run_backtest()
    def action_intake(self) -> None: self._dash.action_open_intake()
    def action_config(self) -> None: self._dash.action_edit_config()
    def action_fetch(self) -> None: self._dash.action_fetch_data()
    def action_portfolio(self) -> None: self._dash.action_run_portfolio()
    def action_sentiment(self) -> None: self._dash.action_open_sentiment()
    def action_indicators(self) -> None: self._dash.action_show_indicators()


class StrategyScanScreen(Screen):
    """Strategy capability scan: the default strategy across one zone.

    One backtest per symbol (config defaults: strategy, fast/slow, cost
    tier), gates evaluated per result, rows sorted by total return so the
    first glance answers "where does this strategy actually work".
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "rescan", "Rescan"),
        Binding("a", "ask_galahad", "Ask GALAHAD"),
        Binding("question_mark", "show_help", "Keys"),
    ]
    CSS = """
    StrategyScanScreen { layout: vertical; }
    #scan-head { height: 1; padding: 0 1; background: #202020; }
    #scan-status { height: 1; padding: 0 1; color: #808080; }
    #scan-table-wrap { height: 1fr; border: solid #505050; background: #000000; }
    .scan-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
    """

    def __init__(self, engine, config, zone: str, symbols: list[str], **kwargs):
        super().__init__(**kwargs)
        self.engine = engine
        self.config = config
        self.zone = zone
        self.symbols = list(symbols)

    # ── scan parameters from the workstation defaults ──────────────

    def _params(self) -> dict[str, Any]:
        cfg = self.config or {}
        try:
            strategy = cfg.get("default_strategy") or "dual_ma"
            fast = int(cfg.get("default_fast") or 20)
            slow = int(cfg.get("default_slow") or 50)
            cost_tier = cfg.get("default_cost_tier") or "low"
        except Exception:
            strategy, fast, slow, cost_tier = "dual_ma", 20, 50, "low"
        start = (date.today() - timedelta(days=730)).isoformat()
        return {
            "strategy": str(strategy), "fast": fast, "slow": slow,
            "cost_tier": str(cost_tier), "start": start,
        }

    def compose(self) -> ComposeResult:
        p = self._params()
        yield Static(
            f"  STRATEGY SCAN  |  {self.zone}  ·  {p['strategy']} "
            f"({p['fast']}/{p['slow']})  ·  cost {p['cost_tier']}  ·  "
            f"2y window  ·  sorted by TR",
            id="scan-head",
        )
        yield Static("  starting…", id="scan-status")
        with Vertical(id="scan-table-wrap"):
            yield Static(
                "  Strategy capability by symbol (long-only)", classes="scan-label"
            )
            yield DataTable(id="scan-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#scan-table", DataTable)
        table.add_columns(
            "CODE", "TR%", "CAGR%", "SHARPE", "MAXDD%", "WIN%", "TRADES", "GATES"
        )
        self.action_rescan()

    def _status(self, text: str) -> None:
        def _ui() -> None:
            if self.is_mounted:
                try:
                    self.query_one("#scan-status", Static).update(f"  {text}")
                except Exception:
                    pass
        try:
            self.app.call_from_thread(_ui)
        except Exception:
            pass

    # ── worker ─────────────────────────────────────────────────────

    def action_rescan(self) -> None:
        _run_async(self, self._scan, self._done, dedup_key="strategy-scan")

    def _scan(self) -> dict[str, Any]:
        p = self._params()
        rows: list[dict[str, Any]] = []
        total = len(self.symbols)
        for index, symbol in enumerate(self.symbols, 1):
            self._status(f"scanning {index}/{total}  {symbol}")
            result = self.engine.run_backtest(
                symbol, strategy=p["strategy"], fast=p["fast"], slow=p["slow"],
                start=p["start"], cost_tier=p["cost_tier"],
            )
            if not result.get("ok"):
                rows.append({"symbol": symbol, "error": str(result.get("error"))[:40]})
                self._emit_row(rows[-1])
                continue
            summary = result["summary"]
            metrics = {
                "total_return": summary.total_return,
                "cagr": summary.cagr,
                "sharpe": summary.sharpe,
                "max_drawdown": summary.max_drawdown,
                "win_rate": summary.win_rate,
                "trades": summary.trades,
            }
            gates = self.engine.evaluate_gates(metrics)
            report = gates.get("report") or {}
            gates_passed = getattr(report, "n_passed", 0)
            gates_total = getattr(report, "n_total", 0)
            gates_ok = bool(getattr(report, "all_passed", False))
            row = {"symbol": symbol, **metrics,
                   "gates": f"{gates_passed}/{gates_total}",
                   "gates_ok": gates_ok}
            rows.append(row)
            self._emit_row(row)
        ok_rows = [r for r in rows if "error" not in r]
        ok_rows.sort(key=lambda r: r["total_return"], reverse=True)
        return {"ok": True, "rows": rows, "ranked": ok_rows}

    def _add_row(self, row: dict[str, Any]) -> None:
        """UI-thread row insertion; no threading wrapper inside."""
        if not self.is_mounted:
            return
        table = self.query_one("#scan-table", DataTable)
        if "error" in row:
            table.add_row(row["symbol"], f"error: {row['error']}",
                          "", "", "", "", "", "", key=row["symbol"])
        else:
            table.add_row(
                row["symbol"],
                f"{row['total_return'] * 100:+.1f}",
                f"{row['cagr'] * 100:+.1f}",
                f"{row['sharpe']:+.2f}",
                f"{row['max_drawdown'] * 100:.1f}",
                f"{row['win_rate'] * 100:.0f}",
                str(row["trades"]),
                row["gates"],
                key=row["symbol"],
            )

    def _emit_row(self, row: dict[str, Any]) -> None:
        """Worker-thread entry: hop to the UI thread for the insertion."""
        try:
            self.app.call_from_thread(self._add_row, row)
        except Exception:
            pass

    def _done(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.query_one("#scan-status", Static).update(
                f"  scan failed: {str(result.get('error'))[:120]}  ·  [R] rescan"
            )
            return
        table = self.query_one("#scan-table", DataTable)
        table.clear()
        ranked = result.get("ranked") or []
        for row in ranked:
            self._add_row(row)  # UI thread already: no hop
        errors = [r for r in result.get("rows") or [] if "error" in r]
        for row in errors:
            self._add_row(row)
        summary = (
            f"done — {len(ranked)} scanned"
            + (f", {len(errors)} failed" if errors else "")
            + "  ·  [R] rescan  ·  [?] keys"
        )
        self.query_one("#scan-status", Static).update(f"  {summary}")

    def action_ask_galahad(self) -> None:
        """Hand the ranked scan table to GALAHAD for the decision pass."""
        table = self.query_one("#scan-table", DataTable)
        if table.row_count == 0:
            self.notify("Scan something first — the table is empty.", severity="warning")
            return
        p = self._params()
        context = [
            f"Strategy scan — zone {self.zone}, {p['strategy']} "
            f"({p['fast']}/{p['slow']}), cost {p['cost_tier']}, 2y window, "
            f"sorted by total return:",
            "symbol | TR% | CAGR% | sharpe | maxdd% | win% | trades | gates",
        ]
        for row_key in table.rows:
            cells = [str(v) for v in table.get_row(row_key)]
            context.append(" | ".join(cells))
        from .chat import ChatScreen
        self.app.push_screen(ChatScreen(
            self.app.ai,
            initial_prompt=(
                "你是配置决策人。基于上面的策略扫描表,用工具核实关键标的后,"
                "给出这一区的配置决策:买什么、权重顺序、不买什么和原因、"
                "以及两个必须盯住的风险。引用数字一律以工具返回为准。"
            ),
            initial_context="\n".join(context),
        ))

    def action_show_help(self) -> None:
        from .modals import KeyHelpScreen

        p = self._params()
        self.app.push_screen(KeyHelpScreen("STRATEGY SCAN — KEYS", [
            ("a", "hand the ranked table to GALAHAD for the decision pass"),
            ("r", "rescan (re-run every symbol in this zone)"),
            ("↑ ↓", "move in the results table"),
            ("Esc", "back to the security board"),
            ("", ""),
            (f"strategy: {p['strategy']} ({p['fast']}/{p['slow']})",
             "set via default_strategy / default_fast / default_slow"),
            ("cost model", f"default_cost_tier = {p['cost_tier']}"),
            ("gates", "quantkit gate suite per result; N/M = gates passed"),
        ]))

    def action_back(self) -> None:
        self.app.pop_screen()
