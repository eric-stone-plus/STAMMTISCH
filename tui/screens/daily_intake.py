"""Daily intake screen and report-history helpers."""

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
        from ..intake_job import report_session_date, today_yyyymmdd

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
        from ..intake_job import render_progress, supervisor_for

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
        from ..intake_job import load_session, render_progress

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
        from ..intake_job import render_progress, supervisor_for

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
                from ..intake_job import supervisor_for

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
        from ..intake_job import render_progress, supervisor_for

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

        from ..intake import IntakeDriver

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
        from ..intake_job import supervisor_for

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
        from ..brief import load_daily_path

        doc = load_daily_path(json_path, expected_date=day)
        if not doc.get("ok"):
            self.notify(str(doc.get("error") or "report JSON is invalid"), severity="error")
            return
        _galahad_report_analysis(self, doc)
def _report_digest(doc: dict[str, Any]) -> str:
    """Compact, faithful digest of one captured day for GALAHAD."""
    from ..brief import SUMMARY_CAP, TITLE_CAP, clean_text, curate_items, is_displayable_item

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
    from ..lang import tr

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
    from ..history import HistoryStore

    try:
        store = HistoryStore(config.history_db)
        store.index_all(
            config.workspace_root,
            str(config.get("reports_root") or "") or None,
        )
    except Exception as exc:
        return None, str(exc)
    return store, None
