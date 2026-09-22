"""FilesCollector — intake sessions on disk plus optional-plane presence.

Two jobs, neither of which spawns anything:

- Intake session rows, mirroring the old semantics exactly: only the
  live supervisor session may show ``capturing`` — a ``capturing`` row
  found on disk belongs to a host that died mid-capture and is shown as
  ``interrupted``. The supervisor is injected as a settable callable
  (default: none) returning the live session mapping, or ``None``.
- Optional service-plane presence booleans: quantkit via import
  availability, ai via API-key ENV-NAME presence only. Key values are
  never read — a boolean is all this plane ever sees.

Intake sessions are read from ``<root>/intake-sessions/*.json`` (the
``stammtisch.intake-session.v1`` files the old supervisor wrote); rows
are honest projections of whatever parses, newest ``updated_at`` first.
"""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from interface.snapshot import IntakeSessionSnapshot, ServiceStatus

#: Supervisor hook shape: () -> live session mapping (or None). Settable at
#: any time; a raising hook degrades to "no live session", never raises.
LiveIntakeHook = Callable[[], Mapping[str, Any] | None]

#: AI provider env var NAMES only (presence shape check; values never read).
_AI_ENV_KEYS: tuple[str, ...] = (
    "GLM_API_KEY", "ZHIPU_API_KEY", "XIAOMI_API_KEY",
    "DEEPSEEK_API_KEY", "DEEPSEEK_KEY", "DEEPSEEK_TOKEN",
    "QIANWEN_TP_PERSONAL_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
)


def _probe_quantkit() -> bool:
    """True when the quantkit package is importable in this interpreter."""
    try:
        return importlib.util.find_spec("quantkit") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def _quantkit_status() -> ServiceStatus:
    available = _probe_quantkit()
    return ServiceStatus("quantkit", available,
                         "" if available else "not installed")


def _ai_status() -> ServiceStatus:
    found = [name for name in _AI_ENV_KEYS if name in os.environ]
    return ServiceStatus("ai", bool(found),
                         "" if found else "no API key in env")


class FilesCollector:
    """Read-only file-plane collector: intake rows + service booleans."""

    def __init__(self, root: Path,
                 live_intake: LiveIntakeHook | None = None) -> None:
        self.root = Path(root)
        self.live_intake = live_intake

    def collect(self) -> tuple[tuple[IntakeSessionSnapshot, ...],
                              tuple[ServiceStatus, ...],
                              str | None]:
        """One pass: (intake rows, optional services, error or None).

        An intake plane that exists but cannot be read is LOUD (review B:
        the hardcoded None here used to fail silently while events.py
        degraded loudly on the identical condition). A plane that simply
        does not exist yet is not an error.
        """
        rows, error = self._intake_rows()
        return rows, (_quantkit_status(), _ai_status()), error

    # ── intake sessions ────────────────────────────────────────────

    def _intake_rows(self) -> tuple[IntakeSessionSnapshot, ...]:
        rows: list[IntakeSessionSnapshot] = []
        seen: set[str] = set()
        live = self._live_row()
        if live is not None:
            seen.add(live.id)
            rows.append(live)
        disk: list[tuple[str, IntakeSessionSnapshot]] = []
        folder = self.root / "intake-sessions"
        if folder.exists() and not folder.is_dir():
            return (), f"intake plane unreadable: {folder} is not a directory"
        try:
            paths = sorted(folder.glob("*.json"))
        except OSError as exc:  # pragma: no cover - glob is tolerant
            return (), f"intake plane unreadable: {exc}"
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            sid = str(data.get("id") or "")
            if not sid or sid in seen:
                continue
            state = str(data.get("state") or "unknown")
            if state == "capturing":
                # Only the live supervisor session can still be capturing;
                # a capturing row on disk belongs to an app that died
                # mid-capture — interrupted, never a zombie capture.
                state = "interrupted"
            disk.append((self._stamp(data), IntakeSessionSnapshot(
                id=sid, state=state,
                updated_at=self._stamp(data),
                report_date=str(data.get("date") or ""),
            )))
        disk.sort(key=lambda item: item[0], reverse=True)
        return tuple(rows + [row for _stamp, row in disk]), None

    def _live_row(self) -> IntakeSessionSnapshot | None:
        hook = self.live_intake
        if hook is None:
            return None
        try:
            live = hook()
        except Exception:  # noqa: BLE001 - any broken hook degrades, never raises
            return None
        if not isinstance(live, Mapping):
            return None
        sid = str(live.get("id") or "")
        if not sid:
            return None
        return IntakeSessionSnapshot(
            id=sid,
            state=str(live.get("state") or "capturing"),
            updated_at=self._stamp(live),
            report_date=str(live.get("date") or ""),
        )

    @staticmethod
    def _stamp(session: Mapping[str, Any]) -> str:
        value = session.get("updated_at") or session.get("started_at")
        return value if isinstance(value, str) else ""
