"""The SENTIMENT screen (``:sentiment``; route ``sentiment``) — one-shot.

Absorbs the old SentimentScreen (``tui/screens/brief.py:232`` + the
``tui/tape.py`` desk tape, read-only references — never imported): the
market-wide sentiment tape of the latest indexed daily report, with
``←``/``→`` navigation across the report history. The data contract
(report load, tape scoring/formatting, history index) lives in
:mod:`interface.services.sentiment`.

Discipline and deviations:

- one-shot by nature: a daily report changes when the intake product
  writes it, so the screen loads on entry and on day switches — no
  spine poll (unlike the live tapes). ``r`` re-runs the current day.
- ``[O]`` (the old GALAHAD report-analysis handoff) notifies "M7" —
  the chat/analysis lane is deliberately deferred with the write-path
  discipline (RETIREMENT Option B).
- the loaders are constructor-injected; the defaults resolve lazily at
  call time against the old reports-root resolution order (explicit >
  ``STAMMTISCH_REPORTS`` > conventional). The report prose renders
  verbatim — it is data, not chrome.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from interface.app.spine import SlowLane
from interface.render.tokens import token

__all__ = ["SentimentScreen"]


class SentimentScreen(Screen[None]):
    """Market tape + day history over the indexed daily reports."""

    route_name = "sentiment"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back to the previous screen"),
        # Priority: the scrollable body would otherwise consume the
        # arrows for scrolling (the old screen did the same).
        Binding("left", "prev_day", "Previous indexed report day",
                priority=True),
        Binding("right", "next_day", "Next indexed report day",
                priority=True),
        Binding("r", "reload", "Reload the current day's report"),
        Binding("o", "open_report", "GALAHAD report analysis (deferred)"),
        Binding("j", "scroll_down", "Scroll the tape down"),
        Binding("k", "scroll_up", "Scroll the tape up"),
    ]
    CSS = """
    SentimentScreen { layout: vertical; }
    #sent-header { height: auto; color: $text-primary; padding: 0 1; }
    #sent-scroll {
        height: 1fr;
        border: round $panel-border;
        border-title-color: $panel-title;
        padding: 0 2;
        background: $surface;
        color: $text-primary;
    }
    """

    def __init__(self, load_doc: Callable[[str], Any] | None = None,
                 list_history: Callable[[], Any] | None = None) -> None:
        super().__init__()
        self._load_doc = load_doc
        self._list_history = list_history
        self._lane = SlowLane()
        self._entries: tuple[Any, ...] | None = None
        self._index: int = 0
        self.doc: dict[str, Any] | None = None
        #: Test observability: docs landed, days visited.
        self.docs_landed = 0
        self.visited_days: list[str] = []

    # -- the service seams (injection only; no import-time transport) ----

    def _list_entries(self) -> tuple[Any, ...]:
        if self._list_history is not None:
            return tuple(self._list_history())
        from interface.services.sentiment import history_entries

        return history_entries()

    def _load_path(self, entry: Any) -> Any:
        if self._load_doc is not None:
            return self._load_doc(entry.json_path)
        from interface.services.sentiment import load_daily_path

        return load_daily_path(entry.json_path,
                               html_path=entry.html_path or None,
                               expected_date=entry.report_date)

    def compose(self):
        yield Static("", id="sent-header")
        with VerticalScroll(id="sent-scroll"):
            yield Static(Text("loading the latest report…",
                              style=token("text.muted")), id="sent-body")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#sent-scroll", VerticalScroll).border_title = "TAPE"
        self._open_index(0)

    # -- loading (SlowLane: never overlap, drop stale) ----------------------

    def _open_index(self, index: int) -> None:
        if self._entries is None:
            try:
                self._entries = self._list_entries()
            except Exception:  # noqa: BLE001 - degrade to no history
                self._entries = ()
        if not self._entries:
            self._render_error("no indexed report history "
                               "(no daily reports found)")
            return
        self._index = max(0, min(len(self._entries) - 1, index))
        entry = self._entries[self._index]
        self._lane.run(self, lambda: self._load_path(entry), self._deliver,
                       name="sentiment-report")

    def _deliver(self, doc: Any) -> None:
        self.docs_landed += 1
        if not doc.get("ok"):
            self._render_error(str(doc.get("error")
                                   or "report JSON is invalid"))
            return
        self.doc = doc
        self.visited_days.append(str(doc.get("date") or ""))
        self._render_doc()

    def _render_doc(self) -> None:
        from interface.services.sentiment import desk_sentiment

        assert self.doc is not None
        body = desk_sentiment(self.doc)
        text = Text(body if body.strip()
                    else "  No daily report or sentiment tape is available.")
        self.query_one("#sent-body", Static).update(text)
        header = Text()
        header.append("SENTIMENT ", style=f"bold {token('panel.title')}")
        header.append(str(self.doc.get("date") or "?"),
                      style=token("state.info"))
        header.append("  ALL MARKETS", style=token("text.muted"))
        if self._entries:
            # Counted from the OLDEST report: ← walks toward day 1.
            header.append(
                f"  day {len(self._entries) - self._index}"
                f"/{len(self._entries)}",
                style=token("text.muted"))
        model = str(self.doc.get("model") or "")
        if model:
            header.append(f"  via {model}", style=token("provenance.src"))
        self.query_one("#sent-header", Static).update(header)

    def _render_error(self, message: str) -> None:
        self.query_one("#sent-body", Static).update(
            Text(f"[ERROR] {message}", style=token("state.crit")))
        header = Text()
        header.append("SENTIMENT ", style=f"bold {token('panel.title')}")
        header.append("(READ-ONLY)", style=token("text.muted"))
        self.query_one("#sent-header", Static).update(header)

    # -- day navigation (old brief.py:321-348) ------------------------------

    def _switch_day(self, delta: int) -> None:
        """← = one day OLDER, → = one day newer.

        The entries are newest-first, so "older" walks toward higher
        indices; the clamp keeps both ends honest.
        """
        if not self._entries:
            self.notify("no indexed report history", severity="warning")
            return
        target = max(0, min(len(self._entries) - 1,
                            self._index - delta))
        if target == self._index:
            self.notify("Oldest report." if delta < 0 else "Newest report.")
            return
        self._open_index(target)

    def action_prev_day(self) -> None:
        self._switch_day(-1)

    def action_next_day(self) -> None:
        self._switch_day(1)

    # -- bindings -----------------------------------------------------------

    def action_reload(self) -> None:
        if self._lane.in_flight:
            self.notify("a load is already in flight", severity="warning")
            return
        self._open_index(self._index)

    def action_open_report(self) -> None:
        """The old [O] GALAHAD handoff — deferred to M7 (notify, no-op)."""
        self.notify("GALAHAD report analysis lands in M7",
                    severity="information")

    def action_scroll_down(self) -> None:
        self.query_one("#sent-scroll", VerticalScroll).scroll_down(
            animate=False)

    def action_scroll_up(self) -> None:
        self.query_one("#sent-scroll", VerticalScroll).scroll_up(
            animate=False)

    def action_back(self) -> None:
        self.app.pop_screen()
