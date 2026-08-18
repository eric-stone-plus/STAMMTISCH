"""Racing plugin — HKJC historical race board rendered from wagerkit.

The configured ``racing_cmd`` prints one ``wagerkit.hkjc-board.v1`` JSON
object on stdout; this screen only renders it. Off-season discipline: the
board is built from cached official results, model probabilities always
carry their sample size, and edge/EV figures are diagnostics — never bet
advice.
"""

from __future__ import annotations

import json
from .subproc import run_bounded
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from .analysis import _run_async

BOARD_SCHEMA = "wagerkit.hkjc-board.v1"


class RacingDriver:
    """Subprocess bridge to the wagerkit race-board command."""

    def __init__(self, argv: tuple[str, ...] | list[str], timeout: int = 180):
        self.argv = tuple(argv)
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.argv)

    def board(self) -> dict[str, Any]:
        if not self.argv:
            return {"ok": False, "error": "racing command not configured (racing_cmd)"}
        out = run_bounded(list(self.argv), self.timeout, label="racing command")
        if not out["ok"]:
            return {"ok": False, "error": out["error"]}
        raw = out["stdout"].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            noise = raw or out["stderr"].strip()
            return {"ok": False, "error": f"racing output is not JSON: {noise[:200]}"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "racing output must be a JSON object"}
        if out["returncode"] != 0 or not data.get("ok"):
            return {"ok": False, "error": str(data.get("error") or f"exit code {out['returncode']}")}
        if data.get("schema") != BOARD_SCHEMA:
            return {"ok": False, "error": "racing board schema is unsupported"}
        return data


def _pct(value: Any, *, signed: bool = False) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{100 * value:+.1f}%" if signed else f"{100 * value:.1f}%"


def _num(value: Any, digits: int = 2) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:.{digits}f}"


