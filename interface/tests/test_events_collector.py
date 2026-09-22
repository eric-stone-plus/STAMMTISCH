"""EventsCollector tests: lifecycle fold, cursor discipline, corruption, cost.

Synthetic state roots under tmp_path carry hand-written events.jsonl lines
that follow schemas/run-event.schema.json (example ids only; digests are
placeholder hex). Nothing here touches a real state root.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from interface.collectors.events import TAIL_MAX, EventsCollector
from interface.collectors.session import sort_runs
from interface.snapshot import RunSnapshot

SCHEMA = "stammtisch.run-event.v0"
DIGEST = "sha256:" + "ab12cd34" * 8  # 64 lowercase hex chars


def _line(run_id: str, seq: int, kind: str, *,
          at: str = "2026-09-22T10:00:00Z", stage: str | None = None,
          payload: dict | None = None) -> str:
    event: dict = {"schema": SCHEMA, "run_id": run_id, "seq": seq,
                   "type": kind, "at": at, "payload": payload or {}}
    if stage is not None:
        event["stage"] = stage
    return json.dumps(event, separators=(",", ":"))


def _created(run_id: str, pipeline: str = "example.momentum", *,
             at: str = "2026-09-22T10:00:00Z") -> str:
    return _line(run_id, 1, "run.created", at=at, payload={
        "pipeline": {"id": pipeline},
        "stages": [{"id": "ingest", "product": "highball"},
                   {"id": "review", "product": "doctrine"}],
    })


def _append(log: Path, *lines: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _run(root: Path, run_id: str) -> Path:
    return root / "runs" / run_id


# ── lifecycle fold ────────────────────────────────────────────────────


def test_full_lifecycle_folds_to_completed(tmp_path: Path) -> None:
    _append(_run(tmp_path, "run-alpha") / "events.jsonl",
            _created("run-alpha"),
            _line("run-alpha", 2, "run.staged"),
            _line("run-alpha", 3, "stage.started", stage="ingest"),
            _line("run-alpha", 4, "stage.receipt_accepted", stage="ingest",
                  payload={"digest": DIGEST}),
            _line("run-alpha", 5, "stage.artifact_recorded", stage="ingest",
                  payload={"name": "bars.json", "digest": DIGEST}),
            _line("run-alpha", 6, "stage.gate_passed", stage="ingest",
                  payload={"gate_id": "data.floor", "decision": "pass",
                           "record_sha256": DIGEST}),
            _line("run-alpha", 7, "stage.started", stage="review"),
            _line("run-alpha", 8, "stage.gate_passed", stage="review",
                  payload={"gate_id": "risk.cap", "record_sha256": DIGEST}),
            _line("run-alpha", 9, "run.gating"),
            _line("run-alpha", 10, "run.completed",
                  payload={"bundle_manifest_sha256": DIGEST}),
            )
    runs, error = EventsCollector(tmp_path).collect()
    assert error is None
    assert len(runs) == 1
    snap = runs[0]
    assert snap.id == "run-alpha"
    assert snap.state == "completed"
    assert snap.pipeline_id == "example.momentum"
    assert snap.created_at == "2026-09-22T10:00:00Z"
    assert [(s.id, s.product, s.state, s.gate) for s in snap.stages] == [
        ("ingest", "highball", "completed", "data.floor"),
        ("review", "doctrine", "completed", "risk.cap"),
    ]
    assert [(g.gate_id, g.stage, g.decision) for g in snap.gates] == [
        ("data.floor", "ingest", "PASS"),
        ("risk.cap", "review", "PASS"),
    ]
    assert all(g.record_sha256 == DIGEST for g in snap.gates)
    assert snap.events_head_seq == 10
    assert [e.seq for e in snap.events_tail] == list(range(1, 11))


def test_state_progresses_incrementally_across_collects(tmp_path: Path) -> None:
    log = _run(tmp_path, "run-beta") / "events.jsonl"
    _append(log, _created("run-beta"), _line("run-beta", 2, "run.staged"))
    collector = EventsCollector(tmp_path)
    assert collector.collect()[0][0].state == "staged"
    _append(log, _line("run-beta", 3, "stage.started", stage="ingest"))
    assert collector.collect()[0][0].state == "running"
    _append(log, _line("run-beta", 4, "run.gating"))
    assert collector.collect()[0][0].state == "gating"
    _append(log, _line("run-beta", 5, "run.completed"))
    assert collector.collect()[0][0].state == "completed"


def test_gate_failure_blocks_stage_then_run(tmp_path: Path) -> None:
    _append(_run(tmp_path, "run-gate") / "events.jsonl",
            _created("run-gate"),
            _line("run-gate", 2, "run.staged"),
            _line("run-gate", 3, "stage.started", stage="ingest"),
            _line("run-gate", 4, "stage.gate_failed", stage="ingest",
                  payload={"reason": "gate_failed", "gate_id": "data.floor",
                           "record_sha256": DIGEST}),
            _line("run-gate", 5, "run.blocked", payload={"summary": "floor"}),
            )
    runs, _ = EventsCollector(tmp_path).collect()
    snap = runs[0]
    assert snap.state == "blocked"
    assert snap.stages[0].state == "blocked"
    assert snap.stages[0].gate == "data.floor"
    assert snap.gates[0].decision == "FAIL"


def test_stage_failed_reason_maps_refused_to_blocked(tmp_path: Path) -> None:
    _append(_run(tmp_path, "run-sf") / "events.jsonl",
            _created("run-sf"),
            _line("run-sf", 2, "run.staged"),
            _line("run-sf", 3, "stage.started", stage="ingest"),
            _line("run-sf", 4, "stage.failed", stage="ingest",
                  payload={"reason": "product_refused"}),
            _line("run-sf", 5, "run.failed",
                  payload={"summary": "product refused"}),
            )
    runs, _ = EventsCollector(tmp_path).collect()
    assert runs[0].stages[0].state == "blocked"
    assert runs[0].state == "failed"


def test_terminal_event_types_project_their_states(tmp_path: Path) -> None:
    terminal = ["run.cancelled", "run.failed", "run.halted",
                "run.reconciled", "run.resumed"]
    for index, kind in enumerate(terminal):
        run_id = f"run-term-{index}"
        _append(_run(tmp_path, run_id) / "events.jsonl",
                _created(run_id), _line(run_id, 2, "run.staged"),
                _line(run_id, 3, kind),
                )
    runs, _ = EventsCollector(tmp_path).collect()
    by_state = {snap.state: snap.id for snap in runs}
    assert by_state == {
        "cancelled": "run-term-0", "failed": "run-term-1",
        "halted": "run-term-2", "reconciled": "run-term-3",
        "resumed": "run-term-4",
    }


# ── cursor discipline ────────────────────────────────────────────────


def test_cursor_tracks_bytes_and_only_new_lines_are_consumed(
        tmp_path: Path) -> None:
    log = _run(tmp_path, "run-cursor") / "events.jsonl"
    _append(log, _created("run-cursor"))
    collector = EventsCollector(tmp_path)
    collector.collect()
    first = collector.cursor("run-cursor")
    assert first == log.stat().st_size
    second_line = _line("run-cursor", 2, "run.staged")
    third_line = _line("run-cursor", 3, "stage.started", stage="ingest")
    _append(log, second_line, third_line)
    runs, _ = collector.collect()
    assert collector.cursor("run-cursor") == log.stat().st_size
    assert collector.bytes_read == (len(_created("run-cursor").encode()) + 1
                                    + len(second_line.encode()) + 1
                                    + len(third_line.encode()) + 1)
    assert collector.chunk_reads == 2
    assert runs[0].state == "running"


def test_truncated_last_line_is_reread_never_skipped(tmp_path: Path) -> None:
    log = _run(tmp_path, "run-torn") / "events.jsonl"
    _append(log, _created("run-torn"))
    collector = EventsCollector(tmp_path)
    collector.collect()
    after_first = collector.cursor("run-torn")
    torn = _line("run-torn", 2, "run.staged")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(torn)  # torn append: no newline yet
    runs, _ = collector.collect()
    assert collector.cursor("run-torn") == after_first  # not consumed
    assert runs[0].state == "created"  # the torn line is not trusted
    with log.open("a", encoding="utf-8") as handle:
        handle.write("\n")  # writer completes the line
    runs, _ = collector.collect()
    assert collector.cursor("run-torn") == after_first + len(torn.encode()) + 1
    assert runs[0].state == "staged"


# ── corruption is a field, never an exception ────────────────────────


def test_corrupt_line_marks_run_corrupt_stickily(tmp_path: Path) -> None:
    log = _run(tmp_path, "run-bad") / "events.jsonl"
    _append(log, _created("run-bad"))
    collector = EventsCollector(tmp_path)
    assert collector.collect()[0][0].state == "created"
    with log.open("a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
    runs, _ = collector.collect()
    snap = runs[0]
    assert snap.state == "corrupt"
    assert snap.error is not None and "unparseable" in snap.error
    assert collector.collect()[0][0].state == "corrupt"  # sticky


def test_schema_violations_are_corrupt(tmp_path: Path) -> None:
    cases = {
        "seq_gap": [_created("run-x"),
                    _line("run-x", 3, "run.staged")],
        "run_id_mismatch": [_created("run-x"),
                            _line("run-y", 2, "run.staged")],
        "unknown_type": [_created("run-x"),
                         _line("run-x", 2, "run.exploded")],
        "bad_schema": [_created("run-x"),
                       _line("run-x", 2, "run.staged").replace(SCHEMA,
                                                                "other.v9")],
        "unknown_stage": [_created("run-x"),
                          _line("run-x", 2, "stage.started", stage="ghost")],
        "gate_without_digest": [_created("run-x"),
                                _line("run-x", 2, "run.staged"),
                                _line("run-x", 3, "stage.started",
                                      stage="ingest"),
                                _line("run-x", 4, "stage.gate_passed",
                                      stage="ingest",
                                      payload={"gate_id": "data.floor"})],
        "created_without_stages": [
            _line("run-x", 1, "run.created", payload={"pipeline": {"id": "p"}})],
        "duplicate_created": [_created("run-x"),
                              _line("run-x", 2, "run.created", payload={
                                  "stages": [{"id": "s", "product": "x"}]}),
                              ],
    }
    for name, lines in cases.items():
        root = tmp_path / name
        _append(_run(root, "run-x") / "events.jsonl", *lines)
        runs, _ = EventsCollector(root).collect()
        assert runs[0].state == "corrupt", name
        assert runs[0].error, name


def test_missing_or_unreadable_state_degrades_honestly(tmp_path: Path) -> None:
    # Run dir with no events.jsonl at all:
    _run(tmp_path, "run-empty-dir").mkdir(parents=True)
    runs, error = EventsCollector(tmp_path).collect()
    assert error is None
    assert runs[0].state == "corrupt"
    assert runs[0].error and "cannot read" in runs[0].error
    # Empty event log (created but nothing fsynced):
    log = _run(tmp_path, "run-zero") / "events.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    runs, _ = EventsCollector(tmp_path).collect()
    zero = next(snap for snap in runs if snap.id == "run-zero")
    assert zero.state == "corrupt"
    assert "empty event log" in zero.error
    # Whole state root missing:
    runs, error = EventsCollector(tmp_path / "nowhere").collect()
    assert runs == ()
    assert error is not None and "cannot scan" in error


def test_shrinking_log_is_corrupt(tmp_path: Path) -> None:
    log = _run(tmp_path, "run-shrink") / "events.jsonl"
    _append(log, _created("run-shrink"), _line("run-shrink", 2, "run.staged"))
    collector = EventsCollector(tmp_path)
    collector.collect()
    log.write_text(_created("run-shrink") + "\n", encoding="utf-8")
    runs, _ = collector.collect()
    assert runs[0].state == "corrupt"
    assert "shrank" in runs[0].error


# ── tail shape ───────────────────────────────────────────────────────


def test_tail_capped_at_200_ascending(tmp_path: Path) -> None:
    total = TAIL_MAX + 60
    lines = [_created("run-tail")]
    for seq in range(2, total + 1):
        lines.append(_line("run-tail", seq, "stage.receipt_accepted",
                           stage="ingest", payload={"digest": DIGEST}))
    _append(_run(tmp_path, "run-tail") / "events.jsonl", *lines)
    runs, _ = EventsCollector(tmp_path).collect()
    snap = runs[0]
    assert len(snap.events_tail) == TAIL_MAX
    seqs = [event.seq for event in snap.events_tail]
    assert seqs == sorted(seqs)  # ascending, newest last
    assert seqs[0] == total - TAIL_MAX + 1
    assert snap.events_head_seq == total
    summaries = [event.summary for event in snap.events_tail]
    assert all(isinstance(text, str) and text for text in summaries)
    assert summaries[-1].startswith("stage ingest receipt ")


def test_summaries_are_compact_and_safe(tmp_path: Path) -> None:
    long_note = "x" * 300
    _append(_run(tmp_path, "run-sum") / "events.jsonl",
            _created("run-sum"),
            _line("run-sum", 2, "run.staged"),
            _line("run-sum", 3, "stage.started", stage="ingest"),
            _line("run-sum", 4, "run.failed", payload={"summary": long_note}),
            )
    runs, _ = EventsCollector(tmp_path).collect()
    tail = runs[0].events_tail
    assert tail[0].summary.startswith("created example.momentum")
    assert tail[-1].kind == "run.failed"
    assert len(tail[-1].summary) <= 80


# ── cost ledger ──────────────────────────────────────────────────────


def _write_ledger(run_dir: Path, run_id: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "cost-ledger.json").write_text(json.dumps({
        "schema": "stammtisch.cost-ledger.v0",
        "run_id": run_id,
        "pipeline_id": "example.momentum",
        "stages": [
            {"stage": "ingest", "product": "highball", "invocations": 1,
             "observations": 2, "wall_seconds": 12.5,
             "tokens": {"input": 100, "output": 50, "total": 150}},
            {"stage": "review", "product": "doctrine", "invocations": 3,
             "observations": 3, "wall_seconds": 40.0,
             "tokens": {"input": 700, "output": 150, "total": 850}},
        ],
        "generated_at": "2026-09-22T10:05:00Z",
    }), encoding="utf-8")


def test_cost_ledger_projects_when_present(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, "run-cost")
    _append(run_dir / "events.jsonl", _created("run-cost"))
    _write_ledger(run_dir, "run-cost")
    runs, _ = EventsCollector(tmp_path).collect()
    cost = runs[0].cost
    assert cost is not None
    assert cost.total == 1000.0
    assert dict(cost.by_stage) == {"ingest": 150.0, "review": 850.0}
    assert cost.generated_at == "2026-09-22T10:05:00Z"
    assert cost.currency == "tokens"  # ledger v0 carries token counts
    assert cost.unit == "tokens"
    assert cost.token_total == 1_000  # 150 + 850, None-honest sum
    assert cost.wall_s == 52.5  # 12.5 + 40.0
    assert [u.stage for u in cost.usage] == ["ingest", "review"]
    assert cost.usage[0].tokens_in == 100 and cost.usage[0].tokens_out == 50


def test_cost_ledger_absent_or_malformed_degrades_to_none(
        tmp_path: Path) -> None:
    _append(_run(tmp_path, "run-a") / "events.jsonl", _created("run-a"))
    run_b = _run(tmp_path, "run-b")
    _append(run_b / "events.jsonl", _created("run-b"))
    (run_b / "cost-ledger.json").write_text("{not json", encoding="utf-8")
    runs, _ = EventsCollector(tmp_path).collect()
    by_id = {snap.id: snap for snap in runs}
    assert by_id["run-a"].cost is None
    assert by_id["run-b"].cost is None
    assert by_id["run-b"].state != "corrupt"  # ledger is auxiliary


# ── discovery and ordering ───────────────────────────────────────────


def test_new_run_dirs_discovered_and_deleted_dropped(tmp_path: Path) -> None:
    collector = EventsCollector(tmp_path)
    _append(_run(tmp_path, "run-one") / "events.jsonl", _created("run-one"))
    assert len(collector.collect()[0]) == 1
    _append(_run(tmp_path, "run-two") / "events.jsonl", _created("run-two"))
    assert {snap.id for snap in collector.collect()[0]} == {"run-one", "run-two"}
    assert collector.cursor("run-two") is not None
    shutil.rmtree(_run(tmp_path, "run-one"))
    assert {snap.id for snap in collector.collect()[0]} == {"run-two"}
    assert collector.cursor("run-one") is None


def test_session_orders_live_first_then_newest(tmp_path: Path) -> None:
    del tmp_path  # pure function under test; no state root needed

    def snap(run_id: str, state: str, created: str) -> RunSnapshot:
        return RunSnapshot(id=run_id, pipeline_id="p", state=state,
                           created_at=created)

    ordered = sort_runs((
        snap("old-done", "completed", "2026-09-01T00:00:00Z"),
        snap("new-live", "running", "2026-09-22T09:00:00Z"),
        snap("mid-created", "created", "2026-09-10T00:00:00Z"),
        snap("newest-done", "failed", "2026-09-23T00:00:00Z"),
    ))
    assert [run.id for run in ordered] == [
        "new-live", "mid-created", "newest-done", "old-done"]


def test_cancelled_run_retires_its_running_stage(tmp_path: Path) -> None:
    """Review A spec finding: run.cancelled must retire running stages —
    a cancelled run used to show a live stage forever."""
    log = _run(tmp_path, "run-cxl") / "events.jsonl"
    _append(log,
            _created("run-cxl"),
            _line("run-cxl", 2, "run.staged"),
            _line("run-cxl", 3, "stage.started", stage="ingest"),
            _line("run-cxl", 4, "run.cancelled"))
    runs, _error = EventsCollector(tmp_path).collect()
    run = runs[0]
    assert run.state == "cancelled"
    assert run.stages[0].state == "halted", \
        "a cancelled run must not keep a live stage"


# ── grilling S4: corrupt-recovery policy parity ────────────────────────


def test_corrupt_stays_sticky_even_when_valid_lines_follow(
        tmp_path: Path) -> None:
    """S4 parity pin: within a collector session a corrupt run NEVER
    recovers, even when more valid lines are appended after the bad one.

    The Rust ``read_events`` re-reads the whole log per call and halts at
    the FIRST bad line — no session state; because events.jsonl is
    append-only, the same bytes fail every read, so our sticky session
    verdict is observationally identical (documented in events.py's
    module docstring). Recovery mid-session is impossible by design.
    """
    log = _run(tmp_path, "run-sticky") / "events.jsonl"
    _append(log,
            _created("run-sticky"),
            "{ not json at all",
            _line("run-sticky", 2, "run.staged"))
    collector = EventsCollector(tmp_path)
    first, _ = collector.collect()
    assert first[0].state == "corrupt"
    assert "line 2" in first[0].error
    # Valid events appended AFTER the poison line must not heal the run.
    _append(log, _line("run-sticky", 3, "run.gating"))
    _append(log, _line("run-sticky", 4, "run.completed"))
    second, _ = collector.collect()
    assert second[0].state == "corrupt", "no mid-session recovery"
    assert second[0].error == first[0].error, "same verdict, same bytes"
    # A FRESH process (new collector) re-derives the same verdict.
    third, _ = EventsCollector(tmp_path).collect()
    assert third[0].state == "corrupt"
