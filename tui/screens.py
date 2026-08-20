"""Screens — dashboard-first workstation."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, timedelta
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

from .driver import StammtischDriver
from .ai_driver import AIDriver, ChatResponse
from .engine import QuantEngine
from .analysis import DataFetchScreen, BacktestScreen, IndicatorsScreen, PortfolioScreen, GatesScreen
from .analysis import _run_async
from .widgets import (
    GRAY, DIM, GREEN, AMBER, RED, CYAN, WHITE,
    status_badge, DigestWidget, EventTimeline, GateCard,
    StageFlowWidget, SystemHud,
)

import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Dashboard — the main screen, always shown first
# ═══════════════════════════════════════════════════════════════════


class PipelineList(OptionList):
    """Sidebar option list with launcher semantics.

    Click mapping is left to the base OptionList, which locates options
    through per-option strip meta and is therefore border/padding aware —
    a hand-rolled row lookup here predated that and selected the row
    below once the default list style gained a top border. Enter is bound
    on the list itself so the dashboard's own Enter binding does not win
    when the list is focused.

    The selection highlight is transient after an entry fires, but a
    keyboard highlight survives an unrelated mouse leave.  Textual already
    tracks its hover row separately, so clearing ``highlighted`` on leave
    destroys keyboard navigation rather than clearing hover state.
    """

    BINDINGS = [Binding("enter", "select", "Edit pipeline")]

    def action_select(self) -> None:
        super().action_select()
        # The OptionSelected message has already captured the option;
        # dropping the highlight here leaves no stale selection when the
        # pushed screen is popped.
        self.highlighted = None


GENERAL_ITEMS: list[tuple[str, str, str]] = [
    ("screen.open_chat", "A", "quick.ask"),
    ("screen.edit_config", "E", "quick.config"),
    ("screen.open_crawlers", "C", "quick.crawlers"),
]


class DashboardScreen(Screen):
    """Full dashboard with panels."""

    BINDINGS = [
        Binding("a", "open_chat", "Ask"),
        Binding("c", "open_crawlers", "Crawlers", show=False),
        Binding("e", "edit_config", "Edit"),
        Binding("delete", "delete_selected", "Delete"),
        Binding("shift+d", "delete_all", "Delete all"),
        Binding("ctrl+a", "check_all", "Select all", show=False, priority=True),
        Binding("shift+up", "extend_up", "Extend", show=False, priority=True),
        Binding("shift+down", "extend_down", "Extend", show=False, priority=True),
        Binding("q", "quit", "Quit"),
    ]

    CSS = """
    DashboardScreen { layout: vertical; }
    #dash-status { height: auto; }
    #dash-main { height: 1fr; layout: horizontal; }
    #dash-left { width: 1fr; min-width: 50; }
    #dash-right { width: 42; min-width: 32; }
    #run-table-wrap { height: 1fr; border: solid #505050; }
    #run-table-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
    #help-hint { height: 1; color: #606060; }
    """

    driver: StammtischDriver
    ai: AIDriver
    engine: QuantEngine
    config: Any

    def __init__(self, driver: StammtischDriver, ai: AIDriver, engine: QuantEngine, config: Any,
                 casino: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.driver = driver
        self.ai = ai
        self.engine = engine
        self.config = config
        self._runs: list[dict[str, Any]] = []
        self._checked: set[str] = set()
        self._select_anchor: str = ""
        self._last_intake_signature: tuple[str, str] | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="dash-status")
        with Horizontal(id="dash-main"):
            with Vertical(id="dash-left"):
                with Vertical(id="run-table-wrap"):
                    yield Static(self._chrome_text("registry.label", ""), id="run-table-label")
                    yield RegistryTable(id="run-table", cursor_type="row")
            with Vertical(id="dash-right"):
                yield SystemHud(id="sys-hud")
                with Vertical(classes="panel"):
                    yield Static("  Plugins", classes="panel-title")
                    yield PipelineList(id="pipeline-list")
                with Vertical():
                    yield Static("  Quick Start", classes="panel-title")
                    yield PipelineList(
                        *[
                            Option(
                                Text(f"[{key}] {self._chrome_text(label_key, key)}"),
                                id=action,
                            )
                            for action, key, label_key in GENERAL_ITEMS
                        ],
                        id="quick-list",
                    )
                yield Static(
                    "  " + self._chrome_text("help.hint", "[?] HELP"),
                    id="help-hint",
                )

    @property
    def _language(self) -> str:
        try:
            language = str(self.config.get("language", "en") or "en")
        except Exception:
            language = "en"
        return "zh" if language == "zh" else "en"

    def _chrome_text(self, key: str, fallback: str) -> str:
        from .lang import tr

        return tr(self._language, key, fallback)

    def _quick_options(self) -> list[Option]:
        """Quick Start rows for the current language."""
        from .lang import tr

        language = self._language
        return [
            Option(Text(f"[{hotkey}] {tr(language, label_key, hotkey)}"), id=action)
            for action, hotkey, label_key in GENERAL_ITEMS
        ]

    def _rebuild_quick(self) -> None:
        """Rebuild the Quick Start rows (Textual 8.2.8 options are
        immutable; replacing the set is the supported refresh)."""
        quick = self.query_one("#quick-list", OptionList)
        quick.clear_options()
        for option in self._quick_options():
            quick.add_option(option)
        quick.highlighted = None

    def _relabel(self) -> None:
        """Re-render language-dependent chrome in place."""
        from .lang import tr

        language = self._language
        try:
            self._rebuild_quick()
        except Exception:
            pass
        try:
            self.query_one("#run-table-label", Static).update(
                tr(language, "registry.label")
            )
        except Exception:
            pass
        try:
            self.query_one("#help-hint", Static).update(
                "  " + tr(language, "help.hint", "[?] HELP")
            )
        except Exception:
            pass

    def on_mount(self) -> None:
        # No pre-selected row in Quick Start: hover feedback only on demand.
        self.query_one("#quick-list", OptionList).highlighted = None
        self._relabel()
        table = self.query_one("#run-table", DataTable)
        table.add_columns("SESSION", "TIME", "STATE")
        self.set_interval(2.0, self._refresh_intake_rows)
        self.action_refresh()

    def action_open_crawlers(self) -> None:
        from .crawlers import CrawlerPanelScreen

        self.app.push_screen(CrawlerPanelScreen(self.config))

    def _intake_session_rows(self) -> list[dict[str, str]]:
        from .intake_job import list_sessions, supervisor_for

        root = ""
        if self.config:
            root = str(self.config.workspace_root or "")
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        live = supervisor_for(self.app).snapshot()
        if live is not None:
            sid = str(live.get("id") or "")
            if sid:
                seen.add(sid)
                stamp = str(live.get("updated_at") or live.get("started_at") or "")
                created = stamp[:10]
                name, when = _intake_session_parts(live)
                rows.append({
                    "title": name,
                    "when": when,
                    "state": str(live.get("state") or "capturing"),
                    "created": created,
                    "key": f"intake:{sid}",
                    "sort": stamp,
                })
        if root:
            for session in list_sessions(root):
                sid = str(session.get("id") or "")
                if not sid or sid in seen:
                    continue
                stamp = str(session.get("updated_at") or session.get("started_at") or "")
                created = stamp[:10]
                name, when = _intake_session_parts(session)
                state = str(session.get("state") or "unknown")
                if state == "capturing":
                    # Only the live supervisor session can still be
                    # capturing; a `capturing` row on disk belongs to an
                    # app that died mid-capture. Show it as interrupted
                    # instead of a capture that will never finish.
                    state = "interrupted"
                rows.append({
                    "title": name,
                    "when": when,
                    "state": state,
                    "created": created,
                    "key": f"intake:{sid}",
                    "sort": stamp,
                })
        return rows

    def _registry_rows(self, runs: list[dict[str, Any]]) -> list[dict[str, str]]:
        rows = list(self._intake_session_rows())
        for run in runs:
            created_raw = str(run.get("created_at") or "")
            created = created_raw.split("T")[0] if "T" in created_raw else created_raw
            run_id = str(run.get("_full_id") or run.get("run_id") or "")
            name, when = run_session_parts(run)
            rows.append({
                "title": name,
                "when": when,
                "state": str(run.get("state") or "?"),
                "created": created,
                "key": run_id,
                "sort": created_raw or run_id,
            })
        rows.sort(key=lambda item: item.get("sort") or "", reverse=True)
        return rows

    def _paint_registry(self, runs: list[dict[str, Any]]) -> None:
        table = self.query_one("#run-table", DataTable)
        cursor = self._cursor_run_id() if table.row_count else ""
        table.clear()
        rows = self._registry_rows(runs)
        valid = {item["key"] for item in rows if item.get("key")}
        self._checked &= valid
        try:
            self.query_one("#sys-hud", SystemHud).run_count = len(rows)
        except Exception:
            pass
        for item in rows:
            table.add_row(
                item["title"],
                item.get("when") or item.get("created") or "",
                status_badge(item["state"]),
                key=item["key"],
            )
        if cursor:
            for index, item in enumerate(rows):
                if item["key"] == cursor:
                    table.move_cursor(row=index)
                    break
        if isinstance(table, RegistryTable):
            table.refresh_selection()

    def _refresh_intake_rows(self) -> None:
        job = getattr(self.app, "intake_supervisor", None)
        if job is None or not self.is_mounted:
            return
        capturing = getattr(job, "capturing", False)
        live = job.snapshot() if capturing else None
        signature = (
            (str(live.get("id")), str(live.get("state"))) if live else None
        )
        # Repaint on transitions (capture start and completion) instead of
        # every tick: a full-table repaint plus the session-dir walk it
        # triggers is wasted work while a capture quietly progresses.
        # Completion repaints once so the final accepted/rejected row is
        # visible without a manual refresh.
        if signature == getattr(self, "_last_intake_signature", None):
            return
        self._last_intake_signature = signature
        self._paint_registry(list(self._runs))

    def action_refresh(self) -> None:
        hud = self.query_one("#sys-hud", SystemHud)
        hud.state_root = self.driver.state_root or "(not set)"
        hud.initialized = self.driver.is_initialized()
        hud.lock_status = "NOT HELD"
        hud.ai_status = "ONLINE" if self.ai.available else "NO API KEY"
        # The disk walk scales with the state root: run it in a worker and
        # patch the HUD when it lands (stale results are dropped by the
        # generation guard in _run_async).

        def _apply_disk(usage):
            if isinstance(usage, str):
                self.query_one("#sys-hud", SystemHud).disk_usage = usage

        self._run_async(hud.compute_disk_usage, _apply_disk)

        status = self.query_one("#dash-status", Static)
        hints = []
        if not self.engine.available:
            hints.append("quantkit not installed — quant commands disabled "
                         "(pip install -e <quantkit>)")
        if not self.ai.available:
            hints.append("AI API key not set. Press [E]dit or run: "
                         "stammtisch config set-key")
        elif not self.driver.is_initialized():
            hints.append("State root not initialized. Press [I] to initialize.")

        def _apply_runs(result):
            runs, registry_error = result
            if registry_error:
                hints.append(f"Run registry unavailable: {registry_error}")
            status.update("  " + "  |  ".join(hints) if hints else "")
            self._runs = list(runs)
            self._paint_registry(self._runs)

        # The registry walk spawns the core CLI; keep it off the UI thread
        # so a slow state root or a wedged subprocess cannot freeze the TUI.
        self._run_async(self._load_runs, _apply_runs)

        pl = self.query_one("#pipeline-list", OptionList)
        pipelines = self.driver.list_pipelines()
        # One alphabetical list: pipeline workbenches, the built-in domain
        # modules, and configured domain plugins share the same ordering.
        # fullstack.json is the example end-to-end pipeline, not a
        # homepage workbench. Keep it off the sidebar.
        entries: list[tuple[str, str]] = [
            (p.stem.upper(), str(p))
            for p in pipelines
            if p.stem.lower() != "fullstack"
        ]
        entries += [("CRYPTO", "domain:CRYPTO"), ("ENERGY", "domain:ENERGY")]
        entries += [
            (plugin["label"], f"domain:{plugin['label']}")
            for plugin in (self.config.plugins if self.config else [])
        ]
        pl.clear_options()
        for label, option_id in sorted(entries, key=lambda entry: entry[0]):
            pl.add_option(Option(label, id=option_id))
        if not entries:
            pl.add_option(Option("(none)", id=""))

    def _load_runs(self) -> tuple[list[dict[str, Any]], str | None]:
        """Load the registry from the event-folding core only.

        A failed ``status`` is an integrity/availability result, not an
        invitation to trust per-run manifest projections directly.
        """
        result = self.driver.status()
        if not result.ok:
            return [], result.error_message or "status failed"
        runs = result.data.get("runs")
        if not isinstance(runs, list) or any(
            not isinstance(run, dict) for run in runs
        ):
            return [], "status returned an invalid run registry"
        return runs, None


    def _run_async(self, target, callback, drop_stale: bool = True,
                   dedup_key: str | None = None) -> None:
        """Run target() in a background thread, deliver on the UI thread.

        Same guards as analysis._run_async: worker exceptions become error
        results and callbacks never fire after the dashboard was popped or
        the app quit. drop_stale=True (default) drops stale submissions via
        a generation counter — right for refreshes; one-shot user actions
        whose result must always surface pass drop_stale=False (a resume
        refresh would otherwise bump the generation and swallow them).

        dedup_key: when given, only one target per key may be in flight —
        repeated submissions with the same key while it runs are dropped
        instead of stacking identical workers (heavy commands like a domain
        board fetch would otherwise pile up under rapid refreshes).
        """
        gen = getattr(self, "_async_gen", 0) + 1
        self._async_gen = gen
        inflight = getattr(self, "_async_inflight", None)
        if inflight is None:
            inflight = {}
            self._async_inflight = inflight
        if dedup_key is not None:
            if dedup_key in inflight:
                return
            inflight[dedup_key] = gen

        def _worker():
            try:
                try:
                    result = target()
                except Exception as e:
                    result = {"ok": False, "error": str(e)}
            finally:
                if dedup_key is not None:
                    inflight.pop(dedup_key, None)

            def _deliver():
                if not self.is_mounted:
                    return
                if drop_stale and getattr(self, "_async_gen", 0) != gen:
                    return
                try:
                    callback(result)
                except Exception:
                    # display helpers must never crash the UI thread, but a
                    # swallowed bug makes refreshes silently do nothing —
                    # log it instead of vanishing it.
                    logger.exception("async callback failed")
                    self.notify("refresh failed; see log", severity="error")

            try:
                self.app.call_from_thread(_deliver)
            except Exception:
                logger.exception("call_from_thread failed (app torn down?)")

        threading.Thread(target=_worker, daemon=True).start()


    # ── Keyboard actions ───────────────────────────────────────────

    def action_new_run(self) -> None:
        self.app.push_screen(PipelineRunScreen(self.driver, self.ai))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on the run table inspects the selected run."""
        self.action_inspect_run()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # A selection that was queued before its screen was pushed (the
        # second half of a double-click) must not push another screen on
        # top of it.
        if self.app.screen is not self:
            return
        option_id = event.option_id
        if not option_id:
            return
        if option_id.startswith("screen."):
            method = getattr(self, f"action_{option_id[len('screen.'):]}", None)
            if method is not None:
                method()
            return
        if option_id == "domain:CRYPTO":
            self.action_open_crypto()
            return
        if option_id == "domain:ENERGY":
            self.action_open_energy()
            return
        if option_id.startswith("domain:"):
            label = option_id[len("domain:"):]
            # Configured domains open their dedicated screens; every other
            # plugin keeps the read-only directory browser.
            if label == "FUTURES" and self.config and (
                self.config.futures_symbols or self.config.futures_argv
            ):
                self.app.push_screen(FuturesScreen(self.engine, self.config))
                return
            if label == "SHIPPING" and self.config and self.config.shipping_argv:
                self.app.push_screen(ShippingScreen(self.config))
                return
            if label == "CASINO" and self.config and self.config.racing_argv:
                from .racing import CasinoScreen

                self.app.push_screen(CasinoScreen(self.config))
                return
            root = next(
                (p["root"] for p in (self.config.plugins if self.config else [])
                 if p["label"] == label),
                None,
            )
            if root:
                self.app.push_screen(DomainBrowserScreen(label, root))
            return
        if Path(option_id).stem == "security":
            self.app.push_screen(
                SecurityScreen(self.driver, self.ai, self.engine, self.config, option_id, self)
            )
            return
        self.app.push_screen(PipelineViewScreen(self.driver, option_id))

    def action_inspect_run(self) -> None:
        table = self.query_one("#run-table", DataTable)
        if table.row_count == 0:
            self.notify("No runs found. Press [N] to create one.", severity="warning")
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        run_id = str(row_key.value) if row_key.value is not None else ""
        if not run_id:
            self.notify("No run selected. Use arrow keys to select.", severity="warning")
            return
        if run_id.startswith("intake:"):
            self.app.push_screen(
                DailyIntakeScreen(self.config, session_id=run_id.split(":", 1)[1])
            )
            return
        self.app.push_screen(RunInspectorScreen(self.driver, self.ai, run_id))

    def action_init_state(self) -> None:
        # Initialization writes through the core CLI; run it off the UI
        # thread and refresh only after the result lands (one-shot action,
        # so the stale guard must not drop it).
        def _apply_init(r):
            self.notify(f"Initialized: {r.data.get('state_root', '?')}" if r.ok else f"Error: {r.error_message}",
                        severity="information" if r.ok else "error")
            self.action_refresh()

        self._run_async(self.driver.init, _apply_init, drop_stale=False)

    def _cursor_run_id(self) -> str:
        table = self.query_one("#run-table", DataTable)
        if table.row_count == 0:
            return ""
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return str(row_key.value) if row_key.value is not None else ""

    def on_registry_row_click(self, row_key: str, *, shift: bool) -> None:
        """Plain click selects one row. Shift+click selects the range from the anchor."""
        if not row_key:
            return
        if shift:
            self._select_range_to(row_key)
        else:
            self._checked = {row_key}
            self._select_anchor = row_key
        self._refresh_registry_selection()

    def on_registry_cursor_move(self) -> None:
        """Arrow keys move the single selection with the cursor."""
        row_key = self._cursor_run_id()
        if not row_key:
            return
        self._checked = {row_key}
        self._select_anchor = row_key
        self._refresh_registry_selection()

    def _refresh_registry_selection(self) -> None:
        table = self.query_one("#run-table", DataTable)
        if isinstance(table, RegistryTable):
            table.refresh_selection()
        else:
            self._paint_registry(self._runs)

    def _select_range_to(self, row_key: str) -> None:
        keys = [item["key"] for item in self._registry_rows(self._runs) if item.get("key")]
        if row_key not in keys:
            return
        if not self._select_anchor or self._select_anchor not in keys:
            self._select_anchor = row_key
        start = keys.index(self._select_anchor)
        end = keys.index(row_key)
        lo, hi = sorted((start, end))
        self._checked = set(keys[lo : hi + 1])

    def action_check_all(self) -> None:
        keys = [item["key"] for item in self._registry_rows(self._runs) if item.get("key")]
        if keys and self._checked.issuperset(keys):
            self._checked.clear()
            self._select_anchor = ""
        else:
            self._checked = set(keys)
            self._select_anchor = keys[0] if keys else ""
        self._refresh_registry_selection()

    def action_extend_up(self) -> None:
        self._extend_selection(-1)

    def action_extend_down(self) -> None:
        self._extend_selection(1)

    def _extend_selection(self, delta: int) -> None:
        rows = self._registry_rows(self._runs)
        keys = [item["key"] for item in rows if item.get("key")]
        if not keys:
            return
        current = self._cursor_run_id()
        if current not in keys:
            current = keys[0]
        if not self._select_anchor or self._select_anchor not in keys:
            self._select_anchor = current
        index = keys.index(current)
        nxt = max(0, min(len(keys) - 1, index + delta))
        anchor = keys.index(self._select_anchor)
        lo, hi = sorted((anchor, nxt))
        self._checked = set(keys[lo : hi + 1])
        table = self.query_one("#run-table", DataTable)
        table.move_cursor(row=nxt)
        self._refresh_registry_selection()

    def action_delete_selected(self) -> None:
        """Delete checked rows, or the cursor row if none are checked."""
        keys = []
        if self._checked:
            keys = list(self._checked)
        else:
            run_id = self._cursor_run_id()
            if run_id:
                keys = [run_id]
        if not keys:
            self.notify("No run selected. Ctrl+A highlights all, or arrow to a row.", severity="warning")
            return
        if len(keys) == 1 and keys[0].startswith("intake:"):
            self._delete_intake_session(keys[0].split(":", 1)[1])
            return
        if len(keys) == 1:
            run_id = keys[0]
            short = run_id[:16] + "..."

            def _confirmed_one():
                def _deliver(r):
                    if isinstance(r, dict):
                        self.notify(f"Delete failed: {r.get('error')}", severity="error")
                    elif r.ok:
                        self._checked.discard(run_id)
                        self.action_refresh()
                        self.notify(f"Deleted run {short}", severity="information")
                    else:
                        self.notify(f"Delete failed: {r.error_message}", severity="error")

                self._run_async(lambda: self.driver.delete(run_id), _deliver, drop_stale=False)

            self.app.push_screen(ConfirmScreen(
                f"  Delete run {short}?\n\n  This removes the run directory and its evidence.\n\n"
                f"  [Y] Confirm  [N]/[Esc] Cancel",
                _confirmed_one,
            ))
            return

        count = len(keys)

        def _confirmed_many() -> None:
            def _work() -> dict[str, Any]:
                deleted = 0
                failed: list[str] = []
                from .intake_job import delete_session, supervisor_for

                root = str(self.config.workspace_root or "") if self.config else ""
                job = supervisor_for(self.app)
                for key in keys:
                    if key.startswith("intake:"):
                        sid = key.split(":", 1)[1]
                        if job.is_capturing_session(sid):
                            failed.append(f"{sid[:12]}: capture running")
                            continue
                        if root and delete_session(root, sid):
                            deleted += 1
                            # Drop the in-memory session only through the
                            # supervisor's lock: an unlocked write here
                            # could clobber a newer capture started while
                            # this batch ran.
                            job.forget(sid)
                        else:
                            failed.append(f"{sid[:12]}: not removed")
                        continue
                    try:
                        result = self.driver.delete(key)
                    except Exception as exc:
                        failed.append(f"{key[:12]}: {exc}")
                        continue
                    if getattr(result, "ok", False):
                        deleted += 1
                    else:
                        failed.append(
                            f"{key[:12]}: {getattr(result, 'error_message', None) or 'failed'}"
                        )
                return {"deleted": deleted, "failed": failed}

            def _deliver(result: Any) -> None:
                self._checked.clear()
                if isinstance(result, dict) and "deleted" in result:
                    self.action_refresh()
                    failed = result.get("failed") or []
                    if failed:
                        self.notify(
                            f"Deleted {result['deleted']}/{count}; {failed[0]}",
                            severity="warning",
                        )
                    else:
                        self.notify(f"Deleted {result['deleted']} item(s).", severity="information")
                    return
                if isinstance(result, dict):
                    self.notify(f"Delete failed: {result.get('error')}", severity="error")

            self._run_async(_work, _deliver, drop_stale=False)

        self.app.push_screen(ConfirmScreen(
            f"  Delete {count} selected rows?\n\n"
            f"  Pipeline runs remove evidence. Intake Home rows drop the session only.\n\n"
            f"  [Y] Confirm  [N]/[Esc] Cancel",
            _confirmed_many,
        ))

    def _delete_intake_session(self, session_id: str) -> None:
        from .intake_job import delete_session, supervisor_for

        job = supervisor_for(self.app)
        if job.is_capturing_session(session_id):
            self.notify("Capture is still running; wait for it to finish.", severity="warning")
            return
        root = str(self.config.workspace_root or "") if self.config else ""
        if not root:
            self.notify("No workspace is configured.", severity="warning")
            return

        def _confirmed() -> None:
            # Re-check under the supervisor's lock: the dialog can sit
            # open long enough for a capture to (re)start.
            if job.is_capturing_session(session_id):
                self.notify("Capture is still running; wait for it to finish.", severity="warning")
                return
            delete_session(root, session_id)
            job.forget(session_id)
            self.action_refresh()
            self.notify(f"Removed intake session {session_id[:16]}")

        self.app.push_screen(ConfirmScreen(
            f"  Remove intake session {session_id[:16]}… from Home?\n\n"
            f"  Workspace evidence is kept; only the Home session row is removed.\n\n"
            f"  [Y] Confirm  [N]/[Esc] Cancel",
            _confirmed,
        ))

    def action_delete_all(self) -> None:
        """Delete every run currently listed on the registry."""
        runs = [run for run in self._runs if run.get("run_id") or run.get("_full_id")]
        if not runs:
            self.notify("No runs to delete.", severity="warning")
            return
        count = len(runs)

        def _confirmed() -> None:
            ids = [str(run.get("_full_id") or run.get("run_id")) for run in runs]

            def _work() -> dict[str, Any]:
                deleted = 0
                failed: list[str] = []
                for run_id in ids:
                    try:
                        result = self.driver.delete(run_id)
                    except Exception as exc:
                        failed.append(f"{run_id[:12]}: {exc}")
                        continue
                    if getattr(result, "ok", False):
                        deleted += 1
                    else:
                        failed.append(
                            f"{run_id[:12]}: {getattr(result, 'error_message', None) or 'failed'}"
                        )
                return {"deleted": deleted, "failed": failed}

            def _deliver(result: Any) -> None:
                if isinstance(result, dict) and "deleted" in result:
                    self.action_refresh()
                    failed = result.get("failed") or []
                    if failed:
                        self.notify(
                            f"Deleted {result['deleted']}/{count}; "
                            f"{failed[0]}",
                            severity="warning",
                        )
                    else:
                        self.notify(
                            f"Deleted {result['deleted']} run(s).",
                            severity="information",
                        )
                    return
                if isinstance(result, dict):
                    self.notify(f"Delete failed: {result.get('error')}", severity="error")
                    return
                self.notify("Delete failed.", severity="error")

            self._run_async(_work, _deliver, drop_stale=False)

        self.app.push_screen(ConfirmScreen(
            f"  Delete all {count} listed run(s)?\n\n"
            f"  This removes each run directory and its evidence.\n\n"
            f"  [Y] Confirm  [N]/[Esc] Cancel",
            _confirmed,
        ))

    def action_validate_pipeline(self) -> None:
        self.app.push_screen(ValidateScreen(self.driver))

    def action_edit_config(self) -> None:
        self.app.push_screen(ConfigScreen(self.config, self.ai, self.driver, self.engine))

    def on_screen_resume(self, event) -> None:
        """Returning from a sub-screen: reload the config file (external
        `stammtisch config set` edits land here) and re-sync drivers."""
        self.config.load()
        self.ai.api_key = self.config.ai_api_key
        self.ai.base_url = self.config.ai_base_url
        self.ai.model = self.config.ai_model
        if self.config.state_root:
            self.driver.state_root = self.config.state_root
        self.action_refresh()

    def action_open_chat(self) -> None:
        if not self.ai.available:
            self.notify("AI API key not set.", severity="error")
            return
        self.app.push_screen(ChatScreen(self.ai))

    def action_fetch_data(self) -> None:
        self.app.push_screen(DataFetchScreen(self.engine, self.config))

    def action_open_intake(self) -> None:
        """Land on the newest indexed daily report; capture is explicit (R)."""
        self.app.push_screen(DailyIntakeScreen(self.config))

    def action_open_sentiment(self) -> None:
        from .brief import SentimentScreen, load_daily, load_daily_path

        # Prefer the exact report graph most recently verified by IntakeDriver.
        # A bare JSON found under workspace_root is not sufficient evidence and
        # is deliberately not rediscovered after a restart.
        result = getattr(self.app, "last_daily_intake_result", None)
        if result is not None and result.ok:
            artifacts = result.artifacts or {}
            doc = load_daily_path(
                artifacts.get("report_json"),
                html_path=artifacts.get("report_html"),
                expected_date=(result.envelope or {}).get("date"),
            )
        else:
            # History index next: it covers intake-native reports, which the
            # legacy tree reader cannot see (they never land in reports_root).
            store, _index_error = _history_store(self.config)
            entry = store.latest() if store is not None else None
            if entry is not None:
                doc = load_daily_path(
                    entry.json_path,
                    html_path=entry.html_path or None,
                    expected_date=entry.report_date,
                )
            else:
                # Explicit compatibility path for historical report trees.
                root = self.config.get("reports_root") if self.config else None
                doc = load_daily(None, root)
        if not doc.get("ok"):
            self.notify(str(doc.get("error") or "no daily brief"), severity="warning")
            return
        # Market-wide tape. Do not inherit the last chart ticker — that
        # pinned 600098.SS (and any other empty name) onto every `s`.
        self.app.push_screen(SentimentScreen(doc, "", self.config))

    def action_open_crypto(self) -> None:
        """Crypto module — Polymarket tape beside the daily crypto slice."""
        from .polymarket import CryptoScreen

        proxy_url = self.config.get("polymarket_proxy_url") if self.config else None
        self.app.push_screen(CryptoScreen(proxy_url=proxy_url, config=self.config))

    def action_open_energy(self) -> None:
        """ENERGY module — read-only EIA Open Data watchlist."""
        from .energy import EnergyScreen

        api_key = self.config.get("eia_api_key") if self.config else None
        proxy_url = self.config.get("energy_proxy_url") if self.config else None
        self.app.push_screen(EnergyScreen(api_key=api_key, proxy_url=proxy_url))

    def action_open_chart(self) -> None:
        """Open the browser K-line timeseries viewer (starts the local
        chart server on first use)."""
        import webbrowser

        from .chart_server import DEFAULT_PORT, ensure_running

        configured_port = self.config.get("chart_port", DEFAULT_PORT) if self.config else DEFAULT_PORT

        def _deliver(port) -> None:
            # ensure_running reports every startup failure as None; a
            # worker exception arrives as an error dict.
            if not isinstance(port, int) or port <= 0:
                self.notify("chart server failed to start", severity="error")
                return
            url = f"http://127.0.0.1:{port}/"
            try:
                opened = webbrowser.open(url)
            except Exception:
                opened = False
            if opened:
                self.notify(f"Chart: {url}", severity="information")
            else:
                # The chart is served regardless — hand the URL to the user.
                self.notify(f"Chart ready at {url} (no browser could be opened)", severity="warning")

        # A cold start waits for the server to come up (~5s worst case), so
        # never on the UI thread; the result is user-requested feedback, not
        # a refresh, and must not be dropped as stale.
        self._run_async(lambda: ensure_running(configured_port), _deliver, drop_stale=False)

    def action_run_backtest(self) -> None:
        self.app.push_screen(BacktestScreen(self.engine, self.config))

    def action_show_indicators(self) -> None:
        self.app.push_screen(IndicatorsScreen(self.engine, self.config))

    def action_run_portfolio(self) -> None:
        self.app.push_screen(PortfolioScreen(self.engine, self.config))

    def action_eval_gates(self) -> None:
        self.app.push_screen(GatesScreen(self.engine, self.config))