class RacingScreen(Screen):
    """HKJC race board: meetings on the left, runners with model edge right."""

    TITLE = "  RACING  |  [R] Refresh  [N] Next race  [P] Prev race  [Esc] Back"
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("n", "next_race", "Next race"),
        Binding("p", "prev_race", "Prev race"),
    ]
    CSS = """
    RacingScreen { layout: vertical; }
    #race-body { height: 1fr; layout: horizontal; }
    #race-meetings { width: 34; border: solid #505050; background: #000000; }
    #race-runners { width: 1fr; border: solid #505050; background: #000000; }
    .race-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
    #race-status { height: 1; padding: 0 1; color: #909090; }
    """

    def __init__(self, config: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.config = config
        self._meetings: list[dict[str, Any]] = []
        self._race: dict[str, Any] = {}
        self._requested: tuple[str, str, int] | None = None

    def compose(self) -> ComposeResult:
        yield Static(self.TITLE, classes="header-bar")
        with Horizontal(id="race-body"):
            with Vertical(id="race-meetings"):
                yield Static("  Meetings", classes="race-label")
                yield DataTable(id="race-meeting-table", cursor_type="row")
            with Vertical(id="race-runners"):
                yield Static("  Runners", classes="race-label")
                yield DataTable(id="race-runner-table", cursor_type="row")
        yield Static("  diagnostics only — never bet advice", id="race-status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#race-meeting-table", DataTable).add_columns(
            "DATE", "COURSE", "RACES"
        )
        self.query_one("#race-runner-table", DataTable).add_columns(
            "NO", "HORSE", "JOCKEY", "TRAINER", "WT", "DRAW", "WIN",
            "FIN", "MODEL", "EDGE", "EV", "KELLY",
        )
        self.action_refresh()

    def _base_argv(self) -> tuple[str, ...]:
        return tuple(self.config.racing_argv) if self.config else ()

    def _load(self, requested: tuple[str, str, int] | None) -> dict[str, Any]:
        argv = self._base_argv()
        if requested is not None:
            date, course, race_no = requested
            argv = argv + ("--race", date, course, str(race_no))
        return RacingDriver(argv).board()

    def action_refresh(self) -> None:
        self._requested = None
        self.query_one("#race-status", Static).update(
            "  loading board… (first load parses the whole results cache)"
        )
        _run_async(self, lambda: self._load(None), self._apply)

    def _flat_races(self) -> list[tuple[str, str, int]]:
        flat: list[tuple[str, str, int]] = []
        for meeting in self._meetings:
            date = str(meeting.get("date") or "")
            course = str(meeting.get("course") or "")
            for race_no in range(1, int(meeting.get("races") or 0) + 1):
                flat.append((date, course, race_no))
        return flat

    def _go(self, target: tuple[str, str, int]) -> None:
        self._requested = target
        self.query_one("#race-status", Static).update(
            f"  loading {target[0]} {target[1]} R{target[2]}…"
        )
        _run_async(self, lambda: self._load(target), self._apply)

    def _step(self, delta: int) -> None:
        flat = self._flat_races()
        current = (
            str(self._race.get("date") or ""),
            str(self._race.get("course") or ""),
            int(self._race.get("race_no") or 0),
        )
        if not flat or current not in flat:
            return
        self._go(flat[(flat.index(current) + delta) % len(flat)])

    def action_next_race(self) -> None:
        self._step(1)

    def action_prev_race(self) -> None:
        self._step(-1)

    def _apply(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.query_one("#race-status", Static).update(
                f"  board failed: {result.get('error')}"
            )
            return
        self._meetings = [
            m for m in result.get("meetings", []) if isinstance(m, dict)
        ]
        self._race = result.get("race") or {}
        meetings = self.query_one("#race-meeting-table", DataTable)
        meetings.clear()
        current_row = None
        # latest meeting on top; the cursor follows the displayed race
        for row_idx, meeting in enumerate(reversed(self._meetings)):
            key = f"{meeting.get('date')}|{meeting.get('course')}"
            if (
                meeting.get("date") == self._race.get("date")
                and meeting.get("course") == self._race.get("course")
            ):
                current_row = row_idx
            meetings.add_row(
                str(meeting.get("date") or "?"),
                str(meeting.get("course") or "?"),
                str(meeting.get("races") or "?"),
                key=key,
            )
        if current_row is not None and meetings.row_count:
            meetings.move_cursor(row=current_row, animate=False)
        runners = self.query_one("#race-runner-table", DataTable)
        runners.clear()
        for runner in self._race.get("runners") or []:
            if not isinstance(runner, dict):
                continue
            runners.add_row(
                str(runner.get("no") or "?"),
                str(runner.get("horse") or "?"),
                str(runner.get("jockey") or "?"),
                str(runner.get("trainer") or "?"),
                str(runner.get("weight_lb") or "—"),
                str(runner.get("draw") or "—"),
                _num(runner.get("win_odds"), 1),
                str(runner.get("finish") or "—"),
                _pct(runner.get("model_prob")),
                _pct(runner.get("edge"), signed=True),
                _num(runner.get("ev_per_unit")),
                _pct(runner.get("kelly_stake")),
            )
        model = result.get("model") or {}
        if model.get("status") == "fitted":
            model_line = (
                f"model fitted on {model.get('races_used')} races"
                f" (walk-forward logloss {model.get('walk_forward_logloss')})"
            )
        else:
            model_line = (
                f"model off: {model.get('status', '?')}"
                f" (races available: {model.get('races_available', 0)})"
            )
        self.query_one("#race-status", Static).update(
            f"  {self._race.get('date')} {self._race.get('course')}"
            f" R{self._race.get('race_no')}  |  {model_line}"
            "  |  diagnostics only — never bet advice"
        )

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        """Clicking/Enter on a meeting jumps to that meeting's last race."""
        if event.data_table.id != "race-meeting-table":
            return
        date, _, course = str(event.row_key.value).partition("|")
        meeting = next(
            (
                m
                for m in self._meetings
                if str(m.get("date")) == date and str(m.get("course")) == course
            ),
            None,
        )
        races = int(meeting.get("races") or 0) if meeting else 0
        if races < 1:
            return
        self._go((date, course, races))

    def action_back(self) -> None:
        self.app.pop_screen()


class CasinoScreen(RacingScreen):
    """CASINO module — currently hosts the wagerkit race board."""

    TITLE = "  CASINO  |  RACING BOARD  |  [R] Refresh  [N] Next race  [P] Prev race  [Esc] Back"
