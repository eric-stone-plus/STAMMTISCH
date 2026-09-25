"""FilesCollector tests: intake listing semantics, supervisor override,
service-plane presence booleans (shape checks only, never key values)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import interface.collectors.files as files_mod
from interface.collectors.files import FilesCollector

SCHEMA = "stammtisch.intake-session.v1"


def _session_file(root: Path, sid: str, state: str, updated_at: str,
                  started_at: str = "2026-09-22T08:00:00Z",
                  date: str = "20260922") -> None:
    folder = root / "intake-sessions"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sid}.json").write_text(json.dumps({
        "schema": SCHEMA, "id": sid, "title": f"daily-intake {sid}",
        "date": date, "state": state,
        "started_at": started_at, "updated_at": updated_at,
        "finished_at": None, "elapsed_s": 0, "summary": "fixture",
        "lines": [], "error": None, "workspace_root": str(root),
    }), encoding="utf-8")


def _collect(root: Path):
    return FilesCollector(root).collect()


# ── intake semantics (mirrors the old dashboard rows) ────────────────


def test_disk_capturing_becomes_interrupted(tmp_path: Path) -> None:
    _session_file(tmp_path, "sess-early", "capturing", "2026-09-22T08:01:00Z")
    _session_file(tmp_path, "sess-ok", "accepted", "2026-09-22T09:00:00Z")
    _session_file(tmp_path, "sess-bad", "rejected", "2026-09-22T07:00:00Z")
    intake, _services, error = _collect(tmp_path)
    assert error is None
    states = {row.id: row.state for row in intake}
    # Only the live supervisor session can still be capturing; a capturing
    # row on disk belongs to a host that died mid-capture.
    assert states == {"sess-ok": "accepted", "sess-early": "interrupted",
                      "sess-bad": "rejected"}


def test_rows_sorted_by_updated_at_desc_with_report_date(
        tmp_path: Path) -> None:
    _session_file(tmp_path, "sess-old", "accepted", "2026-09-20T08:00:00Z")
    _session_file(tmp_path, "sess-new", "accepted", "2026-09-22T08:00:00Z")
    intake, _, _ = _collect(tmp_path)
    assert [row.id for row in intake] == ["sess-new", "sess-old"]
    assert intake[0].updated_at == "2026-09-22T08:00:00Z"
    assert intake[0].report_date == "20260922"


def test_live_supervisor_row_overrides_disk(tmp_path: Path) -> None:
    _session_file(tmp_path, "sess-live", "capturing", "2026-09-22T10:00:00Z")
    _session_file(tmp_path, "sess-done", "accepted", "2026-09-22T09:00:00Z")
    collector = FilesCollector(tmp_path, live_intake=lambda: {
        "id": "sess-live", "state": "capturing",
        "updated_at": "2026-09-22T10:00:05Z", "started_at": "2026-09-22T10:00:00Z",
        "date": "20260922",
    })
    intake, _, error = collector.collect()
    assert error is None
    ids = [row.id for row in intake]
    assert len(ids) == len(set(ids)), "live row must not be duplicated"
    assert ids[0] == "sess-live"  # live row leads
    assert intake[0].state == "capturing"
    assert intake[0].updated_at == "2026-09-22T10:00:05Z"
    assert {row.id: row.state for row in intake[1:]} == {"sess-done": "accepted"}


def test_supervisor_hook_is_settable_and_degrades(tmp_path: Path) -> None:
    _session_file(tmp_path, "sess-disk", "accepted", "2026-09-22T08:00:00Z")
    collector = FilesCollector(tmp_path)  # default: no hook
    assert [row.id for row in collector.collect()[0]] == ["sess-disk"]

    def broken():  # a raising hook degrades to "no live session"
        raise RuntimeError("supervisor gone")

    collector.live_intake = broken
    assert [row.id for row in collector.collect()[0]] == ["sess-disk"]

    collector.live_intake = lambda: {"state": "capturing"}  # no id: ignored
    assert [row.id for row in collector.collect()[0]] == ["sess-disk"]


def test_malformed_and_foreign_files_skipped(tmp_path: Path) -> None:
    folder = tmp_path / "intake-sessions"
    folder.mkdir(parents=True)
    (folder / "broken.json").write_text("{not json", encoding="utf-8")
    (folder / "noid.json").write_text(json.dumps({"state": "accepted"}),
                                      encoding="utf-8")
    (folder / "notes.txt").write_text("not a session", encoding="utf-8")
    _session_file(tmp_path, "sess-fine", "accepted", "2026-09-22T08:00:00Z")
    intake, _, error = _collect(tmp_path)
    assert error is None
    assert [row.id for row in intake] == ["sess-fine"]


def test_missing_intake_dir_is_empty_not_an_error(tmp_path: Path) -> None:
    intake, _services, error = _collect(tmp_path)
    assert intake == ()
    assert error is None


def test_session_without_state_shows_unknown(tmp_path: Path) -> None:
    folder = tmp_path / "intake-sessions"
    folder.mkdir(parents=True)
    (folder / "sess-bare.json").write_text(json.dumps({
        "id": "sess-bare", "updated_at": "2026-09-22T08:00:00Z",
    }), encoding="utf-8")
    intake, _, _ = _collect(tmp_path)
    assert intake[0].state == "unknown"


# ── service-plane booleans ───────────────────────────────────────────


@pytest.mark.skipif(
    importlib.util.find_spec("quantkit") is not None,
    reason="this pin asserts the quantkit-ABSENT env shape (ephemeral CI); "
           "hosts with quantkit installed are covered by the monkeypatched "
           "test_quantkit_reflected_when_importable below — an ungated "
           "absent-shape assert would also silently block the "
           "--with-quantkit preflight mode (docs/ci-python-blocking.md)",
)
def test_quantkit_absent_in_test_env_shape(tmp_path: Path) -> None:
    _intake, services, _ = _collect(tmp_path)
    quantkit = next(s for s in services if s.name == "quantkit")
    # The ephemeral test env does not install quantkit: the boolean must
    # reflect that shape, not assume availability.
    assert quantkit.available is False
    assert quantkit.detail == "not installed"


def test_quantkit_reflected_when_importable(tmp_path: Path,
                                            monkeypatch) -> None:
    monkeypatch.setattr(files_mod, "_probe_quantkit", lambda: True)
    _intake, services, _ = _collect(tmp_path)
    quantkit = next(s for s in services if s.name == "quantkit")
    assert quantkit.available is True
    assert quantkit.detail == ""


def test_ai_presence_is_env_name_shape_only(tmp_path, monkeypatch) -> None:
    for name in files_mod._AI_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    _intake, services, _ = _collect(tmp_path)
    ai = next(s for s in services if s.name == "ai")
    assert ai.available is False
    assert "no API key" in ai.detail

    monkeypatch.setenv("GLM_API_KEY", "fixture-not-a-real-key")
    _intake, services, _ = _collect(tmp_path)
    ai = next(s for s in services if s.name == "ai")
    assert ai.available is True
    # The value NEVER leaves the env: no key material in the row.
    assert "fixture-not-a-real-key" not in ai.detail


def test_services_tuple_order(tmp_path: Path) -> None:
    _intake, services, _ = _collect(tmp_path)
    assert [s.name for s in services] == ["quantkit", "ai"]


def test_intake_plane_unreadable_is_loud(tmp_path: Path) -> None:
    """Review B standards finding: the error channel used to be hardcoded
    None — an unreadable intake plane failed silently while events.py
    degrades loudly on the identical condition."""
    (tmp_path / "intake-sessions").write_text("not a directory",
                                              encoding="utf-8")
    rows, _services, error = FilesCollector(tmp_path).collect()
    assert rows == ()
    assert error is not None and "not a directory" in error
