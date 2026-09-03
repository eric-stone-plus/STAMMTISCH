"""GALAHAD intake steward: resident auto-capture decisions.

While the TUI is open the steward ticks every few minutes and decides
whether a daily-data capture is due. Deterministic gates run first
(fail-closed, no model): a capture already running, no intake command,
or an unreachable Firecrawl endpoint all mean "not now" without a
model call. Only when the gates pass does GALAHAD — the workstation's
LLM driver — judge from a compact digest whether to capture now, and
say why. The date itself stays certified by the intake product; the
steward never guesses trading days.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .intake_job import supervisor_for

TICK_SECONDS = 300.0
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now_shanghai() -> str:
    return datetime.now(_SHANGHAI).strftime("%Y-%m-%d %H:%M (%a) Asia/Shanghai")


def _firecrawl_reachable(url: str | None, timeout: float = 4.0) -> bool:
    """Cheap liveness probe of the local capture endpoint, if configured."""
    if not url:
        # Not configured here: assume reachable — the product owns the
        # contract and will fail visibly on its own.
        return True
    import urllib.request

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # the server answered; auth/404 is not an outage
    except Exception:
        return False


def _latest_report_dates(config: Any) -> dict[str, str]:
    """Newest accepted report date from the history index."""
    try:
        from .screens import _history_store

        store, _error = _history_store(config)
        entry = store.latest() if store is not None else None
        if entry is not None and entry.report_date:
            return {"latest": str(entry.report_date)}
    except Exception:
        pass
    return {}


class IntakeSteward:
    """Resident capture decider owned by the GALAHAD driver."""

    def __init__(self, tick_seconds: float = TICK_SECONDS) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_decision: dict[str, Any] = {"at": None, "capture": False, "reason": "not started"}
        self._last_capture_started_at: float = 0.0
        self._tick_seconds = tick_seconds

    # -- lifecycle ---------------------------------------------------------

    def start(self, app: Any) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, args=(app,), daemon=True, name="intake-steward"
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, app: Any) -> None:
        # First check shortly after open: the "open STAMMTISCH and it just
        # works" moment, without racing dashboard mount.
        if self._stop.wait(8.0):
            return
        while not self._stop.wait(self._tick_seconds):
            try:
                self.tick(app)
            except Exception as exc:  # a resident loop must never die
                self._record({"capture": False, "reason": f"steward error: {exc}"})

    # -- decision ----------------------------------------------------------

    def digest(self, app: Any) -> dict[str, Any]:
        config = getattr(app, "config", None)
        supervisor = supervisor_for(app)
        workspace = str(getattr(config, "workspace_root", "") or "")
        sessions = []
        try:
            from .intake_job import list_sessions

            sessions = [
                {"state": s.get("state"), "updated_at": s.get("updated_at")}
                for s in list_sessions(workspace)[:5]
            ]
        except Exception:
            pass
        snapshot = supervisor.snapshot()
        reports = _latest_report_dates(config)
        return {
            "now_shanghai": _now_shanghai(),
            "capture_running": supervisor.capturing,
            "last_capture_state": snapshot.get("state") if snapshot else None,
            "sessions_on_disk": sessions,
            "latest_report_date": reports.get("latest", ""),
            "seconds_since_last_capture": int(time.time() - self._last_capture_started_at)
            if self._last_capture_started_at
            else None,
        }

    def deterministic_gate(self, app: Any) -> str | None:
        """Return a blocking reason, or None if a model judgment is due."""
        config = getattr(app, "config", None)
        if not config:
            return "no config"
        argv = getattr(config, "intake_argv", None)
        if not argv:
            return "intake command not configured"
        if supervisor_for(app).capturing:
            return "capture already running"
        firecrawl = None
        for index, item in enumerate(argv):
            if str(item) == "--firecrawl-url" and index + 1 < len(argv):
                firecrawl = str(argv[index + 1])
        if firecrawl and not _firecrawl_reachable(firecrawl):
            return "capture endpoint unreachable"
        if self._last_capture_started_at and time.time() - self._last_capture_started_at < 1800:
            return "captured within the last 30 minutes"
        return None

    def ask_galahad(self, app: Any, digest: dict[str, Any]) -> dict[str, Any]:
        """One structured judgment call through the GALAHAD driver."""
        ai = getattr(app, "ai", None)
        if ai is None or not getattr(ai, "available", False):
            # No brain available: fall back to a conservative rule —
            # capture when the day has no accepted report yet.
            latest = str(digest.get("latest_report_date") or "")
            today = _now_shanghai()[:10].replace("-", "")
            return {
                "capture": bool(latest and latest < today),
                "reason": "model unavailable; captured only if today's report is missing",
            }
        prompt = (
            "You schedule the workstation's daily market-data capture. "
            "Decide whether to START a capture NOW. Facts:\n"
            + json.dumps(digest, ensure_ascii=False)
            + '\nReply ONLY JSON: {"capture": true|false, "reason": "short Chinese reason", '
            '"next_check_minutes": int}. A capture is worth starting when the data for the '
            "most recent completed trading session is missing or stale; not when one is "
            "running, was just done, or the session is not finished yet. Be stingy."
        )
        try:
            from .ai_driver import AIDriver

            judge = AIDriver(
                api_key=ai.api_key, base_url=ai.base_url, model=ai.model
            )
            response = judge.chat(prompt)
        except Exception as exc:
            return {"capture": False, "reason": f"judge call failed: {exc}"}
        text = (response.content or "").strip()
        try:
            start = text.find("{")
            end = text.rfind("}")
            decision = json.loads(text[start : end + 1])
        except Exception:
            return {"capture": False, "reason": f"unparsable judgment: {text[:80]}"}
        return {
            "capture": bool(decision.get("capture")),
            "reason": str(decision.get("reason") or ""),
            "next_check_minutes": decision.get("next_check_minutes"),
        }

    def tick(self, app: Any) -> dict[str, Any]:
        blocking = self.deterministic_gate(app)
        if blocking is not None:
            return self._record({"capture": False, "reason": blocking})
        digest = self.digest(app)
        decision = self.ask_galahad(app, digest)
        if decision.get("capture"):
            self.start_capture(app)
        return self._record(decision)

    def start_capture(self, app: Any) -> bool:
        from .screens import DailyIntakeScreen  # late import: screens imports steward-free

        config = app.config
        supervisor = supervisor_for(app)
        if supervisor.capturing:
            return False
        screen = getattr(app, "screen", None)
        # Capture without leaving the current screen: the supervisor owns
        # the job and the Home row shows progress.
        supervisor.start(app, config, date=None)
        self._last_capture_started_at = time.time()
        if isinstance(screen, DailyIntakeScreen):
            screen.action_capture()
        return True

    def decision(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_decision)

    def _record(self, decision: dict[str, Any]) -> dict[str, Any]:
        decision = dict(decision)
        decision["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            self._last_decision = decision
        return decision


def steward_for(app: Any) -> IntakeSteward:
    steward = getattr(app, "intake_steward", None)
    if not isinstance(steward, IntakeSteward):
        steward = IntakeSteward()
        app.intake_steward = steward
    return steward