# ═══════════════════════════════════════════════════════════════════
# Domain browser — read-only view of a configured domain plugin root
# ═══════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════
# FUTURES — continuous contracts + exchange-settled fuels, by category
# ═══════════════════════════════════════════════════════════════════


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

    from .chart_server import DEFAULT_PORT, ensure_running

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
        board.add_columns("CODE", "NAME", "LAST", "CHG%", "5D%", "20D%", "VOL", "UNIT")
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

    def _load(self) -> dict[str, Any]:
        from .engine import _missing_ohlcv, _normalize_symbol

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
        adapter_board = None
        adapter_error = None
        argv = tuple(self.config.futures_argv) if self.config else ()
        if argv:
            from .domaindata import DomainDriver

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

    def _render_strip(self) -> None:
        strip = Text()
        for index, category in enumerate(self._cats):
            if index:
                strip.append(" ")
            label = f"  {category}  "
            strip.append(label, style="bold reverse" if index == self._cat_idx
                         else "color(160)")
        self.query_one("#fut-cats", Static).update(strip)

    def _render_board(self) -> None:
        board = self.query_one("#fut-board", DataTable)
        board.clear()
        items = self._by_cat.get(self._cats[self._cat_idx], []) if self._cats else []
        for item in items:
            if "error" in item:
                board.add_row(item["code"], item["name"], item["error"],
                              "", "", "", "", "", key=item["key"])
                continue
            board.add_row(
                item["code"],
                item["name"],
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
        for bar in item.get("recent") or []:
            if item["source"] == "sgx":
                recent_table.add_row(
                    bar["date"], f"{bar['settle']:,.2f}",
                    f"{bar.get('volume', 0):.0f}",
                    f"{bar.get('open_interest', 0):.0f}",
                    str(bar.get("month", "")),
                )
            else:
                recent_table.add_row(
                    bar["date"], f"{bar['open']:.2f}", f"{bar['high']:.2f}",
                    f"{bar['low']:.2f}", f"{bar['close']:.2f}",
                    f"{bar['volume']:.0f}",
                )
        curve_table = self.query_one("#fut-curve", DataTable)
        curve_table.clear()
        for point in item.get("curve") or []:
            curve_table.add_row(point["month"], f"{point['settle']:,.2f}")

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


# ═══════════════════════════════════════════════════════════════════
# SHIPPING — exchange settlement board via configured adapter command
# ═══════════════════════════════════════════════════════════════════


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
        from .domaindata import DomainDriver, validate_spval_board_v2

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
        instruments = result.get("instruments", [])
        self._instruments = {
            inst["code"]: inst for inst in instruments if isinstance(inst, dict)
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


# ═══════════════════════════════════════════════════════════════════
# Daily data intake
# ═══════════════════════════════════════════════════════════════════


class DailyIntakeScreen(Screen):
    """Direct product intake with evidence and coverage visible first.

    Opening the screen never captures: the landing view surfaces the newest
    indexed report and only an explicit R starts a capture (the capture
    stack is memory-heavy on this host class).
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "capture", "Capture"),
        Binding("enter", "open_report", "Analyze (GALAHAD)"),
    ]
    CSS = """
    DailyIntakeScreen { layout: vertical; }
    #intake-body { height: 1fr; border: solid #505050; background: #000000; padding: 1 2; }
    """

    def __init__(
        self,
        config: Any,
        *,
        auto_start: bool = False,
        session_id: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.config = config
        self.auto_start = auto_start
        self._session_id = session_id
        self._auto_started = False
        self._capture_running = False
        self._result: Any = None
        self._progress_timer = None

    def compose(self) -> ComposeResult:
        yield Static(
            "  DAILY DATA INTAKE  |  [R] Capture  [Enter] Analyze (GALAHAD)  [Esc] Back",
            classes="header-bar",
        )
        yield ScrollableContainer(
            # Canonical titles may contain Rich markup characters.  Rendering
            # this surface as plain text keeps source wording byte-for-byte
            # visible instead of interpreting it as framework markup.
            Static("  Status: READY\n", id="intake-text", markup=False),
            id="intake-body",
        )
        yield Footer()

    def on_show(self, _event: Any) -> None:
        # A pushed Screen receives Mount before Textual marks it mounted.  The
        # first Show event is the earliest reliable point for widget updates.
        if self._auto_started:
            return
        self._auto_started = True
        if self.auto_start:
            self.action_capture()
            return
        if self._attach_live_job() or self._show_named_session():
            return
        self._show_latest_report()

    def _show_latest_report(self) -> None:
        """Landing view: the newest indexed report, without capture side effects."""
        store, error = _history_store(self.config)
        entry = store.latest() if store is not None else None
        lines = ["  Status: READY", ""]
        if entry is not None:
            lines.append(f"  Latest report: {entry.row_text()}")
        elif error:
            lines.append(f"  History index: {error}")
        else:
            lines.append("  No daily report is indexed yet.")
        from .intake_job import report_session_date, today_yyyymmdd

        day = report_session_date()
        if day is None:
            # No offline calendar is importable in this environment: the
            # product certifies the session date itself (a weekday or
            # fixed-clock guess is forbidden), so the hint stays undated.
            capture_hint = "R captures the session the product calendar certifies"
        else:
            pretty = f"{day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else day
            if day == today_yyyymmdd():
                capture_hint = f"R captures {pretty} (current session)"
            else:
                capture_hint = f"R captures {pretty} (last session — pre-open / closed)"
        lines.extend([
            "",
            f"  Enter opens the latest report, {capture_hint}, H shows history.",
            "  Capture feeds the Chinese daily report and Sentiment. It stays on Home.",
        ])
        self.query_one("#intake-text", Static).update("\n".join(lines) + "\n")

    def _attach_live_job(self) -> bool:
        from .intake_job import render_progress, supervisor_for

        job = supervisor_for(self.app)
        live = job.snapshot()
        if live is None:
            return False
        if live.get("state") == "capturing":
            self._capture_running = True
            self._session_id = str(live.get("id") or "")
            self.query_one("#intake-text", Static).update(render_progress(live))
            self._start_progress_timer()
            return True
        if job.result is not None:
            self._result = job.result
            self._capture_running = False
            self.query_one("#intake-text", Static).update(self._format_result(job.result))
            return True
        return False

    def _show_named_session(self) -> bool:
        from .intake_job import load_session, render_progress

        if not self._session_id or not self.config:
            return False
        session = load_session(self.config.workspace_root, self._session_id)
        if session is None:
            return False
        if session.get("state") == "capturing":
            self._capture_running = True
            self.query_one("#intake-text", Static).update(render_progress(session))
            self._start_progress_timer()
            return True
        self.query_one("#intake-text", Static).update(render_progress(session))
        return True

    def _start_progress_timer(self) -> None:
        if self._progress_timer is not None:
            return
        self._progress_timer = self.set_interval(1.0, self._tick_progress)

    def _stop_progress_timer(self) -> None:
        timer = self._progress_timer
        if timer is not None:
            timer.stop()
            self._progress_timer = None

    def _tick_progress(self) -> None:
        from .intake_job import render_progress, supervisor_for

        if not self.is_mounted:
            return
        live = supervisor_for(self.app).snapshot()
        if live is None:
            self._stop_progress_timer()
            return
        if live.get("state") != "capturing":
            self._stop_progress_timer()
            return
        self._capture_running = True
        self.query_one("#intake-text", Static).update(render_progress(live))

    def on_intake_job_finished(self, result: Any) -> None:
        if not self.is_mounted:
            return
        self._stop_progress_timer()
        self._capture_running = False
        self._result = result
        if result is None:
            live = None
            try:
                from .intake_job import supervisor_for

                live = supervisor_for(self.app).snapshot()
            except Exception:
                live = None
            message = (live or {}).get("error") or "Daily intake failed."
            self.query_one("#intake-text", Static).update(
                f"  Status: REJECTED\n\n  {message}\n"
            )
            self.notify(message, severity="error")
            return
        self._refresh_history_index()
        self.query_one("#intake-text", Static).update(self._format_result(result))
        if result.ok:
            self.notify("Daily data accepted. Press Enter to open the report.")
        else:
            self.notify(f"Daily intake failed: {result.error}", severity="error")

    def _refresh_history_index(self) -> None:
        """Fold the just-accepted run into the history index (best effort)."""
        _history_store(self.config)

    def action_capture(self) -> None:
        from .intake_job import render_progress, supervisor_for

        job = supervisor_for(self.app)
        if job.capturing:
            live = job.snapshot() or {}
            self._capture_running = True
            self._session_id = str(live.get("id") or "")
            self.query_one("#intake-text", Static).update(render_progress(live))
            self._start_progress_timer()
            self.notify("Capture already running; showing live progress.")
            return
        argv = tuple(self.config.intake_argv) if self.config else ()
        if not argv:
            self.query_one("#intake-text", Static).update(
                "  Status: NOT CONFIGURED\n\n"
                "  Set intake_cmd to a daily-data product command. The adapter "
                "will append --workspace-root and --json.\n"
            )
            self.notify("Daily-data intake command is not configured.", severity="warning")
            return

        from .intake import IntakeDriver

        try:
            # Constructed before any state flip: the driver rejects config
            # values the editor accepts (timeout ceiling, driver-owned
            # flags), and a raise here must neither kill the app nor wedge
            # the screen in CAPTURING.
            IntakeDriver(
                (*argv, "--report-builder", self.config.get("intake_report_builder", "ai")),
                self.config.workspace_root,
                timeout_seconds=self.config.intake_timeout_seconds,
            )
        except ValueError as exc:
            self.notify(f"Invalid intake configuration: {exc}", severity="error")
            return

        session = job.start(self.app, self.config)
        self._capture_running = True
        self._result = None
        self._session_id = str(session.get("id") or "")
        self.query_one("#intake-text", Static).update(render_progress(session))
        self._start_progress_timer()

    @staticmethod
    def _format_result(result: Any) -> str:
        if not result.ok:
            envelope = result.envelope if isinstance(result.envelope, dict) else {}
            quality = envelope.get("quality") if isinstance(envelope.get("quality"), dict) else {}
            issues = quality.get("issues") if isinstance(quality.get("issues"), list) else []
            lines = ["  Status: REJECTED", "", f"  {result.error or 'Unknown intake error'}"]
            session_markets = envelope.get("session_markets")
            if isinstance(session_markets, list) and session_markets:
                lines.append(
                    "  Market session: "
                    + ", ".join(str(value) for value in session_markets)
                )
            for issue in issues:
                if isinstance(issue, str) and issue.strip():
                    lines.append(f"    - {issue.strip()}")
            evidence_exceptions, evidence_error = DailyIntakeScreen._evidence_exceptions(result)
            for status in ("failed", "pruned"):
                sources = evidence_exceptions.get(status, [])
                if sources:
                    lines.append(f"    - {status.capitalize()} sources: {', '.join(sources)}")
            if evidence_error and result.artifacts:
                lines.append(f"    - Evidence status display error: {evidence_error}")
            if result.artifacts:
                lines.extend(["", "  Verified diagnostic artifacts"])
                for label in ("evidence_manifest", "canonical_dataset"):
                    artifact = result.artifacts.get(label)
                    if artifact is not None:
                        lines.append(f"    {label}: {artifact}")
            lines.extend(["", "  No report JSON or HTML was published."])
            return "\n".join(lines) + "\n"

        envelope = result.envelope or {}
        counts = result.counts or {}
        market_counts = envelope.get("market_counts") or {}
        quality = envelope.get("quality") if isinstance(envelope.get("quality"), dict) else {}
        quality_status = quality.get("status")
        quality_label = (
            quality_status.upper()
            if isinstance(quality_status, str) and quality_status.strip()
            else "UNKNOWN"
        )
        quality_issues = quality.get("issues")
        if not isinstance(quality_issues, list):
            quality_issues = []
        quality_issues = [issue for issue in quality_issues if isinstance(issue, str) and issue]
        source_expected = counts.get("expected", "?")
        source_ok = counts.get("succeeded", counts.get("successful", "?"))
        source_failed = counts.get("failed", 0)
        source_pruned = counts.get("pruned", 0)
        records = counts.get("canonical_records", counts.get("records", "?"))
        lines = [
            "  Status: ACCEPTED",
            f"  Date: {envelope.get('date', '?')}",
            f"  Quality gate: {quality_label}",
            f"  Sources: {source_ok}/{source_expected} successful  failed={source_failed}  pruned={source_pruned}",
            f"  Canonical records: {records}",
            "",
            "  Quality issues",
        ]
        if quality_issues:
            lines.extend(f"    - {issue}" for issue in quality_issues)
        elif quality_label == "PASSED":
            lines.append("    None reported.")
        else:
            lines.append("    - Quality metadata did not provide an issue description.")

        evidence_exceptions, evidence_error = DailyIntakeScreen._evidence_exceptions(result)
        for status in ("failed", "pruned"):
            sources = evidence_exceptions.get(status, [])
            if sources:
                lines.append(f"    - {status.capitalize()} sources: {', '.join(sources)}")
        if evidence_error:
            lines.append(f"    - Evidence status display error: {evidence_error}")

        lines.extend([
            "",
            "  Market coverage",
        ])
        if isinstance(market_counts, dict) and market_counts:
            for market, value in sorted(market_counts.items()):
                lines.append(f"    {market}: {value}")
        else:
            lines.append("    (reported in the canonical dataset)")

        canonical_records, canonical_error = DailyIntakeScreen._canonical_records(result)
        lines.extend(["", "  Canonical record titles"])
        if canonical_records:
            for index, record in enumerate(canonical_records, 1):
                lines.append(f"    {index}. [{record['market']}] {record['title']}")
        elif canonical_error:
            lines.append(f"    Canonical data display error: {canonical_error}")
        else:
            lines.append("    No canonical records were provided.")

        lines.extend(["", "  Verified lineage"])
        for label in ("evidence_manifest", "canonical_dataset", "report_json", "report_html"):
            artifact = (result.artifacts or {}).get(label)
            metadata = ((result.envelope or {}).get("artifacts") or {}).get(label) or {}
            digest = str(metadata.get("sha256") or "") if isinstance(metadata, dict) else ""
            path = str(artifact or "?")
            suffix = f"  sha256={digest[:16]}..." if digest else ""
            lines.append(f"    {label}: {path}{suffix}")
        lines.extend([
            "",
            "  Report JSON was derived from the canonical dataset.",
            "  Report HTML was derived from that report JSON.",
            "  Press Enter to open the Chinese daily report.",
        ])
        return "\n".join(lines) + "\n"

    @staticmethod
    def _artifact_document(result: Any, key: str) -> tuple[dict[str, Any], str | None]:
        artifact = (result.artifacts or {}).get(key)
        if artifact is None:
            return {}, f"{key} is unavailable"
        try:
            document = json.loads(Path(artifact).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {}, str(exc)
        if not isinstance(document, dict):
            return {}, f"{key} root is not an object"
        return document, None

    @staticmethod
    def _canonical_records(result: Any) -> tuple[list[dict[str, str]], str | None]:
        document, error = DailyIntakeScreen._artifact_document(result, "canonical_dataset")
        if error:
            return [], error
        raw_records = document.get("records")
        if not isinstance(raw_records, list):
            return [], "canonical dataset records are unavailable"
        records: list[dict[str, str]] = []
        for record in raw_records:
            if not isinstance(record, dict):
                continue
            title = record.get("title")
            market = record.get("market")
            if isinstance(title, str) and title.strip() and isinstance(market, str) and market:
                # Keep title exactly as stored; validation already established
                # that it is a canonical source field.
                records.append({"market": market, "title": title})
        return records, None

    @staticmethod
    def _evidence_exceptions(result: Any) -> tuple[dict[str, list[str]], str | None]:
        document, error = DailyIntakeScreen._artifact_document(result, "evidence_manifest")
        if error:
            return {}, error
        captures = document.get("captures")
        if not isinstance(captures, list):
            return {}, "evidence manifest captures are unavailable"
        exceptions: dict[str, list[str]] = {"failed": [], "pruned": []}
        for capture in captures:
            if not isinstance(capture, dict):
                continue
            status = capture.get("status")
            source = capture.get("source")
            if status in exceptions and isinstance(source, str) and source:
                exceptions[status].append(source)
        return exceptions, None

    def action_back(self) -> None:
        self._stop_progress_timer()
        self.app.pop_screen()

    def on_unmount(self) -> None:
        self._stop_progress_timer()

    def action_open_report(self) -> None:
        """Open the daily report — the browser renders the HTML; the
        terminal keeps only the sentiment tape: Enter hands the captured
        dataset to GALAHAD for a real analysis turn."""
        from .intake_job import supervisor_for

        if self._capture_running or supervisor_for(self.app).capturing:
            self.notify("Capture is still running.", severity="warning")
            return
        if self._result is not None and self._result.ok:
            artifacts = self._result.artifacts or {}
            json_path = artifacts.get("report_json")
            day = (self._result.envelope or {}).get("date")
        else:
            # No capture this session: analyze the newest indexed report.
            store, error = _history_store(self.config)
            entry = store.latest() if store is not None else None
            if entry is None:
                self.notify(
                    error or "No accepted daily dataset is available.",
                    severity="warning",
                )
                return
            json_path = entry.json_path
            day = entry.report_date
        from .brief import load_daily_path

        doc = load_daily_path(json_path, expected_date=day)
        if not doc.get("ok"):
            self.notify(str(doc.get("error") or "report JSON is invalid"), severity="error")
            return
        _galahad_report_analysis(self, doc)


def _report_digest(doc: dict[str, Any]) -> str:
    """Compact, faithful digest of one captured day for GALAHAD."""
    from .brief import SUMMARY_CAP, TITLE_CAP, clean_text, curate_items, is_displayable_item

    day = str(doc.get("date") or "?")
    lines = [f"Daily market dataset {day}"]
    counts = doc.get("counts") or {}
    if counts:
        lines.append(f"counts: {counts}")
    brief = doc.get("brief") or []
    if brief:
        lines.append("brief:")
        for item in brief[:6]:
            text = clean_text(item.get("text"), 160)
            if text:
                lines.append(f"  - {text}")
    markets = doc.get("markets") or {}
    for market in ("ashare", "hk", "us", "crypto"):
        items = [i for i in (markets.get(market) or []) if is_displayable_item(i)]
        if not items:
            continue
        lines.append(f"{market} ({len(items)}):")
        for item in curate_items(items, 8):
            title = clean_text(item.get("title"), TITLE_CAP)
            summary = clean_text(item.get("summary"), SUMMARY_CAP)
            line = f"  - {title}"
            if summary and summary != title:
                line += f" | {summary}"
            lines.append(line)
    text = "\n".join(lines)
    return text[:8000]


def _galahad_report_analysis(screen: Any, doc: dict[str, Any]) -> None:
    """Hand one captured day to GALAHAD — a real model turn, not a view.

    The workstation renders no human-facing report beyond the sentiment
    board: captured data goes straight to GALAHAD for analysis under
    its five-discipline paradigm (tools stay available to it).
    """
    ai = getattr(screen.app, "ai", None)
    if ai is None or not getattr(ai, "available", False):
        screen.notify("AI service is not configured.", severity="warning")
        return
    day = str(doc.get("date") or "?")
    pretty = f"{day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else day
    from .lang import tr

    try:
        language = str(screen.app.config.get("language", "en") or "en")
    except Exception:
        language = "en"
    prompt = tr(language, "galahad.report_prompt", "Analyze this daily market dataset.")
    screen.app.push_screen(
        ChatScreen(
            ai,
            initial_prompt=f"{prompt} Date: {pretty}.",
            initial_context=_report_digest(doc),
        )
    )


def _history_store(config: Any) -> tuple[Any, str | None]:
    """Build the report-history store and index both origins.

    Never raises: an index failure is reported, not thrown, so one corrupted
    artifact cannot wedge a screen.
    """
    if config is None:
        return None, "report history is not configured"
    from .history import HistoryStore

    try:
        store = HistoryStore(config.history_db)
        store.index_all(
            config.workspace_root,
            str(config.get("reports_root") or "") or None,
        )
    except Exception as exc:
        return None, str(exc)
    return store, None


# ═══════════════════════════════════════════════════════════════════
# Pipeline Run
# ═══════════════════════════════════════════════════════════════════


class PipelineRunScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+r", "execute", "Run selected"),
    ]
    CSS = """
    PipelineRunScreen { layout: vertical; }
    #pr-body { height: 1fr; layout: horizontal; }
    #pr-left { width: 1fr; }
    #pr-right { width: 46; }
    #pr-actions { height: 3; padding: 0 1; align-horizontal: right; }
    #pr-actions Button { min-width: 24; }
    """
    driver: StammtischDriver
    ai: AIDriver
    _selected: str | None = None
    _is_running: reactive[bool] = reactive(False)

    def __init__(self, driver: StammtischDriver, ai: AIDriver, **kwargs: Any):
        super().__init__(**kwargs)
        self.driver = driver
        self.ai = ai
        self._selected_stages: list[dict[str, Any]] = []
        self._selection_token: object | None = None
        self._started_selection_token: object | None = None
        self._active_run_token: object | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            "  Pipeline Execution  |  Select, then Run (Enter on button / Ctrl+R)  |  Esc back",
            classes="header-bar",
        )
        yield Static("  Status: READY", id="pr-status")
        with Horizontal(id="pr-body"):
            with Vertical(id="pr-left"):
                with Vertical(classes="panel"):
                    yield Static("  Select Pipeline", classes="panel-title")
                    options = [Option(p.stem, id=str(p)) for p in self.driver.list_pipelines()]
                    yield OptionList(*options, id="pipeline-select") if options else Static("  No pipelines.")
                with Vertical(classes="panel"):
                    yield Static("  Stage Flow", classes="panel-title")
                    yield StageFlowWidget(id="pr-stage-flow")
            with Vertical(id="pr-right"):
                with Vertical(classes="panel"):
                    yield Static("  Execution Log", classes="panel-title")
                    yield EventTimeline(id="pr-timeline")
        with Horizontal(id="pr-actions"):
            yield Button("Run Selected Pipeline", id="pr-run", disabled=True)
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Selection is deliberately preview-only.  A real run is a durable,
        # potentially remote operation and therefore has its own explicit
        # Run control instead of sharing OptionList's click/Enter action.
        selected = event.option_id
        if not selected:
            return
        # Disarm the previous selection before reading the newly selected
        # file.  If this spec is unreadable, Run must not silently execute
        # whichever valid pipeline happened to be selected beforehand.
        self._selected = None
        self._selected_stages = []
        self._selection_token = None
        self._sync_run_button()
        try:
            spec = json.loads(Path(selected).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.notify(f"Unreadable pipeline spec: {exc}", severity="error")
            return
        if not isinstance(spec, dict):
            self.notify("Pipeline spec is not a JSON object.", severity="error")
            return
        raw_stages = spec.get("stages")
        stages = (
            [dict(stage) for stage in raw_stages if isinstance(stage, dict)]
            if isinstance(raw_stages, list) else []
        )
        self._selected = selected
        self._selected_stages = stages
        # A fresh explicit selection arms exactly one run.  This prevents a
        # queued double-click on the Run button from launching again after a
        # very fast first invocation has already completed.
        self._selection_token = object()
        flow = self.query_one("#pr-stage-flow", StageFlowWidget)
        flow.stages = [dict(stage) for stage in stages]
        flow.stage_states = {}
        flow.active_stage = None
        self.query_one("#pr-timeline", EventTimeline).events = []
        self._sync_run_button()
        if not self._is_running:
            self.query_one("#pr-status", Static).update(
                f"  Status: SELECTED  |  {Path(selected).stem}  |  Activate Run to execute"
            )
        # Move the confirmation boundary to a real control: after selecting
        # by mouse or keyboard, Enter activates Run rather than re-selecting.
        self.query_one("#pr-run", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pr-run":
            self.action_execute()

    def _sync_run_button(self) -> None:
        button = self.query_one("#pr-run", Button)
        button.disabled = (
            self._is_running
            or self._selection_token is None
            or self._selection_token is self._started_selection_token
        )

    def action_execute(self) -> None:
        if not self._selected:
            self.notify("Select a pipeline first.", severity="warning")
            return
        if self._is_running:
            return
        selection_token = self._selection_token
        if selection_token is None:
            self.notify("Select a pipeline first.", severity="warning")
            return
        if selection_token is self._started_selection_token:
            self.notify(
                "This selection has already run; select it again to re-arm.",
                severity="warning",
            )
            return

        # Freeze all UI-derived run context before starting the worker.  A
        # user may preview another pipeline while this one is running; the
        # late result must never borrow that later selection or its stages.
        selected = self._selected
        stages = [dict(stage) for stage in self._selected_stages]
        run_token = object()
        self._started_selection_token = selection_token
        self._active_run_token = run_token
        self._is_running = True
        self._sync_run_button()
        self.query_one("#pr-status", Static).update(
            f"  Status: RUNNING  |  {Path(selected).stem}"
        )

        def _worker():
            inspect_error: str | None = None
            error: str | None = None
            try:
                result = self.driver.run(selected)
                if result is None:
                    raise ValueError("run returned no result")
                if not hasattr(result, "ok"):
                    raise ValueError("run returned an invalid result")
                if result.ok and not isinstance(result.data, dict):
                    raise ValueError("run returned invalid result data")
            except Exception as e:
                result = None
                error = str(e)
            inspect_result = None
            if result is not None and result.ok:
                run_id = result.data.get("run_id")
                if isinstance(run_id, str) and run_id:
                    try:
                        inspect_result = self.driver.inspect(run_id)
                    except Exception as exc:
                        inspect_error = str(exc)
                else:
                    inspect_error = "run result omitted a valid run id"

            def _deliver():
                # The screen must never stay stuck in RUNNING: worker
                # exceptions reset the state on the UI thread.
                if (
                    not self.is_mounted
                    or self._active_run_token is not run_token
                ):
                    return
                if result is None:
                    self._active_run_token = None
                    self._is_running = False
                    self._sync_run_button()
                    self.query_one("#pr-status", Static).update(
                        f"  Status: ERROR  |  {Path(selected).stem}  |  "
                        f"{error or 'run failed'}"
                    )
                    self.notify(
                        f"Run failed: {error or 'run failed'}",
                        severity="error",
                    )
                else:
                    events: list[dict[str, Any]] = []
                    delivery_inspect_error = inspect_error
                    if inspect_result is not None and not inspect_result.ok:
                        delivery_inspect_error = (
                            inspect_result.error_message or "inspect failed"
                        )
                    elif inspect_result is not None:
                        candidate = inspect_result.data.get("events")
                        if isinstance(candidate, list) and all(
                            isinstance(event, dict) for event in candidate
                        ):
                            events = candidate
                        else:
                            delivery_inspect_error = (
                                "inspect returned invalid events"
                            )
                    self._on_run_result(
                        result,
                        run_token,
                        selection_token,
                        selected,
                        stages,
                        events,
                        delivery_inspect_error,
                    )

            try:
                self.app.call_from_thread(_deliver)
            except Exception:
                pass  # app torn down mid-call: nothing left to update

        threading.Thread(target=_worker, daemon=True).start()

    def _on_run_result(
        self,
        result,
        run_token: object,
        selection_token: object,
        selected: str,
        stages: list[dict[str, Any]],
        events: list[dict[str, Any]],
        inspect_error: str | None,
    ) -> None:
        if not self.is_mounted or self._active_run_token is not run_token:
            return
        status = self.query_one("#pr-status", Static)
        flow = self.query_one("#pr-stage-flow", StageFlowWidget)
        pipeline_name = Path(selected).stem
        if result.ok:
            terminal = result.data.get("terminal", "?")
            run_id = result.data.get("run_id", "?")
            if not isinstance(terminal, str) or not isinstance(run_id, str):
                inspect_error = "run returned invalid terminal metadata"
                terminal = str(terminal)
                run_id = str(run_id)
            if inspect_error:
                status.update(
                    f"  Status: EVIDENCE UNAVAILABLE  |  {pipeline_name}  |  "
                    f"Run: {run_id[:16]}...  |  {inspect_error}"
                )
                self.notify(
                    f"Run result could not be verified: {inspect_error}",
                    severity="error",
                )
                self._active_run_token = None
                self._is_running = False
                self._sync_run_button()
                return
            if events and self._selection_token is selection_token:
                self.query_one("#pr-timeline", EventTimeline).events = events
            # Reassign (never mutate in place): Textual reactives compare
            # object identity, so in-place dict edits do not re-render.
            # Only paint the stage snapshot if the user is still viewing the
            # pipeline that produced this result.  Selecting B while A runs
            # must leave B idle when A's result arrives.
            if self._selection_token is selection_token:
                states = {
                    stage.get("id", "?"): (
                        "completed" if terminal == "completed" else "failed"
                    )
                    for stage in stages
                }
                flow.stages = [dict(stage) for stage in stages]
                flow.stage_states = states
                flow.active_stage = None
            status.update(
                f"  Status: {terminal.upper()}  |  {pipeline_name}  |  Run: {run_id[:16]}..."
            )
            self.notify(f"Run {run_id[:16]}... {terminal}", severity="information")
        else:
            if self._selection_token is selection_token:
                flow.stages = [dict(stage) for stage in stages]
                flow.stage_states = {
                    stage.get("id", "?"): "failed" for stage in stages
                }
                flow.active_stage = None
            status.update(
                f"  Status: FAILED  |  {pipeline_name}  |  {result.error_message}"
            )
            self.notify(f"Failed: {result.error_message}", severity="error")
        self._active_run_token = None
        self._is_running = False
        self._sync_run_button()

    def on_unmount(self) -> None:
        # Invalidate any result queued by a worker after this screen closes.
        self._active_run_token = None


# ═══════════════════════════════════════════════════════════════════
# Run Inspector
# ═══════════════════════════════════════════════════════════════════


def format_run_session_summary(run_id: str, snapshot: dict[str, Any]) -> str:
    """Plain-text session summary from a verified inspect snapshot."""
    manifest = snapshot.get("manifest") if isinstance(snapshot.get("manifest"), dict) else {}
    events = snapshot.get("events") if isinstance(snapshot.get("events"), list) else []
    gates = snapshot.get("gates") if isinstance(snapshot.get("gates"), list) else []
    receipts = snapshot.get("receipts") if isinstance(snapshot.get("receipts"), list) else []
    state = manifest.get("state") if isinstance(manifest.get("state"), dict) else {}
    pipeline = manifest.get("pipeline") if isinstance(manifest.get("pipeline"), dict) else {}
    name, when = run_session_parts({
        "pipeline_id": pipeline.get("id"),
        "created_at": manifest.get("created_at"),
        "run_id": run_id,
    })
    lines = [
        f"  {name}  {when}",
        f"  Run     {run_id}",
        f"  State   {state.get('code', '?')}",
    ]
    terminal = manifest.get("terminal") if isinstance(manifest.get("terminal"), dict) else {}
    if terminal.get("code"):
        lines.append(f"  End     {terminal.get('code')}  {terminal.get('at') or ''}".rstrip())
    blockers = state.get("blockers") if isinstance(state.get("blockers"), list) else []
    for blocker in blockers[:6]:
        if blocker:
            lines.append(f"  Blocker {blocker}")
    stages: dict[str, str] = {}
    first_at = ""
    last_at = ""
    last_type = ""
    for event in events:
        if not isinstance(event, dict):
            continue
        at = str(event.get("at") or "")
        if at and not first_at:
            first_at = at
        if at:
            last_at = at
        last_type = str(event.get("type") or last_type)
        stage = event.get("stage")
        kind = str(event.get("type") or "")
        if isinstance(stage, str) and stage and kind:
            stages[stage] = kind
    if stages:
        lines.append("  Stages")
        for stage, kind in stages.items():
            short = kind.rsplit(".", 1)[-1] if "." in kind else kind
            lines.append(f"    {stage}  {short}")
    if gates:
        lines.append("  Gates")
        for item in gates:
            record = item.get("record") if isinstance(item, dict) else None
            if not isinstance(record, dict):
                continue
            gate_id = str(record.get("gate_id") or record.get("id") or "?")
            decision = str(
                record.get("decision")
                or record.get("outcome")
                or record.get("result")
                or "?"
            )
            lines.append(f"    {gate_id}  {decision}")
    lines.append(f"  Evidence  events={len(events)}  gates={len(gates)}  receipts={len(receipts)}")
    if last_type:
        lines.append(f"  Last     {last_type}  {_created_stamp(last_at) or last_at}")
    return "\n".join(lines) + "\n"


class RunInspectorScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("e", "export_run", "Export"),
        Binding("v", "verify_bundle", "Verify"),
        Binding("a", "open_chat", "Ask"),
        Binding("x", "delete_run", "Delete"),
    ]
    CSS = """
    RunInspectorScreen { layout: vertical; }
    #ri-scroll { height: 1fr; }
    #ri-summary {
        height: auto;
        max-height: 14;
        border: solid #505050;
        background: #000000;
        padding: 0 1 1 1;
        overflow-y: auto;
    }
    """

    driver: StammtischDriver
    ai: AIDriver
    run_id: str

    def __init__(self, driver: StammtischDriver, ai: AIDriver, run_id: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.driver = driver
        self.ai = ai
        self.run_id = run_id
        self._snapshot: dict[str, Any] | None = None
        self._inspect_error: str | None = None
        self._inspect_token: object | None = None

    def compose(self) -> ComposeResult:
        short_id = self.run_id[:20] + "..." if len(self.run_id) > 20 else self.run_id
        yield Static(
            f"  Run: {short_id}  |  [E]xport [V]erify [A]sk [X] Delete [Esc] back",
            classes="header-bar",
            id="ri-header",
        )
        yield Static("", id="ri-status")
        with ScrollableContainer(id="ri-scroll"):
            with Vertical(classes="panel panel-green"):
                yield Static("  Manifest", classes="panel-title")
                yield DigestWidget(id="ri-digest")
                yield Static(id="ri-manifest")
            with Vertical(classes="panel"):
                yield Static("  Event Log", classes="panel-title")
                yield EventTimeline(id="ri-timeline")
            with Vertical(classes="panel panel-amber"):
                yield Static("  Gate Records", classes="panel-title")
                yield Vertical(id="ri-gates")
            with Vertical(classes="panel"):
                yield Static("  Receipts", classes="panel-title")
                yield Static(id="ri-receipts")
        yield Static("  Session summary", classes="panel-title")
        yield Static("  Loading session summary…\n", id="ri-summary", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._load()

    def _load(self) -> None:
        self.query_one("#ri-status", Static).update(
            "  Loading verified run snapshot..."
        )

        def _deliver(result) -> None:
            if isinstance(result, dict):
                self._show_inspect_error(str(result.get("error") or "inspect failed"))
                return
            if not result.ok:
                self._show_inspect_error(result.error_message or "inspect failed")
                return
            data = result.data
            required = {
                "manifest": dict,
                "events": list,
                "gates": list,
                "receipts": list,
            }
            if not isinstance(data, dict) or any(
                not isinstance(data.get(key), expected)
                for key, expected in required.items()
            ):
                self._show_inspect_error("inspect returned an invalid snapshot")
                return
            if any(not isinstance(event, dict) for event in data["events"]):
                self._show_inspect_error("inspect returned invalid events")
                return
            manifest = data["manifest"]
            if not isinstance(manifest.get("state"), dict) or not isinstance(
                manifest.get("pipeline"), dict
            ):
                self._show_inspect_error("inspect returned an invalid manifest")
                return
            if any(
                not isinstance(item, dict)
                or not isinstance(item.get("record"), dict)
                for item in data["gates"]
            ):
                self._show_inspect_error("inspect returned invalid gate records")
                return
            if any(
                not isinstance(item, dict)
                or not isinstance(item.get("receipt"), dict)
                for item in data["receipts"]
            ):
                self._show_inspect_error("inspect returned invalid receipts")
                return
            self._render_snapshot(data)

        # Inspect folds events and validates every returned evidence file;
        # rendering and AI analysis both reuse this one authoritative result.
        # Its token is independent of export/verify workers so invoking one
        # of those actions while this loads cannot silently discard it.
        inspect_token = object()
        self._inspect_token = inspect_token

        def _worker() -> None:
            try:
                result = self.driver.inspect(self.run_id)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}

            def _accept() -> None:
                if (
                    not self.is_mounted
                    or self._inspect_token is not inspect_token
                ):
                    return
                _deliver(result)

            try:
                self.app.call_from_thread(_accept)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _render_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self._inspect_error = None
        events = snapshot["events"]
        gates = snapshot["gates"]
        receipts = snapshot["receipts"]
        manifest = snapshot["manifest"]
        self.query_one("#ri-timeline", EventTimeline).events = events
        container = self.query_one("#ri-gates")
        container.remove_children()
        for item in gates:
            card = GateCard()
            card.gate = item["record"]
            container.mount(card)
        self.query_one("#ri-receipts", Static).update(
            "\n".join(
                f"  {item.get('file', '?')} "
                f"[{item['receipt'].get('schema', '?')}] "
                f"{str(item.get('sha256', ''))[:24]}..."
                for item in receipts
            ) if receipts else "  (none)"
        )
        md = self.query_one("#ri-digest", DigestWidget)
        mv = self.query_one("#ri-manifest", Static)
        s, p = manifest.get("state", {}), manifest.get("pipeline", {})
        lines = [
            f"  State: {s.get('code', '?')}",
            f"  Pipeline: {p.get('id', '?')}",
            f"  Canonical: {str(p.get('canonical_sha256', '?'))[:32]}...",
        ]
        if manifest.get("created_at"):
            lines.append(f"  Created: {manifest['created_at']}")
        terminal = manifest.get("terminal", {})
        if terminal:
            lines.append(
                f"  Terminal: {terminal.get('code', '?')} @ {terminal.get('at', '?')}"
            )
        mv.update("\n".join(lines))
        md.digest = p.get("canonical_sha256", "")
        title = run_session_title({
            "pipeline_id": p.get("id"),
            "created_at": manifest.get("created_at"),
            "run_id": self.run_id,
        })
        short_id = self.run_id[:20] + "..." if len(self.run_id) > 20 else self.run_id
        self.query_one("#ri-header", Static).update(
            f"  {title}  ·  {short_id}  |  [E]xport [V]erify [A]sk [X] Delete [Esc] back"
        )
        self.query_one("#ri-status", Static).update(
            f"  Verified snapshot: {len(events)} events, "
            f"{len(gates)} gates, {len(receipts)} receipts"
        )
        self.query_one("#ri-summary", Static).update(
            format_run_session_summary(self.run_id, snapshot)
        )

    def _show_inspect_error(self, error: str) -> None:
        self._snapshot = None
        self._inspect_error = error
        self.query_one("#ri-status", Static).update(
            f"  CORRUPT / UNAVAILABLE: {error}"
        )
        self.query_one("#ri-manifest", Static).update(
            "  Run evidence could not be verified by the core."
        )
        self.query_one("#ri-digest", DigestWidget).digest = ""
        self.query_one("#ri-timeline", EventTimeline).events = []
        self.query_one("#ri-gates").remove_children()
        self.query_one("#ri-receipts", Static).update("  (unavailable)")
        self.query_one("#ri-summary", Static).update(
            "  Session summary unavailable: run evidence could not be verified.\n"
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_unmount(self) -> None:
        self._inspect_token = None

    def action_delete_run(self) -> None:
        """Delete this run (with confirmation)."""
        run_id = self.run_id
        short = run_id[:16] + "..."

        def _confirmed():
            def _deliver(r):
                if not self.is_mounted:
                    return
                if isinstance(r, dict):
                    self.notify(f"Delete failed: {r.get('error')}", severity="error")
                elif r.ok:
                    self.app.pop_screen()  # back to the (now updated) dashboard
                    self.notify(f"Deleted run {short}", severity="information")
                else:
                    self.notify(f"Delete failed: {r.error_message}", severity="error")

            def _worker():
                try:
                    result = self.driver.delete(run_id)
                except Exception as e:
                    result = {"ok": False, "error": str(e)}
                try:
                    self.app.call_from_thread(_deliver, result)
                except Exception:
                    pass

            threading.Thread(target=_worker, daemon=True).start()

        self.app.push_screen(ConfirmScreen(
            f"  Delete run {short}?\n\n  This removes the run directory and its evidence.\n\n"
            f"  [Y] Confirm  [N]/[Esc] Cancel",
            _confirmed,
        ))

    def action_export_run(self) -> None:
        import tempfile
        out = tempfile.mkdtemp(prefix="stammtisch-bundle-")

        def _deliver(r):
            if r.ok:
                self.notify(f"Exported: {out}", severity="information")
            else:
                self.notify(f"Failed: {r.error_message}", severity="error")

        # export can take tens of seconds: keep it off the UI thread.
        _run_async(self, lambda: self.driver.export(self.run_id, out), _deliver)

    def action_verify_bundle(self) -> None:
        import tempfile
        out = tempfile.mkdtemp(prefix="stammtisch-bundle-")

        def _verify():
            r = self.driver.export(self.run_id, out)
            if not r.ok:
                return r
            return self.driver.verify(out)

        def _deliver(vr):
            if vr.ok:
                d = vr.data
                self.notify(f"Verified: {d.get('entries')} entries, {d.get('receipts')} receipts", severity="information")
            else:
                self.notify(f"Failed: {vr.error_message}", severity="error")

        # export + verify both shell out: keep them off the UI thread.
        _run_async(self, _verify, _deliver)

    def action_open_chat(self) -> None:
        if not self.ai.available:
            self.notify("AI API key not set.", severity="error")
            return
        self.app.push_screen(ChatScreen(self.ai))



# ═══════════════════════════════════════════════════════════════════
# Validate
# ═══════════════════════════════════════════════════════════════════


class ValidateScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]
    CSS = "ValidateScreen { layout: vertical; }"
    driver: StammtischDriver

    def __init__(self, driver: StammtischDriver, **kwargs: Any):
        super().__init__(**kwargs)
        self.driver = driver

    def compose(self) -> ComposeResult:
        yield Static("  Pipeline Validation  |  Select a pipeline  |  Esc back", classes="header-bar")
        with ScrollableContainer():
            with Vertical(classes="panel"):
                yield Static("  Select Pipeline", classes="panel-title")
                options = [Option(p.stem, id=str(p)) for p in self.driver.list_pipelines()]
                yield OptionList(*options, id="val-select") if options else Static("  No pipelines.")
            with Vertical(classes="panel"):
                yield Static("  Result", classes="panel-title")
                yield Static("  Select a pipeline to validate.", id="val-output")
        yield Footer()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # validate shells out to the core binary (bounded by its own
        # timeout): never on the UI thread. Picking another pipeline drops
        # the older pending result as stale, which is what we want here.
        self.query_one("#val-output", Static).update("  Validating…")
        _run_async(self, lambda: self.driver.validate(str(event.option_id)), self._show_result)

    def _show_result(self, r) -> None:
        out = self.query_one("#val-output", Static)
        if isinstance(r, dict):  # worker exception
            out.update(f"  [ERROR] {r.get('error')}")
        elif r.ok:
            d = r.data
            doc = d.get("doctrine", {})
            out.update(f"  [VALID] {d.get('pipeline_id')} | stages={d.get('stages')} | doctrine={doc.get('pack')} | gates={', '.join(d.get('gates', []))}")
        else:
            out.update(f"  [INVALID] {r.error_message}")

    def action_back(self) -> None:
        self.app.pop_screen()


# ═══════════════════════════════════════════════════════════════════
# Pipeline viewer
# ═══════════════════════════════════════════════════════════════════


class PipelineViewScreen(Screen):
    """Read-only view of a pipeline spec JSON.

    The spec is deliberately not editable from the TUI: pipelines are
    provenance roots, and edits happen in the editor of choice on disk.
    Esc goes back.
    """

    BINDINGS = [Binding("escape", "back", "Back")]
    CSS = """
    PipelineViewScreen { layout: vertical; }
    #pe-area { height: 1fr; border: solid #505050; }
    """

    def __init__(self, driver: StammtischDriver, path: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.driver = driver
        self.path = path

    def compose(self) -> ComposeResult:
        yield Static(
            f"  Pipeline: {self.path}  (read-only)  |  [Esc] Back",
            classes="header-bar",
        )
        yield Static(_pipeline_summary(self.path), id="pe-area")
        yield Footer()


def _pipeline_summary(path: str) -> str:
    """Render a pipeline spec as a readable summary — never a raw JSON dump."""
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"  unreadable spec: {e}"
    if not isinstance(spec, dict):
        return "  unreadable spec: top level is not a JSON object"
    lines = [f"  id: {spec.get('id', '?')}    schema: {spec.get('schema', '?')}"]
    doctrine = spec.get("doctrine")
    doctrine = doctrine if isinstance(doctrine, dict) else {}
    lines.append(f"  doctrine: {doctrine.get('pack', '?')}  ref: {doctrine.get('ref', '?')}")
    lines.append("")
    stages = spec.get("stages")
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, dict):
            continue
        lines.append(
            f"  {stage.get('id', '?'):<10} product={stage.get('product', '?'):<10} gate={stage.get('gate', '-')}"
        )
        io = []
        for field_name in ("in", "out"):
            values = stage.get(field_name)
            if isinstance(values, list) and values:
                io.append(f"{field_name}: " + ",".join(str(v) for v in values))
        if io:
            lines.append(f"  {'':<10} {'  '.join(io)}")
        runtime = stage.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        if runtime.get("endpoint"):
            lines.append(f"  {'':<10} runtime: {runtime.get('protocol', '?')} {runtime['endpoint']}")
        if stage.get("on_block"):
            lines.append(f"  {'':<10} on_block: {stage['on_block']}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# SECURITY — equity watchlist board by market zone (←/→)
# ═══════════════════════════════════════════════════════════════════


# Market zones in display order. Symbols classify by exchange suffix;
# anything unmatched lands in OTHER.
SECURITY_ZONES = ("A-SHARE", "HK", "US", "OTHER")


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
        Binding("a", "chat", "Ask"),
        Binding("b", "backtest", "Backtest"),
        Binding("e", "config", "Config"),
        Binding("f", "fetch", "Fetch"),
        Binding("p", "portfolio", "Portfolio"),
        Binding("s", "sentiment", "Sentiment"),
        Binding("t", "indicators", "Indicators"),
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
    #sec-recent-wrap { height: 14; border: solid #505050; background: #000000; }
    .sec-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
    """

    def __init__(self, driver, ai, engine, config, path, dashboard, **kwargs):
        super().__init__(**kwargs)
        self.driver, self.ai, self.engine, self.config = driver, ai, engine, config
        self.path = path
        self._dash = dashboard
        self._symbols: list[str] = list(config.security_symbols) if config else []
        self._zones: list[str] = []
        self._by_zone: dict[str, list[dict[str, Any]]] = {}
        self._zone_idx = 0

    def compose(self) -> ComposeResult:
        yield Static(
            "  SECURITY  |  [←→] Zone  [R] Refresh  [K] K-line  "
            "[B/F/T/P] Quant  [D/H/S] Daily  [A] Ask  [E] Config  [Esc] Back",
            classes="header-bar",
        )
        yield Static("", id="sec-cats")
        yield Static("  ", id="sec-fetch")
        with Vertical(id="sec-board-wrap"):
            yield Static("  Watchlist by market zone (daily)", classes="sec-label")
            yield _RowTable(id="sec-board", cursor_type="row")
        with Vertical(id="sec-recent-wrap"):
            yield Static("  Recent", classes="sec-label")
            yield DataTable(id="sec-recent", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        board = self.query_one("#sec-board", DataTable)
        board.add_columns("CODE", "LAST", "CHG%", "5D%", "20D%", "VOL")
        self.query_one("#sec-recent", DataTable).add_columns(
            "DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"
        )
        cached = self._cached_board()
        if cached is not None:
            self._zone_idx = int(cached.get("zone_idx") or 0)
            self._restore_selected = cached.get("selected")
            self._apply({"ok": True, "quotes": cached.get("quotes") or {}}, persist=False)
            self._set_fetch_status("cached — [R] refresh")
            return
        self.action_refresh()

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
        if not self._symbols:
            self.notify(
                "No security symbols configured (security_symbols).",
                severity="warning",
            )
        self._set_fetch_status("fetching…")
        _run_async(self, self._load, self._apply, dedup_key="security-board")

    def _load(self) -> dict[str, Any]:
        from .engine import _missing_ohlcv, _normalize_symbol

        quotes: dict[str, dict[str, Any]] = {}
        if not self._symbols:
            return {"ok": True, "quotes": quotes}
        try:
            from quantkit.data import fetch_ohlcv
        except ImportError:
            quotes = {symbol: {"error": "quantkit is not installed"}
                      for symbol in self._symbols}
            return {"ok": True, "quotes": quotes}
        start = (date.today() - timedelta(days=400)).isoformat()
        total = len(self._symbols)
        for index, raw in enumerate(self._symbols, 1):
            symbol = _normalize_symbol(raw)
            self._post_progress(f"fetching {index}/{total}  {symbol}")
            try:
                df = fetch_ohlcv(
                    symbol, market="auto", start=start,
                    data_dir=str(self.config.data_dir),
                )
            except Exception as exc:
                quotes[symbol] = {"error": str(exc)[:120]}
                continue
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
        self._render_strip()
        self._render_board()

    @staticmethod
    def _fmt_pct(value: float | None) -> str:
        if value is None:
            return "—"
        return f"{value:+.2f}%"

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
        items = self._by_zone.get(self._zones[self._zone_idx], []) if self._zones else []
        for item in items:
            if "error" in item:
                board.add_row(item["code"], item["error"], "", "", "", "",
                              key=item["key"])
                continue
            board.add_row(
                item["code"],
                "—" if item["last"] is None else f"{item['last']:,.2f}",
                self._fmt_pct(item["chg_pct"]),
                self._fmt_pct(item["pct5"]),
                self._fmt_pct(item["pct20"]),
                f"{item['volume']:.0f}",
                key=item["key"],
            )
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
        for bar in item.get("recent") or []:
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
    def action_backtest(self) -> None: self._dash.action_run_backtest()
    def action_intake(self) -> None: self._dash.action_open_intake()
    def action_config(self) -> None: self._dash.action_edit_config()
    def action_fetch(self) -> None: self._dash.action_fetch_data()
    def action_portfolio(self) -> None: self._dash.action_run_portfolio()
    def action_sentiment(self) -> None: self._dash.action_open_sentiment()
    def action_indicators(self) -> None: self._dash.action_show_indicators()


# ═══════════════════════════════════════════════════════════════════
# Ask (full screen, for longer conversations)
# ═══════════════════════════════════════════════════════════════════


_ASK_INTRO = (
    "  Ready. Ask about pipelines, gates, backtests, strategy analysis.\n\n"
)
# Clockwise box corners — not the Grok Build working spinner.
_LOAD_FRAMES = ("┌", "┐", "┘", "└")
_THINK_TICK = 0.12
_INPUT_HISTORY_CAP = 200
_SESSION_KEEP = 80


class RegistryTable(DataTable):
    """Run list with full-row highlight for the current selection.

    Click or arrow keys select one row. Shift+click / Shift+arrows select
    the range from the last anchor. Ctrl+A selects all. Selected rows use
    the same full-cell cursor background as the focused row.
    """

    BINDINGS = [
        Binding("ctrl+a", "screen.check_all", "Select all", show=False),
        Binding("shift+up", "screen.extend_up", "Extend", show=False),
        Binding("shift+down", "screen.extend_down", "Extend", show=False),
    ]

    def _selected_keys(self) -> set[str]:
        checked = getattr(self.screen, "_checked", None)
        return checked if isinstance(checked, set) else set()

    def _key_at(self, row_index: int) -> str:
        if row_index < 0:
            return ""
        try:
            value = self.ordered_rows[row_index].key.value
        except Exception:
            return ""
        return "" if value is None else str(value)

    def _row_is_selected(self, row_index: int) -> bool:
        key = self._key_at(row_index)
        return bool(key) and key in self._selected_keys()

    def _should_highlight(self, cursor, target_cell, type_of_cursor) -> bool:
        if super()._should_highlight(cursor, target_cell, type_of_cursor):
            return True
        if type_of_cursor == "row":
            return self._row_is_selected(target_cell.row)
        return False

    def refresh_selection(self) -> None:
        self._update_count += 1
        clear = getattr(self._get_styles_to_render_cell, "cache_clear", None)
        if callable(clear):
            clear()
        cache = getattr(self, "_row_render_cache", None)
        if cache is not None:
            cache.clear()
        self.refresh()

    def _apply_click(self, row_index: int, shift: bool) -> None:
        if row_index < 0:
            return
        key = self._key_at(row_index)
        handler = getattr(self.screen, "on_registry_row_click", None)
        if handler is not None and key:
            handler(key, shift=shift)

    def _follow_cursor(self) -> None:
        handler = getattr(self.screen, "on_registry_cursor_move", None)
        if handler is not None:
            handler()

    def action_cursor_up(self) -> None:
        super().action_cursor_up()
        self._follow_cursor()

    def action_cursor_down(self) -> None:
        super().action_cursor_down()
        self._follow_cursor()

    def action_page_up(self) -> None:
        super().action_page_up()
        self._follow_cursor()

    def action_page_down(self) -> None:
        super().action_page_down()
        self._follow_cursor()

    def action_scroll_home(self) -> None:
        super().action_scroll_home()
        self._follow_cursor()

    def action_scroll_end(self) -> None:
        super().action_scroll_end()
        self._follow_cursor()

    def _event_shift(self, event: Any) -> bool:
        if bool(getattr(event, "shift", False)):
            return True
        # Some terminals encode Shift on the button number (SGR +4).
        try:
            return bool(int(getattr(event, "button", 0)) & 4)
        except (TypeError, ValueError):
            return False

    def _row_from_event(self, event: Any) -> int | None:
        meta = getattr(getattr(event, "style", None), "meta", None) or {}
        row_index = meta.get("row")
        if row_index is None or int(row_index) < 0:
            return None
        return int(row_index)

    async def _on_mouse_down(self, event) -> None:
        row_index = self._row_from_event(event)
        if row_index is None:
            await super()._on_mouse_down(event)
            return
        from textual.coordinate import Coordinate

        column = 0
        meta = getattr(getattr(event, "style", None), "meta", None) or {}
        try:
            column = int(meta.get("column") or 0)
        except (TypeError, ValueError):
            column = 0
        self.cursor_coordinate = Coordinate(row_index, column)
        event.stop()
        self._ignore_next_click = True
        self._apply_click(row_index, self._event_shift(event))

    async def _on_click(self, event) -> None:
        if getattr(self, "_ignore_next_click", False):
            self._ignore_next_click = False
            event.stop()
            return
        row_index = self._row_from_event(event)
        if row_index is None:
            await super()._on_click(event)
            return
        from textual.coordinate import Coordinate

        meta = getattr(getattr(event, "style", None), "meta", None) or {}
        self.cursor_coordinate = Coordinate(row_index, int(meta.get("column") or 0))
        event.stop()
        self._apply_click(row_index, self._event_shift(event))


def run_session_parts(run: dict[str, Any]) -> tuple[str, str]:
    """Name and aligned timestamp for a registry row."""
    explicit = run.get("title")
    if isinstance(explicit, str) and explicit.strip():
        name = " ".join(explicit.split())
    else:
        name = str(run.get("pipeline_id") or "").strip() or "run"
    stamp = _created_stamp(run.get("created_at"))
    if not stamp:
        rid = str(run.get("run_id") or "")
        stamp = rid[:8]
    return name, stamp


def run_session_title(run: dict[str, Any]) -> str:
    """Single-line title kept for tests and inspector headers."""
    name, stamp = run_session_parts(run)
    return f"{name}  {stamp}".rstrip() if stamp else name


def _intake_session_parts(session: dict[str, Any]) -> tuple[str, str]:
    title = str(session.get("title") or "daily-intake")
    name = title.split(" · ", 1)[0].strip() or "daily-intake"
    when = _created_stamp(session.get("started_at") or session.get("updated_at"))
    day = str(session.get("date") or "")
    if not when and len(day) == 8:
        when = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    return name, when


def _created_stamp(created: Any) -> str:
    text = str(created or "").strip()
    if not text:
        return ""
    if "T" in text:
        day, rest = text.split("T", 1)
        hhmm = rest[:5]
        return f"{day} {hhmm}" if len(hhmm) == 5 else day
    return text[:16]


def _thinking_line(elapsed: int, frame: int) -> str:
    return f"  {_LOAD_FRAMES[frame % len(_LOAD_FRAMES)]}  thinking  {elapsed}s\n"


def _thought_line(elapsed: int) -> str:
    return f"  ·  {elapsed}s\n"


def _ask_dir(app: Any | None = None) -> Path:
    if app is not None:
        driver = getattr(app, "driver", None)
        root = getattr(driver, "state_root", None) if driver is not None else None
        if root:
            return Path(root) / "ask"
        config = getattr(app, "config", None)
        cfg_root = getattr(config, "state_root", None) if config is not None else None
        if cfg_root:
            return Path(cfg_root) / "ask"
    env = os.environ.get("STAMMTISCH_HOME")
    if env:
        return Path(env) / "ask"
    return Path.home() / ".local" / "share" / "stammtisch" / "ask"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _new_ask_session_id() -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{stamp}-{os.urandom(3).hex()}"


def _session_path(ask_dir: Path, session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    return ask_dir / "sessions" / f"{safe}.json"


def _empty_session(session_id: str | None = None) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "schema": "stammtisch.ask-session.v1",
        "id": session_id or _new_ask_session_id(),
        "created_at": now,
        "updated_at": now,
        "title": "",
        "turns": [],
    }


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_input_history(ask_dir: Path) -> list[str]:
    raw = _load_json(ask_dir / "input_history.json", [])
    if not isinstance(raw, list):
        return []
    return [
        str(item) for item in raw if isinstance(item, str) and item.strip()
    ][-_INPUT_HISTORY_CAP:]


def save_input_history(ask_dir: Path, items: list[str]) -> None:
    try:
        _atomic_write_json(ask_dir / "input_history.json", items[-_INPUT_HISTORY_CAP:])
    except OSError:
        pass


def load_ask_session(ask_dir: Path, session_id: str) -> dict[str, Any] | None:
    data = _load_json(_session_path(ask_dir, session_id), None)
    if not isinstance(data, dict) or not isinstance(data.get("turns"), list):
        return None
    data.setdefault("id", session_id)
    data.setdefault("title", "")
    return data


def save_ask_session(ask_dir: Path, session: dict[str, Any]) -> None:
    session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        _atomic_write_json(_session_path(ask_dir, str(session["id"])), session)
    except OSError:
        return
    rows = list_ask_sessions(ask_dir)
    for row in rows[_SESSION_KEEP:]:
        try:
            _session_path(ask_dir, row["id"]).unlink()
        except OSError:
            pass


def list_ask_sessions(ask_dir: Path) -> list[dict[str, Any]]:
    folder = ask_dir / "sessions"
    if not folder.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in folder.glob("*.json"):
        data = _load_json(path, None)
        if not isinstance(data, dict):
            continue
        sid = str(data.get("id") or path.stem)
        title = str(data.get("title") or "").strip()
        turns = data.get("turns") if isinstance(data.get("turns"), list) else []
        preview = ""
        texts: list[str] = [title, sid]
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            content = str(turn.get("content") or "").strip()
            if content:
                texts.append(content)
            if not preview and turn.get("role") == "user" and content:
                preview = content
        if not title:
            title = preview or sid
        rows.append({
            "id": sid,
            "title": title,
            "preview": preview,
            "updated_at": str(data.get("updated_at") or data.get("created_at") or ""),
            "turns": len(turns),
            "hay": " ".join(texts).lower(),
        })
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    return rows


def delete_ask_session(ask_dir: Path, session_id: str) -> bool:
    try:
        _session_path(ask_dir, session_id).unlink()
        return True
    except OSError:
        return False


def render_ask_session(session: dict[str, Any]) -> str:
    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    if not turns:
        return _ASK_INTRO
    parts: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = str(turn.get("content") or "")
        if role == "user":
            parts.append(f"  USER: {content}\n")
        elif role == "assistant":
            elapsed = turn.get("elapsed_s")
            if isinstance(elapsed, int):
                parts.append(_thought_line(elapsed))
            for event in turn.get("tool_events") or []:
                parts.append(f"  GALAHAD·tool: {event}\n")
            parts.append(f"  GALAHAD: {content}\n\n")
    return "".join(parts) if parts else _ASK_INTRO


class ChatInput(TextArea):
    """Multi-line chat box: Enter submits,
    Shift-Enter / Ctrl-J inserts a newline; height follows content.

    Up/Down move the caret inside the buffer. At the first/last line they
    step the sent-line history instead.
    """

    BINDINGS = [
        # The box keeps focus for the screen's lifetime, so the screen's
        # own PgUp/PgDn bindings never fire — route them from here.
        Binding("pageup", "screen.scroll_up", show=False),
        Binding("pagedown", "screen.scroll_down", show=False),
    ]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class HistoryStep(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            if self.text.strip():
                self.post_message(self.Submitted(self.text))
            return
        if event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "up" and self.cursor_at_first_line:
            event.prevent_default()
            event.stop()
            self.post_message(self.HistoryStep(-1))
            return
        if event.key == "down" and self.cursor_at_last_line:
            event.prevent_default()
            event.stop()
            self.post_message(self.HistoryStep(1))
            return
        await super()._on_key(event)


class ChatScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("pageup", "scroll_up", "PgUp"),
        Binding("pagedown", "scroll_down", "PgDn"),
        Binding("ctrl+o", "open_sessions", "Sessions"),
        Binding("ctrl+n", "new_session", "New"),
    ]
    CSS = """
    ChatScreen { layout: vertical; }
    #chat-scroll {
        height: 1fr;
        border: solid #505050;
        background: #000000;
        padding: 1 1 1 2;
        overflow-x: hidden;
        overflow-y: scroll;
        scrollbar-gutter: stable;
        scrollbar-size-vertical: 1;
    }
    #chat-messages { height: auto; background: #000000; }
    #chat-input { height: 3; border: solid #4fc3f7; }
    """
    ai: AIDriver
    _chat_log: str = ""

    def __init__(
        self,
        ai: AIDriver,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        initial_context: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.ai = ai
        self._asked_session_id = session_id
        # A screen can arrive with work to do (e.g. the captured daily
        # dataset handed to GALAHAD): fire one real chat turn on mount.
        self._initial_prompt = initial_prompt
        self._initial_context = initial_context
        self._ask_dir: Path | None = None
        self._session = _empty_session(session_id)
        self._chat_log = _ASK_INTRO
        self._pending: str | None = None
        self._pending_started = 0.0
        self._think_timer = None
        self._spin_frame = 0
        self._active_request: object | None = None
        self._input_history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""

    def compose(self) -> ComposeResult:
        yield Static(
            "  Ask  |  Enter send · Shift-Enter newline · ↑↓ history  "
            "|  Ctrl+O sessions · Ctrl+N new  |  PgUp/PgDn  |  Esc back",
            classes="header-bar",
        )
        yield ScrollableContainer(
            Static(_ASK_INTRO, id="chat-messages", markup=False),
            id="chat-scroll",
        )
        yield ChatInput(id="chat-input")

    def on_mount(self) -> None:
        self._ask_dir = _ask_dir(self.app)
        self._input_history = load_input_history(self._ask_dir)
        # A opens a fresh turn. Prior conversations stay in Ctrl+O.
        loaded = None
        if self._asked_session_id:
            loaded = load_ask_session(self._ask_dir, str(self._asked_session_id))
        if loaded is not None:
            self._session = loaded
        self._chat_log = render_ask_session(self._session)
        self._sync_model_history()
        self._refresh_log()
        self.query_one("#chat-input", ChatInput).focus()
        if self._initial_prompt:
            prompt, self._initial_prompt = self._initial_prompt, None
            context, self._initial_context = self._initial_context, None
            self.call_after_refresh(self._submit, prompt, context)

    def _scroller(self) -> ScrollableContainer:
        return self.query_one("#chat-scroll", ScrollableContainer)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_scroll_up(self) -> None:
        self._scroller().scroll_page_up()

    def action_scroll_down(self) -> None:
        self._scroller().scroll_page_down()

    def action_open_sessions(self) -> None:
        if self._ask_dir is None:
            self._ask_dir = _ask_dir(self.app)
        self.app.push_screen(AskSessionScreen(self._ask_dir, self._session.get("id")))

    def action_new_session(self) -> None:
        if self._active_request is not None:
            self.notify("Wait for the current answer before starting a new session.",
                        severity="warning")
            return
        self._persist()
        self._session = _empty_session()
        self._chat_log = _ASK_INTRO
        self._pending = None
        self._sync_model_history()
        self._persist()
        self._refresh_log()
        self.notify("New Ask session.")

    def open_session(self, session_id: str) -> None:
        if self._active_request is not None:
            self.notify("Wait for the current answer before switching sessions.",
                        severity="warning")
            return
        if self._ask_dir is None:
            self._ask_dir = _ask_dir(self.app)
        loaded = load_ask_session(self._ask_dir, session_id)
        if loaded is None:
            self.notify("Session not found.", severity="warning")
            return
        self._session = loaded
        self._chat_log = render_ask_session(loaded)
        self._pending = None
        self._sync_model_history()
        self._persist()
        self._refresh_log()

    def on_text_area_changed(self, event: ChatInput.Changed) -> None:
        lines = event.text_area.text.count("\n") + 1
        event.text_area.styles.height = min(10, max(3, lines + 2))

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        if self._active_request is not None:
            self.notify("Wait for the current answer before sending another.", severity="warning")
            return
        box = self.query_one("#chat-input", ChatInput)
        box.text = ""
        box.styles.height = 3
        self._submit(event.value.strip())

    def on_chat_input_history_step(self, event: ChatInput.HistoryStep) -> None:
        if not self._input_history:
            return
        box = self.query_one("#chat-input", ChatInput)
        if self._history_index is None:
            self._draft = box.text
            self._history_index = len(self._input_history)
        nxt = self._history_index + event.delta
        nxt = max(0, min(len(self._input_history), nxt))
        self._history_index = nxt
        text = self._draft if nxt == len(self._input_history) else self._input_history[nxt]
        box.text = text
        lines = text.count("\n") + 1
        box.styles.height = min(10, max(3, lines + 2))
        rows = text.split("\n") or [""]
        box.move_cursor((len(rows) - 1, len(rows[-1])))

    def _remember_input(self, query: str) -> None:
        if not query:
            return
        if not self._input_history or self._input_history[-1] != query:
            self._input_history.append(query)
            self._input_history = self._input_history[-_INPUT_HISTORY_CAP:]
        self._history_index = None
        self._draft = ""
        if self._ask_dir is not None:
            save_input_history(self._ask_dir, self._input_history)

    def _refresh_log(self) -> None:
        text = self._chat_log + (self._pending or "")
        self.query_one("#chat-messages", Static).update(text)
        # Follow the tail: new turns must stay visible without manual
        # scrolling; without this the viewport pins to the top of a growing
        # log and new answers look like the conversation stalled.
        self._scroller().scroll_end(animate=False)

    def _tick_thinking(self) -> None:
        if self._active_request is None:
            return
        self._spin_frame += 1
        elapsed = int(time.monotonic() - self._pending_started)
        self._pending = _thinking_line(elapsed, self._spin_frame)
        self._refresh_log()

    def _stop_thinking(self, request_token: object | None = None) -> None:
        if (
            request_token is not None
            and self._active_request is not request_token
        ):
            return
        timer = getattr(self, "_think_timer", None)
        if timer is not None:
            timer.stop()
            self._think_timer = None
        self._pending = None

    def _persist(self) -> None:
        if self._ask_dir is None:
            return
        save_ask_session(self._ask_dir, self._session)
        try:
            _atomic_write_json(
                self._ask_dir / "current.json",
                {"id": self._session.get("id")},
            )
        except OSError:
            pass

    def _sync_model_history(self) -> None:
        if not hasattr(self.ai, "history"):
            return
        try:
            from .ai_driver import SYSTEM_PROMPT, ChatMessage
            messages = [ChatMessage("system", SYSTEM_PROMPT)]
            for turn in self._session.get("turns") or []:
                if not isinstance(turn, dict):
                    continue
                role = turn.get("role")
                content = str(turn.get("content") or "")
                if role in ("user", "assistant") and content:
                    messages.append(ChatMessage(str(role), content))
            lock = getattr(self.ai, "_lock", None)
            if lock is not None:
                with lock:
                    self.ai.history = messages
            else:
                self.ai.history = messages
        except Exception:
            pass

    def on_unmount(self) -> None:
        self._stop_thinking()
        self._active_request = None
        self._persist()

    def _submit(self, query: str, context: str | None = None) -> bool:
        if not query:
            return False
        if self._active_request is not None:
            self.notify("Wait for the current answer before sending another.", severity="warning")
            return False

        self._remember_input(query)
        request_token = object()
        self._active_request = request_token
        if not self._session.get("title"):
            self._session["title"] = query.splitlines()[0][:80]
        self._session.setdefault("turns", []).append({"role": "user", "content": query})
        self._chat_log = render_ask_session(self._session)
        self._pending_started = time.monotonic()
        self._spin_frame = 0
        self._pending = _thinking_line(0, 0)
        self._think_timer = self.set_interval(_THINK_TICK, self._tick_thinking)
        self._persist()
        box = self.query_one("#chat-input", ChatInput)
        box.disabled = True
        self._refresh_log()

        def _query():
            try:
                r = self.ai.chat(query, context=context)
            except Exception as e:
                r = ChatResponse(content="", error=str(e))
            result = r.content if r.ok else f"Error: {r.error}"
            def _update():
                if (
                    not self.is_mounted
                    or self._active_request is not request_token
                ):
                    return
                elapsed = int(time.monotonic() - self._pending_started)
                self._stop_thinking(request_token)
                # Reasoning stays collapsed: only elapsed time is kept.
                self._session.setdefault("turns", []).append({
                    "role": "assistant",
                    "content": result,
                    "elapsed_s": elapsed,
                    "tool_events": list(r.tool_events or []),
                })
                self._chat_log = render_ask_session(self._session)
                self._active_request = None
                self._persist()
                box = self.query_one("#chat-input", ChatInput)
                box.disabled = False
                box.focus()
                self._refresh_log()
            try:
                self.app.call_from_thread(_update)
            except Exception:
                pass
        threading.Thread(target=_query, daemon=True).start()
        return True


class AskSessionScreen(Screen):
    """Search and reopen persisted Ask sessions."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("enter", "open_selected", "Open"),
        Binding("x", "delete_selected", "Delete"),
    ]
    CSS = """
    AskSessionScreen { layout: vertical; }
    #ask-query { height: 3; border: solid #4fc3f7; }
    #ask-sessions { height: 1fr; border: solid #505050; background: #000000; }
    """

    def __init__(self, ask_dir: Path, current_id: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.ask_dir = ask_dir
        self.current_id = str(current_id or "")
        self._rows: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static(
            "  ASK SESSIONS  |  type to search  |  Enter open  |  X delete  |  Esc back",
            classes="header-bar",
        )
        yield Input(placeholder="search title or text", id="ask-query")
        yield OptionList(id="ask-sessions")

    def on_mount(self) -> None:
        self._reload()
        self.query_one("#ask-query", Input).focus()

    def _reload(self, query: str = "") -> None:
        needle = query.strip().lower()
        rows = list_ask_sessions(self.ask_dir)
        if needle:
            rows = [row for row in rows if needle in row["hay"]]
        self._rows = rows
        options = self.query_one("#ask-sessions", OptionList)
        options.clear_options()
        if not rows:
            options.add_option(Option(Text("  No matching sessions."), id="empty", disabled=True))
            return
        for row in rows:
            mark = "*" if row["id"] == self.current_id else " "
            stamp = _created_stamp(row["updated_at"]) or row["updated_at"]
            title = row["title"].replace("\n", " ")[:60]
            options.add_option(
                Option(Text(f" {mark} {stamp}  {title}  ({row['turns']})"), id=row["id"])
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "ask-query":
            self._reload(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._open(event.option_id)

    def action_open_selected(self) -> None:
        options = self.query_one("#ask-sessions", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return
        option = options.get_option_at_index(highlighted)
        self._open(option.id)

    def action_delete_selected(self) -> None:
        options = self.query_one("#ask-sessions", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return
        option = options.get_option_at_index(highlighted)
        sid = option.id
        if not sid or sid == "empty":
            return

        def _confirmed() -> None:
            delete_ask_session(self.ask_dir, str(sid))
            self._reload(self.query_one("#ask-query", Input).value)
            self.notify(f"Deleted session {str(sid)[:16]}")

        self.app.push_screen(ConfirmScreen(
            f"  Delete Ask session {str(sid)[:16]}…?\n\n"
            f"  [Y] Confirm  [N]/[Esc] Cancel",
            _confirmed,
        ))

    def _open(self, session_id: str | None) -> None:
        if not session_id or session_id == "empty":
            return
        self.app.pop_screen()
        screen = self.app.screen
        if isinstance(screen, ChatScreen):
            screen.open_session(session_id)

    def action_back(self) -> None:
        self.app.pop_screen()


# ═══════════════════════════════════════════════════════════════════
# Config editor
# ═══════════════════════════════════════════════════════════════════


class ConfigScreen(Screen):
    """Edit workstation config — API key, model, workspace, backtest defaults."""

    # Known OpenAI-compatible providers: picking one fills Base URL + Model.
    AI_PROVIDERS = [
        ("GLM official (bigmodel v4)", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3"),
        ("DeepSeek official", "https://api.deepseek.com/v1", "deepseek-v4-pro"),
    ]
    PROXY_POLICY_KEYS = [
        ("proxy.off", "off"),
        ("proxy.all", "all"),
        ("proxy.poly", "poly"),
        ("proxy.energy", "energy"),
        ("proxy.custom", "custom"),
    ]
    DEFAULT_EGRESS_PORT = "17878"

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+s", "save", "Save"),
    ]
    CSS = """
    ConfigScreen { layout: vertical; }
    #cfg-scroll { height: 1fr; }
    .cfg-row { height: 3; }
    .cfg-label { width: 16; color: #a0a0a0; padding: 1 0; }
    .cfg-input { width: 1fr; }
    .cfg-row Select { width: 1fr; }
    #cfg-buttons { height: 3; padding: 0 1; align-horizontal: right; }
    #cfg-buttons Button { margin: 0 0 0 1; }
    """

    config: Any
    ai: AIDriver
    driver: StammtischDriver
    engine: QuantEngine

    def __init__(self, config: Any, ai: AIDriver, driver: StammtischDriver,
                 engine: QuantEngine, **kwargs: Any):
        super().__init__(**kwargs)
        self.config = config
        self.ai = ai
        self.driver = driver
        self.engine = engine

    def _provider_value(self) -> tuple[str, str] | None:
        """Map the configured base URL to a known provider preset."""
        base = self.config.ai_base_url
        for _label, preset_base, model in self.AI_PROVIDERS:
            if preset_base == base:
                return (preset_base, model)
        return None

    @property
    def _language(self) -> str:
        try:
            language = str(self.config.get("language", "en") or "en")
        except Exception:
            language = "en"
        return "zh" if language == "zh" else "en"

    def _chrome(self, key: str, fallback: str) -> str:
        from .lang import tr

        return tr(self._language, key, fallback)

    def _policy_value(self) -> str:
        poly = str(self.config.get("polymarket_proxy_url", "") or "")
        energy = str(self.config.get("energy_proxy_url", "") or "")

        def local(url: str) -> bool:
            return url.startswith("http://127.0.0.1:")

        if poly and energy:
            return "all" if local(poly) and local(energy) else "custom"
        if poly:
            return "poly" if local(poly) else "custom"
        if energy:
            return "energy" if local(energy) else "custom"
        return "off"

    def _egress_port(self) -> str:
        poly = str(self.config.get("polymarket_proxy_url", "") or "")
        if poly.startswith("http://127.0.0.1:"):
            port = poly.rsplit(":", 1)[-1]
            if port.isdigit():
                return port
        return self.DEFAULT_EGRESS_PORT

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "cfg-provider" and event.value:
            base, model = event.value
            self.query_one("#cfg-base-url", Input).value = base
            self.query_one("#cfg-model", Input).value = model
        elif event.select.id == "cfg-policy":
            self._apply_proxy_policy(event.value)

    def _apply_proxy_policy(self, policy: str) -> None:
        port = (
            self.query_one("#cfg-egress-port", Input).value.strip()
            or self.DEFAULT_EGRESS_PORT
        )
        url = f"http://127.0.0.1:{port}"
        poly = self.query_one("#cfg-polymarket-proxy", Input)
        energy = self.query_one("#cfg-energy-proxy", Input)
        if policy == "off":
            poly.value = ""
            energy.value = ""
        elif policy == "all":
            poly.value = url
            energy.value = url
        elif policy == "poly":
            poly.value = url
            energy.value = ""
        elif policy == "energy":
            poly.value = ""
            energy.value = url
        # "custom": leave both fields exactly as they are.

    def compose(self) -> ComposeResult:
        yield Static("  Config  |  [Ctrl+S] Save  [Esc] Cancel", classes="header-bar")
        with ScrollableContainer(id="cfg-scroll"):
            with Vertical(classes="panel"):
                yield Static(self._chrome("config.ai", "AI Service"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Provider", classes="cfg-label")
                    yield Select(
                        [("Custom", None)]
                        + [(label, (base, model)) for label, base, model in self.AI_PROVIDERS],
                        value=self._provider_value(),
                        allow_blank=False,
                        id="cfg-provider",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  API Key", classes="cfg-label")
                    yield Input(value=self.config.get("ai_api_key", ""), password=True,
                                placeholder="sk-...", id="cfg-key", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Base URL", classes="cfg-label")
                    yield Input(value=self.config.ai_base_url, id="cfg-base-url", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Model", classes="cfg-label")
                    yield Input(value=self.config.ai_model, id="cfg-model", classes="cfg-input")
            with Vertical(classes="panel"):
                yield Static(self._chrome("config.workspace", "Workspace"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  State Root", classes="cfg-label")
                    yield Input(value=self.config.state_root or "",
                                placeholder="~/.local/share/stammtisch", id="cfg-state-root", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Data Dir", classes="cfg-label")
                    yield Input(value=self.config.data_dir or "", id="cfg-data-dir", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Chart Port", classes="cfg-label")
                    yield Input(value=str(self.config.get("chart_port", 0)),
                                placeholder="0 = auto",
                                id="cfg-chart-port", classes="cfg-input")
            with Vertical(classes="panel"):
                yield Static(self._chrome("config.network", "Egress Proxies"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Policy", classes="cfg-label")
                    yield Select(
                        [
                            (self._chrome(key, value), value)
                            for key, value in self.PROXY_POLICY_KEYS
                        ],
                        value=self._policy_value(),
                        allow_blank=False,
                        id="cfg-policy",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Port", classes="cfg-label")
                    yield Input(value=self._egress_port(),
                                placeholder=self.DEFAULT_EGRESS_PORT,
                                id="cfg-egress-port", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Market Proxy", classes="cfg-label")
                    yield Input(
                        value=self.config.get("polymarket_proxy_url", ""),
                        placeholder="http://127.0.0.1:PORT (empty = off)",
                        id="cfg-polymarket-proxy",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Energy Proxy", classes="cfg-label")
                    yield Input(
                        value=self.config.get("energy_proxy_url", ""),
                        placeholder="http://127.0.0.1:PORT (empty = off)",
                        id="cfg-energy-proxy",
                        classes="cfg-input",
                    )
            with Vertical(classes="panel"):
                yield Static(self._chrome("config.energy", "Energy (EIA)"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  EIA API Key", classes="cfg-label")
                    yield Input(value=self.config.get("eia_api_key", ""), password=True,
                                placeholder="register free at eia.gov/opendata",
                                id="cfg-eia-key", classes="cfg-input")
            with Vertical(classes="panel"):
                yield Static(self._chrome("config.intake", "Daily Data Intake"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Intake Cmd", classes="cfg-label")
                    yield Input(
                        value=self.config.get("intake_cmd", ""),
                        placeholder="daily-data product command",
                        id="cfg-intake-cmd",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Data Workspace", classes="cfg-label")
                    yield Input(
                        value=self.config.workspace_root,
                        placeholder="~/.local/share/stammtisch/daily-data",
                        id="cfg-workspace-root",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Timeout (sec)", classes="cfg-label")
                    yield Input(
                        value=str(self.config.intake_timeout_seconds),
                        id="cfg-intake-timeout",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Report Builder", classes="cfg-label")
                    yield Input(
                        value=self.config.get("intake_report_builder", "ai"),
                        placeholder="ai or deterministic",
                        id="cfg-intake-builder",
                        classes="cfg-input",
                    )
            with Vertical(classes="panel"):
                yield Static(self._chrome("config.forecast", "Forecast"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Forecast Cmd", classes="cfg-label")
                    yield Input(value=self.config.get("kronos_cmd", ""),
                                placeholder="e.g. kronos-forecast (empty = off)",
                                id="cfg-kronos-cmd", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Horizon", classes="cfg-label")
                    yield Input(value=str(self.config.get("kronos_horizon", 20)),
                                id="cfg-kronos-horizon", classes="cfg-input")
            with Vertical(classes="panel"):
                yield Static(self._chrome("config.domains", "Domains"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Security Symbols", classes="cfg-label")
                    yield Input(
                        value=" ".join(self.config.security_symbols),
                        placeholder="601088.SS 1088.HK BTU (space or comma separated)",
                        id="cfg-security-symbols",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Futures Symbols", classes="cfg-label")
                    yield Input(
                        value=" ".join(self.config.futures_symbols),
                        placeholder="BZ=F CL=F (space or comma separated)",
                        id="cfg-futures-symbols",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Futures Cmd", classes="cfg-label")
                    yield Input(
                        value=self.config.get("futures_cmd", ""),
                        placeholder="fuel board command (empty = off)",
                        id="cfg-futures-cmd",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Shipping Cmd", classes="cfg-label")
                    yield Input(
                        value=self.config.get("shipping_cmd", ""),
                        placeholder="sgx board command (empty = directory view)",
                        id="cfg-shipping-cmd",
                        classes="cfg-input",
                    )
            with Vertical(classes="panel"):
                yield Static(self._chrome("config.backtest", "Backtest Defaults"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Strategy", classes="cfg-label")
                    yield Input(value=self.config.default_strategy, id="cfg-strategy", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Fast", classes="cfg-label")
                    yield Input(value=str(self.config.default_fast), id="cfg-fast", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Slow", classes="cfg-label")
                    yield Input(value=str(self.config.default_slow), id="cfg-slow", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Cost Tier", classes="cfg-label")
                    yield Input(value=self.config.default_cost_tier, id="cfg-cost-tier", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Rebalance", classes="cfg-label")
                    yield Input(value=self.config.default_rebalance, id="cfg-rebalance", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Lookback", classes="cfg-label")
                    yield Input(value=str(self.config.default_lookback), id="cfg-lookback", classes="cfg-input")
        with Horizontal(id="cfg-buttons"):
            yield Button("Save", id="cfg-save")
            yield Button("Cancel", id="cfg-cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#cfg-key", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-save":
            self.action_save()
        elif event.button.id == "cfg-cancel":
            self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_save(self) -> None:
        try:
            fast = int(self.query_one("#cfg-fast", Input).value.strip())
            slow = int(self.query_one("#cfg-slow", Input).value.strip())
            lookback = int(self.query_one("#cfg-lookback", Input).value.strip())
            horizon = int(self.query_one("#cfg-kronos-horizon", Input).value.strip())
            intake_timeout = int(self.query_one("#cfg-intake-timeout", Input).value.strip())
            chart_port = int(self.query_one("#cfg-chart-port", Input).value.strip() or "0")
        except ValueError:
            self.notify(
                "Fast/Slow/Lookback/Horizon/Timeout/Chart Port must be integers.",
                severity="error",
            )
            return
        if intake_timeout < 1:
            self.notify("Daily-data timeout must be positive.", severity="error")
            return
        if not 0 <= chart_port <= 65535:
            self.notify("Chart port must be 0 (auto) or 1-65535.", severity="error")
            return
        intake_builder = self.query_one("#cfg-intake-builder", Input).value.strip().lower()
        if intake_builder == "deepseek":
            intake_builder = "ai"  # legacy alias for the LLM editorial pass
        if intake_builder not in {"ai", "deterministic"}:
            self.notify(
                "Report builder must be 'ai' or 'deterministic'.", severity="error"
            )
            return

        data_dir = self.query_one("#cfg-data-dir", Input).value.strip()
        if data_dir:
            # Probe before saving: an unusable path must fail the save, not
            # kill the app from the hot-apply mkdir below.
            try:
                Path(data_dir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.notify(f"Data dir is not usable: {exc}", severity="error")
                return

        try:
            self.config.update({
                "ai_api_key": self.query_one("#cfg-key", Input).value.strip(),
                "ai_base_url": self.query_one("#cfg-base-url", Input).value.strip() or self.config.ai_base_url,
                "ai_model": self.query_one("#cfg-model", Input).value.strip() or self.config.ai_model,
                "state_root": self.query_one("#cfg-state-root", Input).value.strip(),
                "data_dir": data_dir,
                "chart_port": chart_port,
                "polymarket_proxy_url": self.query_one("#cfg-polymarket-proxy", Input).value.strip(),
                "eia_api_key": self.query_one("#cfg-eia-key", Input).value.strip(),
                "energy_proxy_url": self.query_one("#cfg-energy-proxy", Input).value.strip(),
                "intake_cmd": self.query_one("#cfg-intake-cmd", Input).value.strip(),
                "workspace_root": self.query_one("#cfg-workspace-root", Input).value.strip()
                                  or self.config.workspace_root,
                "intake_timeout_seconds": intake_timeout,
                "intake_report_builder": intake_builder,
                "default_strategy": self.query_one("#cfg-strategy", Input).value.strip() or self.config.default_strategy,
                "default_fast": fast,
                "default_slow": slow,
                "default_cost_tier": self.query_one("#cfg-cost-tier", Input).value.strip() or self.config.default_cost_tier,
                "default_rebalance": self.query_one("#cfg-rebalance", Input).value.strip() or self.config.default_rebalance,
                "default_lookback": lookback,
                "kronos_cmd": self.query_one("#cfg-kronos-cmd", Input).value.strip(),
                "kronos_horizon": horizon,
                "futures_symbols": [
                    part.upper()
                    for part in self.query_one("#cfg-futures-symbols", Input)
                    .value.replace(",", " ").split()
                    if part.strip()
                ],
                "security_symbols": [
                    part.upper()
                    for part in self.query_one("#cfg-security-symbols", Input)
                    .value.replace(",", " ").split()
                    if part.strip()
                ],
                "futures_cmd": self.query_one("#cfg-futures-cmd", Input).value.strip(),
                "shipping_cmd": self.query_one("#cfg-shipping-cmd", Input).value.strip(),
            })
        except OSError as exc:
            self.notify(f"Config could not be saved: {exc}", severity="error")
            return

        # Hot-apply to the live drivers so no restart is needed.
        self.ai.api_key = self.config.ai_api_key
        self.ai.base_url = self.config.ai_base_url
        self.ai.model = self.config.ai_model
        if self.config.state_root:
            self.driver.state_root = self.config.state_root
        if self.config.data_dir:
            self.engine.data_dir = Path(self.config.data_dir)

        self.notify(f"Config saved: {self.config.path}", severity="information")
        self.app.pop_screen()


# ═══════════════════════════════════════════════════════════════════
# Help
# ═══════════════════════════════════════════════════════════════════


class ConfirmScreen(Screen):
    """Yes/No modal for destructive actions — keyboard AND mouse.

    Keyboard: y/Enter confirm, n/Esc cancel. Mouse: real buttons (the
    confirm button gets focus on mount, so Tab/Enter also work).
    """

    BINDINGS = [
        Binding("y", "confirm", "Confirm"),
        Binding("enter", "confirm", "Confirm"),
        Binding("n", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box { width: 62; background: #000000; border: solid #ef5350; padding: 1 2; }
    #confirm-buttons { height: 3; padding: 0 1; align-horizontal: center; }
    #confirm-buttons Button { margin: 0 1; min-width: 18; }
    """

    def __init__(self, prompt: str, on_confirm, **kwargs: Any):
        super().__init__(**kwargs)
        self.prompt = prompt
        self.on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.prompt, id="confirm-text")
            with Horizontal(id="confirm-buttons"):
                yield Button("Y — Confirm", variant="error", id="confirm-yes")
                yield Button("N — Cancel", variant="default", id="confirm-no")

    def on_mount(self) -> None:
        # Focus the destructive button so the screen has an interactive
        # focus target from the first keystroke.
        self.query_one("#confirm-yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._finish(event.button.id == "confirm-yes")

    def action_confirm(self) -> None:
        self._finish(True)

    def action_cancel(self) -> None:
        self._finish(False)

    def _finish(self, confirmed: bool) -> None:
        self.app.pop_screen()
        if confirmed:
            self.on_confirm()


class HelpScreen(Screen):
    BINDINGS = [("escape", "back", "Close"), ("q", "back", "Close")]
    CSS = "HelpScreen { align: center middle; } #help-box { width: 70; height: auto; max-height: 80%; background: #000000; border: solid #505050; padding: 1 2; }"

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(
                "  STAMMTISCH Help\n\n"
                "  Plugins (middle list, alphabetical): arrows / Enter / click.\n"
                "    SECURITY (equity board by market zone, ←/→ switches,\n"
                "    quant + daily hotkeys),\n"
                "    CRYPTO (Polymarket tape), ENERGY (EIA watchlist, read-only),\n"
                "    FUTURES (category board: continuous + exchange-settled,\n"
                "    ←/→ switches category, K = browser chart, B/F/T/P quant),\n"
                "    CASINO (wagerkit race board via racing_cmd),\n"
                "    SHIPPING (exchange settlement board via shipping_cmd),\n"
                "    other domain plugins from config 'plugins' (directory view).\n\n"
                "  Quick Start (bottom list):\n"
                "    a  Ask (GALAHAD chat)      e  Edit config\n\n"
                "  Dashboard keys: a Ask · e Edit · Del delete · Shift+D delete all · q Quit.\n"
                "    Click selects one run; Shift+click selects the range; Ctrl+A selects all.\n"
                "    Enter inspects the selected run. Session titles are pipeline + time.\n"
                "    FULLSTACK is the example pipeline, not a sidebar workbench.\n\n"
                "  Chat: Enter send · Shift-Enter newline · ↑↓ history · Ctrl+O sessions\n"
                "    Ctrl+N new session · PgUp/PgDn scroll. Thinking is collapsed with time.\n\n"
                "  Daily: R captures the last session before the A-share open,\n"
                "    otherwise today's session. Progress stays on Home.\n\n"
                "  Press Esc to close"
            )

    def action_back(self) -> None:
        self.app.pop_screen()
