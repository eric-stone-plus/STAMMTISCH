"""THE live integration: a real state root whose events.jsonl GROWS between
frames; the session provider must flip run state in the NEXT frame while
re-reading ZERO already-consumed bytes (locked via byte/call accounting).

This is the M2 acceptance gate from the blueprint: registry liveness via
JSONL appends, no keypresses, no core spawn — the events plane IS the
registry's live data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import interface.collectors.feeds as feeds_mod
from interface.collectors import build_provider, session_for
from interface.collectors.events import TAIL_MAX, EventsCollector

SCHEMA = "stammtisch.run-event.v0"
DIGEST = "sha256:" + "ab12cd34" * 8


@pytest.fixture(autouse=True)
def _offline_feeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this file hermetic: the M4 quotes lane never hits the
    network here (its provider has its own stub-fetch tests)."""
    monkeypatch.setattr(feeds_mod, "_real_fetch", lambda symbols: {})


def _line(run_id: str, seq: int, kind: str, *,
          at: str = "2026-09-22T11:00:00Z", stage: str | None = None,
          payload: dict | None = None) -> str:
    event: dict = {"schema": SCHEMA, "run_id": run_id, "seq": seq,
                   "type": kind, "at": at, "payload": payload or {}}
    if stage is not None:
        event["stage"] = stage
    return json.dumps(event, separators=(",", ":"))


def _created(run_id: str) -> str:
    return _line(run_id, 1, "run.created", payload={
        "pipeline": {"id": "example.momentum"},
        "stages": [{"id": "ingest", "product": "highball"}],
    })


