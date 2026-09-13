"""In-terminal charts — text candlesticks, zero new dependencies.

A tiny block-character renderer (pure functions, unit-testable) plus a
reactive widget and a screen that sources candles from the quantkit
pipeline when available and from the free-data feed chains otherwise.
The provenance rule holds here too: the header always names the source
that produced the bars, and a free-feed chart is labeled as such.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer, Static

from .analysis import _run_async
from .widgets import CYAN, DIM, GREEN, GRAY, RED

# Volume bars use a eighth-block ramp: near-empty sessions stay visible.
_VOLUME_BLOCKS = "▁▂▃▄▅▆▇█"
_BODY_UP = "█"
_BODY_DOWN = "█"
_WICK = "│"


def _series_bounds(candles: list[dict[str, Any]]) -> tuple[float, float] | None:
    highs = [float(c["high"]) for c in candles if c.get("high") is not None]
    lows = [float(c["low"]) for c in candles if c.get("low") is not None]
    if not highs or not lows:
        return None
    return min(lows), max(highs)


def df_to_candles(df: Any) -> list[dict[str, Any]] | None:
    """Convert a quantkit OHLCV frame into candle dicts (None if unusable)."""
    if df is None or getattr(df, "empty", True):
        return None
    columns = {str(column).lower(): column for column in df.columns}
    if not {"open", "high", "low", "close"}.issubset(columns):
        return None
    candles: list[dict[str, Any]] = []
    for index, row in zip(df.index, df.itertuples(index=False)):
        try:
            candles.append({
                "time": str(index)[:10],
                "open": float(getattr(row, columns["open"])),
                "high": float(getattr(row, columns["high"])),
                "low": float(getattr(row, columns["low"])),
                "close": float(getattr(row, columns["close"])),
                "volume": float(getattr(row, columns["volume"], 0) or 0),
            })
        except (ValueError, TypeError, AttributeError):
            continue
    return candles or None


def render_candles(candles: list[dict[str, Any]], width: int, height: int) -> list[Text]:
    """Render candles as ``height`` text rows of at most ``width`` cells.

    Layout: one row of date-ish header (first/last candle time), the
    price area with per-candle bodies and wicks, one price-label row,
    and two volume rows. Up candles are green, down candles red —
    wicks carry the candle's own color. Price labels sit on the right
    edge of the price area.
    """
    if not candles:
        blank = Text("  (no candles)", style=DIM)
        return [blank] + [Text("", style=DIM)] * max(height - 1, 0)
    width = max(width, 24)
    height = max(height, 6)
    bounds = _series_bounds(candles)
    if bounds is None or bounds[1] <= bounds[0]:
        return ([Text("  (candles have no price range)", style=DIM)]
                + [Text("", style=DIM)] * max(height - 1, 0))
    low, high = bounds

    # Cell budget: 2 cells per candle + 1 gap, minus the 2-char indent
    # and the right-edge price-label block on every price row.
    label_width = 9
    indent = 2
    chart_width = width - label_width - indent
    per_candle = 3
    visible = max(1, min(len(candles), chart_width // per_candle))
    shown = candles[-visible:]
    span = high - low
    price_rows = height - 4  # header row + label row + 2 volume rows
    price_rows = max(price_rows, 3)
    label_row = price_rows  # index of the row that carries low/high labels

    def price_to_row(price: float) -> int:
        # Row 0 = high, row price_rows - 1 = low.
        return min(price_rows - 1, max(0, int((high - price) / span * price_rows)))

    max_volume = max((float(c.get("volume") or 0) for c in shown), default=0.0)

    grid_styles: list[dict[int, tuple[str, str]]] = [dict() for _ in range(price_rows)]
    for column, candle in enumerate(shown):
        base = column * per_candle
        open_ = float(candle.get("open") or candle["close"])
        close = float(candle["close"])
        high_ = float(candle.get("high") or max(open_, close))
        low_ = float(candle.get("low") or min(open_, close))
        rising = close >= open_
        style = GREEN if rising else RED
        body_char = _BODY_UP if rising else _BODY_DOWN
        top = price_to_row(max(open_, close))
        bottom = price_to_row(min(open_, close))
        for row in range(top, bottom + 1):
            for offset in (0, 1):
                grid_styles[row][base + offset] = (body_char, style)
        wick_column = base  # wick shares the first body column
        for row in range(price_to_row(high_), price_to_row(low_) + 1):
            grid_styles[row].setdefault(wick_column, (_WICK, style))

    rows: list[Text] = [Text(f"  {str(shown[0].get('time', ''))[:10]} … "
                             f"{str(shown[-1].get('time', ''))[:10]}", style=DIM)]
    last_close = float(shown[-1]["close"])
    close_row = price_to_row(last_close)
    for row in range(price_rows):
        line = Text("  ")
        cells = grid_styles[row]
        for column in range(chart_width):
            char, style = cells.get(column, (" ", DIM))
            line.append(char, style=style)
        # Right-edge labels: range extremes on their true rows, last
        # close wherever it lands (it wins the corner if they collide).
        if row == 0:
            line.append(f"{high:>9,.2f}", style=GRAY)
        elif row == label_row - 1:
            line.append(f"{low:>9,.2f}", style=GRAY)
        elif row == close_row and row not in (0, label_row - 1):
            line.append(f"{last_close:>9,.2f}", style=CYAN)
        rows.append(line)
    rows.append(Text(f"  last {last_close:,.2f}  "
                     f"{'up' if last_close >= float(shown[-1].get('open') or last_close) else 'down'}",
                     style=GRAY))

    # Two volume rows, one ramp each, aligned under the candle columns.
    for volume_row in range(2):
        line = Text("  ")
        for column, candle in enumerate(shown):
            volume = float(candle.get("volume") or 0)
            rising = float(candle.get("close") or 0) >= float(candle.get("open") or 0)
            if volume <= 0 or max_volume <= 0:
                line.append(" ", style=DIM)
                line.append(" ", style=DIM)
                continue
            level = min(7, int(volume / max_volume * 8))
            char = _VOLUME_BLOCKS[level] if volume_row == 1 else (
                _VOLUME_BLOCKS[level] if level >= 4 else " ")
            line.append(char, style=DIM if not rising else GRAY)
            line.append(" ", style=DIM)
        rows.append(line)
    return rows


class CandleChart(Widget):
    """Reactive text candlestick chart; assign ``candles`` to redraw."""

    DEFAULT_CSS = "CandleChart { height: 1fr; min-height: 8; }"

    candles: reactive[list[dict[str, Any]]] = reactive(list)

    def render(self) -> Text:
        size = self.size
        rows = render_candles(
            self.candles,
            max(size.width, 24),
            max(size.height, 8),
        )
        out = Text()
        for index, row in enumerate(rows):
            if index:
                out.append("\n")
            out.append_text(row)
        return out


class TerminalChartScreen(Screen):
    """Daily candles for one symbol, in the terminal.

    Source order: the quantkit pipeline (the verified daily path) first;
    when quantkit is absent or the symbol is unknown there, the free
    datafeeds chains answer and the header labels them explicitly as
    unverified feeds.
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]
    CSS = """
    TerminalChartScreen { layout: vertical; }
    #tc-chart-wrap { height: 1fr; border: solid #505050; background: #000000; }
    """

    def __init__(self, engine: Any, config: Any, symbol: str,
                 candles: list[dict[str, Any]] | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.engine = engine
        self.config = config
        self.symbol = symbol.strip().upper()
        self._injected = candles

    def compose(self) -> ComposeResult:
        yield Static(f"  {self.symbol}  |  loading…", id="tc-head",
                     classes="header-bar")
        with Vertical(id="tc-chart-wrap"):
            yield CandleChart(id="tc-chart")
        yield Footer()

    def on_mount(self) -> None:
        if self._injected:
            self._apply(self._injected, "caller-provided bars")
            return
        self.action_refresh()

    def action_refresh(self) -> None:
        head = self.query_one("#tc-head", Static)
        head.update(f"  {self.symbol}  |  loading…")

        def _work() -> tuple[list[dict[str, Any]], str]:
            if self.engine is not None and self.engine.available:
                result = self.engine.fetch_data(self.symbol)
                if result.get("ok"):
                    candles = df_to_candles(result.get("df"))
                    if candles:
                        return candles, "quantkit daily pipeline (verified)"
            from .datafeeds import service
            try:
                return service.daily_candles(self.symbol), (
                    "free feed chain (stooq → yahoo) — unverified")
            except Exception:
                # Crypto pairs (and anything the CSV/chart chains refuse)
                # still chart through the keyless exchange feed.
                return service.crypto_candles(self.symbol), (
                    "free feed chain (binance) — unverified")

        def _apply_wrapper(result) -> None:
            # _run_async wraps worker exceptions as {"ok": False, ...}.
            if isinstance(result, dict) and not result.get("ok", True):
                self._apply([], str(result.get("error") or "fetch failed"))
                return
            candles, source = result
            self._apply(candles, source)

        _run_async(self, _work, _apply_wrapper, dedup_key="tc-load")

    def _apply(self, candles: list[dict[str, Any]], source: str) -> None:
        try:
            self.query_one("#tc-chart", CandleChart).candles = candles
            if candles:
                last = float(candles[-1]["close"])
                head = f"  {self.symbol}  last {last:,.2f}  ·  src: {source}"
            else:
                head = f"  {self.symbol}  ·  no candles — {source}"
            self.query_one("#tc-head", Static).update(head)
        except Exception:
            pass

    def action_back(self) -> None:
        self.app.pop_screen()
