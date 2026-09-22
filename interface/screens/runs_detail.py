"""The full run detail screen (``:detail <run-id>``; route ``detail``).

Distinct from the M1 Enter modal (:class:`RunDetailScreen` in
:mod:`interface.screens.overview`, one frozen frame): this screen stays
open and re-renders from the spine as the run evolves, and renders the
grilling-adjudicated honest fields — the cost block's unit/token_total/
wall_s/usage rows (em dash for legal nulls) and the gates' record
digests.

Corrupt-run policy (grilling S4): ``run.error`` renders prominently at
the top and stages/gates render their honest empty placeholders — never
fake stages. A run id that leaves the frame entirely says so instead of
inventing content.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from interface.render import panels
from interface.render.tokens import token
from interface.snapshot import WorkstationSnapshot

__all__ = ["RunsDetailScreen"]


class RunsDetailScreen(Screen[None]):
    """A streaming, full-screen inspector for one run id."""

    route_name = "detail"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back to the previous screen"),
    ]
    CSS = """
    RunsDetailScreen { layout: vertical; }
    #rd-body {
        border: round $panel-border;
        border-title-color: $panel-title;
        padding: 0 2;
        background: $surface;
        color: $text-primary;
    }
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self._was_present = True
        #: Test observability: frames applied, and the last rendered run.
        self.update_count = 0
        self.last_run = None

    def compose(self):
        with VerticalScroll():
            yield Static(Text("waiting for the first frame…",
                              style=token("text.muted")), id="rd-body")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#rd-body", Static).border_title = (
            f"RUN DETAIL · {self.run_id}")
        self.app.spine.subscribe(self)
        frame = self.app.spine.last_frame
        if frame is not None:
            self.spine_frame(frame, frozenset({"runs"}))

    def on_unmount(self) -> None:
        self.app.spine.unsubscribe(self)

    def spine_staleness(self, levels: dict[str, str]) -> None:
        """Mirror the spine's staleness verdict into the border title."""
        level = levels.get("runs", "fresh")
        title = Text(f"RUN {self.run_id}", style=f"bold {token('panel.title')}")
        if level == "warn":
            title.append("  \u25b2 stale?", style=token("stale.warn"))
        elif level == "crit":
            title.append("  \u25a0 STALE", style=f"bold {token('stale.crit')}")
        try:
            self.query_one("#rd-body", Static).border_title = title
        except Exception:  # noqa: BLE001, S110 - unmount race; cosmetic
            pass

    def spine_frame(self, frame: WorkstationSnapshot,
                    due: frozenset[str]) -> None:
        """Re-render when the runs table (or feed) was refreshed."""
        if not ({"runs", "feed"} & due):
            return
        run = next((r for r in frame.runs if r.id == self.run_id), None)
        body = self.query_one("#rd-body", Static)
        if run is None and frame.collector_error and self._was_present:
            # Review C-M3: a collector-error frame carries ZERO runs — that
            # is not "the run rotated away". Keep the last good detail and
            # say what actually happened; the staleness badge (below) shows
            # the data age.
            self._was_present = False
            body.update(Text(
                f"collector error — showing last good detail:\n"
                f"{frame.collector_error}",
                style=token("state.warn")))
            self.update_count += 1
            return
        if run is None:
            if self._was_present:
                self.notify(f"run {self.run_id} left the current frame",
                            severity="warning")
            self._was_present = False
            body.update(Text(
                f"run {self.run_id} is not in the current frame "
                "(finished and rotated away, or the id was mistyped)",
                style=token("state.warn")))
            self.update_count += 1
            return
        self._was_present = True
        self.last_run = run
        body.update(panels.runs_detail_body(run))
        self.update_count += 1

    def action_back(self) -> None:
        self.app.pop_screen()