def _append(log: Path, *lines: str) -> int:
    """Append exactly these lines; returns the byte count added."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
    return sum(len(line.encode()) + 1 for line in lines)


def _live_run(frame):
    assert len(frame.runs) == 1, [r.id for r in frame.runs]
    return frame.runs[0]


def test_run_state_flips_next_frame_with_zero_rescan(tmp_path: Path) -> None:
    log = tmp_path / "runs" / "run-live" / "events.jsonl"
    _append(log, _created("run-live"))

    provider = build_provider(tmp_path, demo=False)
    frame1 = provider()
    assert _live_run(frame1).state == "created"
    assert frame1.collector_error is None
    # The feeds lane is stubbed offline in this file; an empty fetch
    # renders no quote rows — never fabricated ones (M4 quotes plane).
    assert frame1.quotes == ()

    # The provider is session-backed: cursors survive ticks.
    session = session_for(tmp_path)
    assert session.events.cursor("run-live") == log.stat().st_size
    bytes_before = session.events.bytes_read
    reads_before = session.events.chunk_reads

    # The writer appends two transitions between frames.
    staged = _line("run-live", 2, "run.staged")
    started = _line("run-live", 3, "stage.started", stage="ingest")
    added = _append(log, staged, started)

    frame2 = provider()
    run2 = _live_run(frame2)
    assert run2.state == "running"
    assert run2.stages[0].state == "running"
    assert run2.events_tail[-1].kind == "stage.started"
    assert run2.events_head_seq == 3

    # ZERO re-scan: exactly the appended bytes were pulled, one chunk read.
    assert session.events.bytes_read - bytes_before == added
    assert session.events.chunk_reads == reads_before + 1
    assert session.events.cursor("run-live") == log.stat().st_size

    # Terminal flip in the NEXT frame, again with only the new bytes.
    bytes_before = session.events.bytes_read
    completed = _append(
        log,
        _line("run-live", 4, "stage.gate_passed", stage="ingest",
              payload={"gate_id": "data.floor", "record_sha256": DIGEST}),
        _line("run-live", 5, "run.gating"),
        _line("run-live", 6, "run.completed",
              payload={"bundle_manifest_sha256": DIGEST}),
    )
    frame3 = provider()
    run3 = _live_run(frame3)
    assert run3.state == "completed"
    assert run3.gates[0].decision == "PASS"
    assert session.events.bytes_read - bytes_before == completed

    # A frame with nothing appended costs a read of ZERO new bytes.
    bytes_before = session.events.bytes_read
    provider()
    assert session.events.bytes_read == bytes_before


def test_new_runs_and_intake_appear_between_frames(tmp_path: Path) -> None:
    _append(tmp_path / "runs" / "run-first" / "events.jsonl",
            _created("run-first"))
    provider = build_provider(tmp_path, demo=False)
    frame1 = provider()
    assert [run.id for run in frame1.runs] == ["run-first"]

    # A second run directory lands between frames (the core wrote it).
    newer = _line("run-second", 1, "run.created",
                  at="2026-09-22T12:00:00Z", payload={
                      "pipeline": {"id": "example.meanrev"},
                      "stages": [{"id": "ingest", "product": "highball"}],
                  })
    _append(tmp_path / "runs" / "run-second" / "events.jsonl", newer)
    # And an intake session file appears on the file plane.
    folder = tmp_path / "intake-sessions"
    folder.mkdir()
    (folder / "sess-live.json").write_text(json.dumps({
        "id": "sess-live", "state": "capturing",
        "updated_at": "2026-09-22T12:00:30Z",
        "date": "20260922",
    }), encoding="utf-8")

    frame2 = provider()
    # Live states first, then newest created_at; second run is newer.
    assert [run.id for run in frame2.runs] == ["run-second", "run-first"]
    # On-disk capturing degrades to interrupted (no live supervisor here).
    assert [(row.id, row.state) for row in frame2.intake] == [
        ("sess-live", "interrupted")]
    assert frame2.collector_error is None
    assert {service.name for service in frame2.services} >= {
        "core", "quantkit", "ai"}


def test_provider_is_shared_per_root(tmp_path: Path) -> None:
    _append(tmp_path / "runs" / "run-shared" / "events.jsonl",
            _created("run-shared"))
    first = build_provider(tmp_path, demo=False)
    second = build_provider(tmp_path, demo=False)
    first()
    # Same session, same cursors: the second provider reuses them rather
    # than re-scanning the log from byte zero.
    session = session_for(tmp_path)
    assert session.events.chunk_reads == 1
    second()
    assert session.events.chunk_reads == 2  # one new (empty) chunk only


def test_uninitialized_root_is_a_loud_empty_frame(tmp_path: Path) -> None:
    provider = build_provider(tmp_path / "not-a-state-root", demo=False)
    frame = provider()
    assert frame.runs == ()
    assert frame.collector_error is not None
    assert "cannot scan" in frame.collector_error


def test_demo_provider_untouched_by_real_roots(tmp_path: Path) -> None:
    demo = build_provider(None, demo=True)
    frame = demo()
    assert frame.runs and frame.quotes  # demo still carries its synthetic
    _append(tmp_path / "runs" / "run-x" / "events.jsonl", _created("run-x"))
    real = build_provider(tmp_path, demo=False)
    assert real().runs[0].id == "run-x"
    assert demo().runs[0].id == "demo-run-live"  # unchanged by the real path


def test_tail_cap_holds_across_live_appends(tmp_path: Path) -> None:
    log = tmp_path / "runs" / "run-tail" / "events.jsonl"
    _append(log, _created("run-tail"))
    provider = build_provider(tmp_path, demo=False)
    seq = 1
    for _ in range(TAIL_MAX + 40):
        seq += 1
        _append(log, _line("run-tail", seq, "stage.receipt_accepted",
                           stage="ingest", payload={"digest": DIGEST}))
        provider()
    run = _live_run(provider())
    assert len(run.events_tail) == TAIL_MAX
    assert [event.seq for event in run.events_tail] == list(
        range(seq - TAIL_MAX + 1, seq + 1))


def test_corrupt_append_degrades_run_not_the_session(tmp_path: Path) -> None:
    log = tmp_path / "runs" / "run-degrade" / "events.jsonl"
    _append(log, _created("run-degrade"))
    provider = build_provider(tmp_path, demo=False)
    assert _live_run(provider()).state == "created"
    with log.open("a", encoding="utf-8") as handle:
        handle.write("corrupt line\n")
    frame = provider()
    run = _live_run(frame)
    assert run.state == "corrupt"
    assert run.error and "unparseable" in run.error
    assert frame.collector_error is None  # a corrupt run is a row, not a crash
    # The rest of the station keeps working:
    _append(tmp_path / "runs" / "run-healthy" / "events.jsonl",
            _created("run-healthy"))
    frame = provider()
    assert {run.id for run in frame.runs} == {"run-degrade", "run-healthy"}


def test_events_collector_reads_are_byte_exactly_incremental(
        tmp_path: Path) -> None:
    # Direct collector-level lock of the same property (no provider).
    log = tmp_path / "runs" / "run-exact" / "events.jsonl"
    _append(log, _created("run-exact"))
    collector = EventsCollector(tmp_path)
    collector.collect()
    # One more append, one more read, only the appended bytes:
    added = _append(log, _line("run-exact", 2, "run.staged"))
    before = collector.bytes_read
    collector.collect()
    assert collector.bytes_read - before == added
