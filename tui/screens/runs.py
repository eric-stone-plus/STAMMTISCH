"""Pipeline run, inspector, validation, and read-only pipeline views."""

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

from .chat import ChatScreen
from .modals import ConfirmScreen
from .sessions import format_run_session_summary, run_session_title

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
        app_config = getattr(self.app, "config", None)
        tz_name = ""
        if app_config is not None:
            try:
                tz_name = str(app_config.get("display_timezone") or "")
            except Exception:
                tz_name = ""
        title = run_session_title({
            "pipeline_id": p.get("id"),
            "created_at": manifest.get("created_at"),
            "run_id": self.run_id,
        }, tz_name)
        short_id = self.run_id[:20] + "..." if len(self.run_id) > 20 else self.run_id
        self.query_one("#ri-header", Static).update(
            f"  {title}  ·  {short_id}  |  [E]xport [V]erify [A]sk [X] Delete [Esc] back"
        )
        self.query_one("#ri-status", Static).update(
            f"  Verified snapshot: {len(events)} events, "
            f"{len(gates)} gates, {len(receipts)} receipts"
        )
        self.query_one("#ri-summary", Static).update(
            format_run_session_summary(self.run_id, snapshot, tz_name)
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
