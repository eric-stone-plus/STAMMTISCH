"""Dashboard — the main screen, always shown first."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, OptionList, Static
from rich.text import Text
from textual.widgets.option_list import Option

from ..driver import StammtischDriver
from ..ai_driver import AIDriver
from ..engine import QuantEngine
from ..analysis import DataFetchScreen, BacktestScreen, IndicatorsScreen, PortfolioScreen, GatesScreen
from ..analysis import _run_async
from ..widgets import (
    status_badge, SystemHud,
)

import logging

logger = logging.getLogger(__name__)

from .chat import ChatScreen
from .config_screen import ConfigScreen
from .daily_intake import DailyIntakeScreen, _history_store
from .domains import DomainBrowserScreen, FuturesScreen, SecurityScreen, ShippingScreen
from .modals import ConfirmScreen
from .runs import PipelineRunScreen, PipelineViewScreen, RunInspectorScreen, ValidateScreen
from .sessions import _intake_session_parts, run_session_parts

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
        # Quit lives only on the dashboard: Esc with an explicit confirm,
        # so a stray keystroke never leaves the workstation.
        Binding("escape", "confirm_quit", "Quit"),
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
                # Plain container: PipelineList draws its own frame — an
                # outer panel here nests two borders (eric, 2026-08-26).
                with Vertical():
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
                with Vertical(classes="panel"):
                    yield Static("  Market Glance", classes="panel-title")
                    yield Static("  …", id="dash-glance", markup=False)

    def _glance_tick(self) -> None:
        """Sidebar market glance: index snapshot + tape stance, sourced."""
        if not self.is_mounted:
            return

        def _work() -> dict:
            from .. import ccifeed, livefeed as lf, signals as sig

            out: dict = {"quotes": lf.fetch_batch(
                ["000001.SS", "HSI", "QQQ"]), "cci": None, "sent": {}}
            try:
                feed = ccifeed.feed()
                snap = feed.snapshot.get("100001.CCI")
                if snap:
                    out["cci"] = snap
            except Exception:
                pass
            try:
                root = Path(str(getattr(self.config, "reports_root", "") or ""))
                for market in ("hk", "us"):
                    stance = sig.sentiment_stance(
                        market, root if str(root) else None)
                    if stance:
                        out["sent"][market] = stance
            except Exception:
                pass
            return out

        def _deliver(result: dict) -> None:
            if not self.is_mounted:
                return
            lines = []
            labels = {"000001.SS": "SH COMP", "HSI": "HSI", "QQQ": "QQX(US)"}
            for sym, label in labels.items():
                q = (result.get("quotes") or {}).get(sym)
                if not q:
                    continue
                chg = ((q["last"] / q["prev_close"] - 1) * 100
                       if q.get("prev_close") else 0.0)
                lines.append(
                    f"  {label:<8} {q['last']:>10,.0f}  {chg:+5.2f}%")
            cci = result.get("cci")
            if cci and cci.get("last") is not None:
                lines.append(f"  {'CCI':<8} {cci['last']:>10,.0f}"
                             f"  {float(cci.get('chg_pct') or 0):+5.2f}%")
            for market, stance in (result.get("sent") or {}).items():
                lines.append(f"  tape {market.upper():<4} {stance.get('stance')}"
                             f" {stance.get('score'):+.2f}")
            if lines:
                lines.append("  src: qt.gtimg.cn · ccidx · fin-daily")
            try:
                self.query_one("#dash-glance", Static).update(
                    "\n".join(lines) or "  (no data)")
            except Exception:
                pass

        _run_async(self, _work, _deliver, dedup_key="dash-glance")

    @property
    def _language(self) -> str:
        try:
            language = str(self.config.get("language", "en") or "en")
        except Exception:
            language = "en"
        return "zh" if language == "zh" else "en"

    def _chrome_text(self, key: str, fallback: str) -> str:
        from ..lang import tr

        return tr(self._language, key, fallback)

    def _quick_options(self) -> list[Option]:
        """Quick Start rows for the current language."""
        from ..lang import tr

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
        from ..lang import tr

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
        self.set_interval(30.0, self._glance_tick)
        # No pre-selected row in Quick Start: hover feedback only on demand.
        self.query_one("#quick-list", OptionList).highlighted = None
        self._relabel()
        table = self.query_one("#run-table", DataTable)
        table.add_columns("SESSION", "TIME", "STATE")
        self.set_interval(2.0, self._refresh_intake_rows)
        self.action_refresh()

    def action_open_crawlers(self) -> None:
        from ..crawlers import CrawlerPanelScreen

        self.app.push_screen(CrawlerPanelScreen(self.config))

    def _display_tz(self) -> str:
        try:
            return str(self.config.get("display_timezone") or "")
        except Exception:
            return ""

    def _intake_session_rows(self) -> list[dict[str, str]]:
        from ..intake_job import list_sessions, supervisor_for

        tz = self._display_tz()
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
                name, when = _intake_session_parts(live, tz)
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
                name, when = _intake_session_parts(session, tz)
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
        tz = self._display_tz()
        rows = list(self._intake_session_rows())
        for run in runs:
            created_raw = str(run.get("created_at") or "")
            created = created_raw.split("T")[0] if "T" in created_raw else created_raw
            run_id = str(run.get("_full_id") or run.get("run_id") or "")
            name, when = run_session_parts(run, tz)
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
        entries += [("ENERGY", "domain:ENERGY")]
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
                # Periodic refresh workers routinely race app teardown on
                # exit; that is the normal shutdown path, not an error.
                logger.debug("call_from_thread skipped (app torn down)")

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
                from ..racing import CasinoScreen

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
                from ..intake_job import delete_session, supervisor_for

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
        from ..intake_job import delete_session, supervisor_for

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
        if self.config.state_root and not getattr(
            self.app, "state_root_override", None
        ):
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
        from ..brief import SentimentScreen, load_daily, load_daily_path

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
        from ..polymarket import CryptoScreen

        proxy_url = self.config.get("polymarket_proxy_url") if self.config else None
        self.app.push_screen(CryptoScreen(proxy_url=proxy_url, config=self.config))

    def action_open_energy(self) -> None:
        """ENERGY module — read-only EIA Open Data watchlist."""
        from ..energy import EnergyScreen

        api_key = self.config.get("eia_api_key") if self.config else None
        proxy_url = self.config.get("energy_proxy_url") if self.config else None
        self.app.push_screen(EnergyScreen(api_key=api_key, proxy_url=proxy_url))

    def action_open_chart(self) -> None:
        """Open the browser K-line timeseries viewer (starts the local
        chart server on first use)."""
        import webbrowser

        from ..chart_server import DEFAULT_PORT, ensure_running

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

    def action_confirm_quit(self) -> None:
        from .modals import ConfirmScreen

        self.app.push_screen(
            ConfirmScreen("Quit STAMMTISCH?", self.app.exit)
        )

    def action_eval_gates(self) -> None:
        self.app.push_screen(GatesScreen(self.engine, self.config))
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
