"""Durable daily-intake sessions that survive leaving the Daily screen.

The product command still emits one envelope at the end.  While it runs the
supervisor watches the workspace (staging, captures, normalized markdown)
and writes a session file the dashboard can list.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA = "stammtisch.intake-session.v1"
_KEEP = 40


def session_dir(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser() / "intake-sessions"


def session_path(workspace_root: str | Path, session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    return session_dir(workspace_root) / f"{safe}.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def new_session_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + os.urandom(3).hex()


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def today_yyyymmdd() -> str:
    return datetime.now(_SHANGHAI).strftime("%Y%m%d")


def report_session_date(now: datetime | None = None) -> str | None:
    """Date stamped on a desk capture, or None when it cannot be known.

    Pre-open of the next session (or a closed day) belongs to the previous
    trading date; after the A-share open, the capture is that session. Both
    answers need the offline exchange calendar. Without it a weekday or
    fixed-clock guess is exactly what the intake contract forbids
    (finance/reports/README.md), so the caller leaves the session date to
    the product: no ``--date`` is passed and the accepted envelope's
    certified date is adopted instead.
    """
    current = now or datetime.now(_SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_SHANGHAI)
    else:
        current = current.astimezone(_SHANGHAI)
    return _calendar_session_date(current)


def _calendar_session_date(current: datetime) -> str | None:
    try:
        import exchange_calendars as xcals
        import pandas as pd
    except ImportError:
        return None
    try:
        calendar = xcals.get_calendar("XSHG")
        day = pd.Timestamp(current.date())
        if calendar.is_session(day):
            opened = calendar.session_open(day)
            if getattr(opened, "tzinfo", None) is None:
                opened = opened.tz_localize("UTC")
            opened_local = opened.tz_convert("Asia/Shanghai").to_pydatetime()
            if current >= opened_local:
                return current.strftime("%Y%m%d")
        previous = calendar.previous_session(day)
        return previous.strftime("%Y%m%d")
    except Exception:
        return None


def session_title(date: str, started_at: str | None = None) -> str:
    # Display label only; an unresolved session date falls back to today.
    day = date if len(date) == 8 else (report_session_date() or today_yyyymmdd())
    pretty = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    clock = ""
    stamp = started_at or ""
    if "T" in stamp:
        clock = stamp.split("T", 1)[1][:5]
    if not clock:
        clock = datetime.now(_SHANGHAI).strftime("%H:%M")
    return f"daily-intake · {pretty} · {clock}"


def empty_session(workspace_root: str | Path, date: str | None = None) -> dict[str, Any]:
    # An unresolved date stays empty: it is display/sort state only, the
    # progress watcher treats "" as "no date filter", and the accepted
    # envelope's certified date replaces it in `_finish`.
    day = date or report_session_date() or ""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "schema": SCHEMA,
        "id": new_session_id(),
        "title": session_title(day, now),
        "date": day,
        "state": "capturing",
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
        "elapsed_s": 0,
        "summary": "starting",
        "lines": [],
        "error": None,
        "workspace_root": str(Path(workspace_root).expanduser()),
    }


def load_session(workspace_root: str | Path, session_id: str) -> dict[str, Any] | None:
    path = session_path(workspace_root, session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_session(session: dict[str, Any]) -> None:
    root = session.get("workspace_root") or ""
    sid = str(session.get("id") or "")
    if not root or not sid:
        return
    session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        _atomic_write_json(session_path(root, sid), session)
    except OSError:
        return
    folder = session_dir(root)
    rows = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in rows[_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass


def delete_session(workspace_root: str | Path, session_id: str) -> bool:
    try:
        session_path(workspace_root, session_id).unlink()
        return True
    except OSError:
        return False


def list_sessions(workspace_root: str | Path) -> list[dict[str, Any]]:
    folder = session_dir(workspace_root)
    if not folder.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("id"):
            rows.append(data)
    rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    return rows


def observe_workspace(workspace_root: str | Path, started: float, date: str) -> dict[str, Any]:
    """Derive live progress from append-only workspace files."""
    root = Path(workspace_root)
    cutoff = started - 2.0
    events: list[tuple[float, str]] = []
    staging_run = ""
    accepted = 0

    staging = root / ".intake-staging"
    if staging.is_dir():
        for run_dir in staging.iterdir():
            if not run_dir.is_dir():
                continue
            try:
                mtime = run_dir.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            staging_run = run_dir.name
            try:
                files = list(run_dir.iterdir())
            except OSError:
                files = []
            for item in files:
                try:
                    events.append((item.stat().st_mtime, f"staging {item.name}"))
                except OSError:
                    continue

    captures = root / "captures"
    if captures.is_dir():
        suffix = f"-{date}" if date else ""
        for cap_dir in captures.iterdir():
            if not cap_dir.is_dir():
                continue
            if suffix and not cap_dir.name.endswith(suffix):
                continue
            try:
                if cap_dir.stat().st_mtime < cutoff:
                    continue
                for item in cap_dir.iterdir():
                    try:
                        events.append((item.stat().st_mtime, f"capture {cap_dir.name}/{item.name}"))
                    except OSError:
                        continue
            except OSError:
                continue

    runs = root / "runs"
    if runs.is_dir():
        for run_dir in runs.iterdir():
            if not run_dir.is_dir():
                continue
            try:
                if run_dir.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            markdown = run_dir / "normalized-markdown"
            if not markdown.is_dir():
                continue
            try:
                for item in markdown.glob("*.md"):
                    accepted += 1
                    try:
                        events.append((item.stat().st_mtime, f"accepted {item.stem}"))
                    except OSError:
                        continue
            except OSError:
                continue

    events.sort()
    last = events[-1][1] if events else ""
    summary = f"{accepted} accepted source(s)"
    if last:
        summary += f"  ·  {last}"
    if staging_run:
        summary += f"  ·  run {staging_run[:18]}"
    return {
        "accepted": accepted,
        "events": len(events),
        "last": last,
        "staging_run": staging_run,
        "lines": [text for _mtime, text in events[-12:]],
        "summary": summary or "waiting for first source",
    }


def render_progress(session: dict[str, Any]) -> str:
    state = str(session.get("state") or "capturing").upper()
    elapsed = int(session.get("elapsed_s") or 0)
    minutes, seconds = divmod(max(elapsed, 0), 60)
    lines = [
        f"  Status: {state}  ·  {minutes}m {seconds:02d}s",
        f"  Session: {session.get('title') or session.get('id')}",
        f"  Activity: {session.get('summary') or 'starting'}",
        "",
    ]
    if state == "CAPTURING":
        lines.append("  Live workspace activity (leave this screen — the session stays on Home):")
    else:
        lines.append("  Session log")
    recent = session.get("lines") if isinstance(session.get("lines"), list) else []
    if recent:
        lines.extend(f"    {item}" for item in recent[-12:] if isinstance(item, str))
    else:
        lines.append("    waiting for the first Firecrawl response…")
    if session.get("error"):
        lines.extend(["", f"  {session['error']}"])
    return "\n".join(lines) + "\n"


class IntakeSupervisor:
    """One live capture owned by the application, not the Daily screen."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active: dict[str, Any] | None = None
        self.result: Any = None
        self._started = 0.0
        self._thread: threading.Thread | None = None

    @property
    def capturing(self) -> bool:
        with self._lock:
            return bool(self.active and self.active.get("state") == "capturing")

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            if self.active is None:
                return None
            return dict(self.active)

    def is_capturing_session(self, session_id: str) -> bool:
        """True when `session_id` is the in-memory session still capturing.

        Checked under the lock at call time — never from a stale snapshot —
        so a capture that started after a dialog opened is still protected.
        """
        with self._lock:
            session = self.active
            return bool(
                session
                and session.get("id") == session_id
                and session.get("state") == "capturing"
            )

    def forget(self, session_id: str) -> bool:
        """Drop the in-memory session/result — only under the lock, only if
        it is still `session_id` and no longer capturing. A live capture
        (or a newer session started meanwhile) is never clobbered."""
        with self._lock:
            session = self.active
            if session is None or session.get("id") != session_id:
                return False
            if session.get("state") == "capturing":
                return False
            self.active = None
            self.result = None
            return True

    def start(self, app: Any, config: Any, date: str | None = None) -> dict[str, Any]:
        # `report_session_date()` may be None (no offline calendar): the
        # product then certifies the session date itself.
        report_date = date or report_session_date()
        with self._lock:
            if self.active is not None and self.active.get("state") == "capturing":
                return dict(self.active)
            session = empty_session(config.workspace_root, report_date)
            self.active = session
            self.result = None
            self._started = time.time()
            save_session(session)
        worker = threading.Thread(
            target=self._run,
            args=(app, config, session["id"], report_date),
            daemon=True,
        )
        self._thread = worker
        worker.start()
        return session

    def _run(self, app: Any, config: Any, session_id: str, date: str | None) -> None:
        from .intake import IntakeDriver

        argv = tuple(config.intake_argv) if config else ()
        try:
            driver = IntakeDriver(
                (*argv, "--report-builder", config.get("intake_report_builder", "deepseek")),
                config.workspace_root,
                timeout_seconds=config.intake_timeout_seconds,
            )
        except ValueError as exc:
            self._finish(None, error=str(exc))
            self._notify_app(app)
            return

        stop = threading.Event()

        def _watch() -> None:
            while not stop.wait(1.0):
                self._refresh_progress(config.workspace_root)

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
        try:
            result = driver.run(date)
        except Exception as exc:
            result = None
            self._finish(None, error=str(exc))
        else:
            self._finish(result)
        finally:
            stop.set()
        self._notify_app(app)

    def _refresh_progress(self, workspace_root: str) -> None:
        with self._lock:
            session = self.active
            if session is None or session.get("state") != "capturing":
                return
            observed = observe_workspace(workspace_root, self._started, str(session.get("date") or ""))
            session["elapsed_s"] = int(time.time() - self._started)
            session["summary"] = observed["summary"]
            session["lines"] = observed["lines"]
            save_session(session)

    def _finish(self, result: Any, error: str | None = None) -> None:
        with self._lock:
            session = self.active
            if session is None:
                return
            self.result = result
            session["elapsed_s"] = int(time.time() - self._started)
            session["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if result is not None and getattr(result, "ok", False):
                session["state"] = "accepted"
                envelope = result.envelope or {}
                day = str(envelope.get("date") or session.get("date") or "")
                if day:
                    session["date"] = day
                    session["title"] = session_title(
                        day, str(session.get("started_at") or "")
                    )
                counts = result.counts or {}
                session["summary"] = (
                    f"accepted  {counts.get('succeeded', '?')}/"
                    f"{counts.get('expected', '?')} sources"
                )
                session["error"] = None
            else:
                session["state"] = "rejected"
                message = error
                if result is not None:
                    message = getattr(result, "error", None) or message
                session["error"] = message or "daily intake failed"
                session["summary"] = session["error"]
            save_session(session)

    def _notify_app(self, app: Any) -> None:
        def _deliver() -> None:
            result = self.result
            if result is not None and getattr(result, "ok", False):
                app.last_daily_intake_result = result
                try:
                    from .screens import _history_store

                    _history_store(app.config)
                except Exception:
                    pass
            screen = getattr(app, "screen", None)
            if screen is not None and hasattr(screen, "on_intake_job_finished"):
                screen.on_intake_job_finished(result)

        try:
            app.call_from_thread(_deliver)
        except Exception:
            pass


def supervisor_for(app: Any) -> IntakeSupervisor:
    job = getattr(app, "intake_supervisor", None)
    if not isinstance(job, IntakeSupervisor):
        job = IntakeSupervisor()
        app.intake_supervisor = job
    return job
