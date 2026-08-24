"""Session-record helpers: ask sessions, run titles, atomic JSON io."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

def format_run_session_summary(run_id: str, snapshot: dict[str, Any], tz_name: str = "") -> str:
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
    }, tz_name)
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
        lines.append(f"  Last     {last_type}  {_created_stamp(last_at, tz_name) or last_at}")
    return "\n".join(lines) + "\n"
_ASK_INTRO = (
    "  Ready. Ask about pipelines, gates, backtests, strategy analysis.\n\n"
)
_INPUT_HISTORY_CAP = 200
_SESSION_KEEP = 80
def run_session_parts(run: dict[str, Any], tz_name: str = "") -> tuple[str, str]:
    """Name and aligned timestamp for a registry row."""
    explicit = run.get("title")
    if isinstance(explicit, str) and explicit.strip():
        name = " ".join(explicit.split())
    else:
        name = str(run.get("pipeline_id") or "").strip() or "run"
    stamp = _created_stamp(run.get("created_at"), tz_name)
    if not stamp:
        rid = str(run.get("run_id") or "")
        stamp = rid[:8]
    return name, stamp
def run_session_title(run: dict[str, Any], tz_name: str = "") -> str:
    """Single-line title kept for tests and inspector headers."""
    name, stamp = run_session_parts(run, tz_name)
    return f"{name}  {stamp}".rstrip() if stamp else name
def _intake_session_parts(session: dict[str, Any], tz_name: str = "") -> tuple[str, str]:
    title = str(session.get("title") or "daily-intake")
    name = title.split(" · ", 1)[0].strip() or "daily-intake"
    when = _created_stamp(session.get("started_at") or session.get("updated_at"), tz_name)
    day = str(session.get("date") or "")
    if not when and len(day) == 8:
        when = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    return name, when
def _created_stamp(created: Any, tz_name: str = "") -> str:
    text = str(created or "").strip()
    if not text:
        return ""
    if "T" in text:
        day, rest = text.split("T", 1)
        if tz_name:
            shifted = _shift_stamp(day, rest, tz_name)
            if shifted is not None:
                return shifted
        hhmm = rest[:5]
        return f"{day} {hhmm}" if len(hhmm) == 5 else day
    return text[:16]
def _shift_stamp(day: str, rest: str, tz_name: str) -> str | None:
    """Re-render an ISO day+time pair in a display timezone.

    Returns None when the timestamp does not parse or the zone name is
    unknown — callers then keep the stored (UTC) rendering. Timestamps
    written by this workstation are UTC, so naive input is assumed UTC.
    """
    try:
        moment = datetime.fromisoformat(f"{day}T{rest}")
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")
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
