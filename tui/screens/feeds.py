"""FEEDS panel — live health of the free-data feed provider chains."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ..datafeeds import cache as dfcache
from ..datafeeds import journal as dfjournal
from ..datafeeds.registry import all_stats


class FeedHealthScreen(Screen):
    """Per-provider ok/failed/latency/last-error, plus cache and tape state.

    The counters accumulate for the life of the process — they answer
    "which source is serving me right now" (and which one silently died)
    for the fallback chains wired through tui/datafeeds.
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]
    CSS = """
    FeedHealthScreen { layout: vertical; }
    #feed-table-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #feed-cache { height: 2; padding: 0 1; color: #808080; }
    """

    def __init__(self, config: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.config = config

    def compose(self) -> ComposeResult:
        yield Static(
            "  FEEDS  |  provider health for the free-data chains  |  "
            "[R] Refresh  [Esc] Back",
            classes="header-bar",
        )
        with Vertical(id="feed-table-wrap"):
            yield DataTable(id="feed-table", cursor_type="row")
        yield Static("", id="feed-cache")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#feed-table", DataTable)
        table.add_columns("PROVIDER", "OK", "FAIL", "AVG ms", "LAST SUCCESS", "LAST ERROR")
        self.action_refresh()

    def action_refresh(self) -> None:
        table = self.query_one("#feed-table", DataTable)
        table.clear()
        for entry in sorted(all_stats(), key=lambda item: item.name):
            table.add_row(
                entry.name,
                str(entry.ok),
                str(entry.failed),
                "—" if entry.avg_latency_ms is None else f"{entry.avg_latency_ms:.0f}",
                (entry.last_success or "—").replace("T", " "),
                entry.last_error or "—",
            )
        stats = dfcache.cache_stats()
        root = getattr(self.config, "state_root", None) if self.config else None
        tape_note = ""
        if root:
            tape_note = f" | quote tape: intel/quotes/{dfjournal.day_file(root).name}"
        self.query_one("#feed-cache", Static).update(
            f"  cache: {stats['entries']} fresh / {stats['stale_entries']} stale"
            f" | disk snapshots: {stats['disk_dir'] or 'off'}{tape_note}"
        )

    def action_back(self) -> None:
        self.app.pop_screen()
