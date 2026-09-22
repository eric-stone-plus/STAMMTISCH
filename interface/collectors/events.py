"""EventsCollector — incremental events.jsonl fold over ``<root>/runs/*/``.

``events.jsonl`` is the authority (never the manifest projection), so this
collector never spawns the core: it discovers run directories, tails each
``events.jsonl`` by BYTE OFFSET, and folds the 16 run-event schema types
into the run/stage/gate state the UI frames carry.

Cursor discipline (the advance-after-commit rule): each tick seeks to the
cursor, reads complete (newline-terminated) lines only, and advances the
offset strictly AFTER a line has both parsed and been folded into the
frame state. A torn last line (append raced the read) is therefore
re-read next tick — never skipped, never consumed.

Corruption is a field, not an exception: an unparseable line, a schema
violation, a sequence gap, or a missing/empty/shrunk log freezes the run
at ``state="corrupt"`` with the error on the row, mirroring the core's
fail-closed ``read_events``. The fold is strict on the reader-side
invariants (shape, seq, run identity, stage identity) and lenient on
writer-side transition legality — the core rejects those at append time;
replaying them read-only would only add ways to lose the frame.

Recovery-policy parity with the Rust core (grilling S4, pinned in
interface/tests/test_events_collector.py): ``src/store.rs``
``read_events`` re-reads the whole log per call and HALTS at the first
bad line — it keeps no session state, and because the log is append-only
the same bytes fail every read. This collector marks the run corrupt
STICKILY for the collector session instead of re-scanning the poison
bytes each tick; for an append-only log the two policies are
observationally identical (a bad line can never be removed or reordered,
so no read, fresh or cached, can ever see a healthy log again). No
mid-session recovery BY DESIGN; a new process re-derives the same
verdict from the same bytes.

Known M2 approximation: the gate event payload carries the record digest
(``record_sha256``) but not the evaluated artifact's own digest — that
lives in ``gates/<stage>.gate.json`` (M3 inspect lane). GateSnapshot
therefore carries the schema-required record digest in
``record_sha256``.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from interface.snapshot import (
    CostSnapshot,
    Event,
    GateSnapshot,
    RunSnapshot,
    StageSnapshot,
    StageUsage,
)

RUN_EVENT_SCHEMA = "stammtisch.run-event.v0"
COST_LEDGER_SCHEMA = "stammtisch.cost-ledger.v0"

#: The 16 event types of schemas/run-event.schema.json, spelled out so an
#: unknown type is corruption rather than a silent skip.
EVENT_TYPES: frozenset[str] = frozenset({
    "run.created", "run.staged", "run.cancelled",
    "stage.started", "stage.receipt_accepted", "stage.artifact_recorded",
    "stage.gate_passed", "stage.gate_failed", "stage.failed",
    "run.gating", "run.completed", "run.blocked", "run.failed",
    "run.halted", "run.reconciled", "run.resumed",
})

#: Activity-feed tail cap per run (newest last, ascending seq).
TAIL_MAX = 200

#: GateSnapshot decisions are normalized to the contract's upper-case
#: vocabulary ("PASS | FAIL | ..."); gate-record payloads are lower-case.
_PASS = "PASS"
_FAIL = "FAIL"

_STAGE_STATE_ON_RUN_TERMINAL = {"blocked": "blocked", "halted": "halted",
                                "failed": "failed",
                                # A run-level cancel aborts its stages too;
                                # "halted" is the stage vocab's abort state.
                                "cancelled": "halted"}


class _Corrupt(Exception):
    """One event line violated the fold contract; the run is corrupt."""


@dataclass
class _StageFold:
    """Mutable fold state for one declared pipeline stage."""

    id: str
    product: str
    state: str = "idle"  # idle | running | completed | failed | blocked | halted
    gate: str | None = None  # gate id evaluated at this stage, if any


@dataclass
class _RunFold:
    """Mutable per-run fold state; the byte cursor survives ticks."""

    run_id: str
    offset: int = 0  # bytes of events.jsonl already folded
    last_seq: int = 0  # last consumed seq (strict +1 discipline)
    line_no: int = 0  # physical lines consumed, for error messages
    error: str | None = None  # sticky corrupt marker
    state: str = "created"
    pipeline_id: str = ""
    created_at: str = ""
    stages: list[_StageFold] = field(default_factory=list)
    gates: list[GateSnapshot] = field(default_factory=list)
    tail: deque[Event] = field(default_factory=lambda: deque(maxlen=TAIL_MAX))
    cost: CostSnapshot | None = None
    cost_stamp: tuple[int, int] | None = None  # (size, mtime_ns) of the ledger


def _is_sha256(value: str) -> bool:
    if not value.startswith("sha256:"):
        return False
    hexpart = value[len("sha256:"):]
    return len(hexpart) == 64 and all(c in "0123456789abcdef" for c in hexpart)


def _digest(payload: dict[str, Any], key: str, kind: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not _is_sha256(value):
        raise _Corrupt(f"{kind} has no valid payload.{key}")
    return value


def _short_digest(value: Any) -> str:
    if isinstance(value, str) and value.startswith("sha256:"):
        return value[len("sha256:"):][:8]
    return "?"


def _clip(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summary(kind: str, stage: str | None, payload: dict[str, Any]) -> str:
    """One compact, presentation-safe feed line for an event."""
    where = stage or "?"
    if kind == "run.created":
        pipeline = payload.get("pipeline")
        pid = pipeline.get("id") if isinstance(pipeline, dict) else None
        stages = payload.get("stages")
        count = len(stages) if isinstance(stages, list) else 0
        plural = "s" if count != 1 else ""
        return f"created {pid or '?'} ({count} stage{plural})"
    if kind == "run.staged":
        return "staged for execution"
    if kind == "stage.started":
        return f"stage {where} started"
    if kind == "stage.receipt_accepted":
        return f"stage {where} receipt {_short_digest(payload.get('digest'))}"
    if kind == "stage.artifact_recorded":
        name = payload.get("name")
        return f"stage {where} artifact {name if isinstance(name, str) else '?'}"
    if kind == "stage.gate_passed":
        return f"stage {where} gate {payload.get('gate_id', where)} PASS"
    if kind == "stage.gate_failed":
        return f"stage {where} gate {payload.get('gate_id', where)} FAIL"
    if kind == "stage.failed":
        reason = payload.get("reason")
        suffix = f" ({reason})" if isinstance(reason, str) and reason else ""
        return f"stage {where} failed{suffix}"
    if kind == "run.gating":
        return "final gates evaluating"
    if kind == "run.completed":
        digest = payload.get("bundle_manifest_sha256")
        sealed = _short_digest(digest) if isinstance(digest, str) else ""
        return f"bundle sealed {sealed}".rstrip()
    if kind in ("run.blocked", "run.failed", "run.halted", "run.cancelled"):
        word = kind.removeprefix("run.")
        note = payload.get("summary") or payload.get("note")
        return f"{word}: {note}" if isinstance(note, str) and note else word
    if kind == "run.reconciled":
        note = payload.get("note")
        return f"reconciled: {note}" if isinstance(note, str) and note else "reconciled"
    return "resumed"


def _read_cost(path: Path) -> CostSnapshot | None:
    """Project cost-ledger.json; unreadable/malformed degrades to None.

    cost-ledger v0 carries token counts and wall seconds, not money and no
    currency field — so M2 sums the per-stage token totals and labels the
    unit honestly ("tokens") instead of inventing a currency.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != COST_LEDGER_SCHEMA:
        return None
    rows = data.get("stages")
    if not isinstance(rows, list):
        return None
    by_stage: dict[str, float] = {}
    usage: list[StageUsage] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("stage")
        if not isinstance(sid, str) or not sid:
            continue
        tokens = tokens if isinstance((tokens := row.get("tokens")), dict) else {}
        wall = row.get("wall_seconds")

        def _int(value: object) -> int | None:
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        def _float(value: object) -> float | None:
            return (float(value)
                    if isinstance(value, (int, float))
                    and not isinstance(value, bool) else None)

        row_usage = StageUsage(
            stage=sid,
            tokens_in=_int(tokens.get("input")),
            tokens_out=_int(tokens.get("output")),
            tokens_total=_int(tokens.get("total")),
            wall_s=_float(wall),
        )
        usage.append(row_usage)
        # Legacy alias: only stages that actually reported a numeric total
        # appear here — a schema-legal null never becomes an invented 0.0.
        if row_usage.tokens_total is not None:
            by_stage[sid] = float(row_usage.tokens_total)
    totals = [u.tokens_total for u in usage]
    walls = [u.wall_s for u in usage]
    generated = data.get("generated_at")
    return CostSnapshot(
        total=float(sum(t for t in totals if t is not None)),
        by_stage=by_stage,
        token_total=(sum(t for t in totals if t is not None)
                     if totals and all(t is not None for t in totals) else None),
        wall_s=(sum(w for w in walls if w is not None)
                if walls and all(w is not None for w in walls) else None),
        usage=tuple(usage),
        generated_at=generated if isinstance(generated, str) else "",
    )


class EventsCollector:
    """Discovers runs under ``<root>/runs`` and tails each events.jsonl.

    Long-lived: the per-run byte cursors survive ticks, so a frame costs
    one directory scan plus the bytes appended since the last frame. All
    failures degrade to rows/fields; nothing raises into the UI.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._folds: dict[str, _RunFold] = {}
        #: Zero-rescan accounting (the live integration test locks this):
        #: total bytes pulled off disk and number of tail reads, lifetime.
        self.bytes_read = 0
        self.chunk_reads = 0

    # ── public ─────────────────────────────────────────────────────

    def collect(self) -> tuple[tuple[RunSnapshot, ...], str | None]:
        """One pass: (runs in discovery order, error or None).

        Frame-level ordering (live first, then newest) is the session's
        job — see :func:`interface.collectors.session.sort_runs`.
        """
        runs_dir = self.root / "runs"
        try:
            names = sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
        except OSError as exc:
            return (), f"events: cannot scan {runs_dir}: {exc}"
        for name in names:
            fold = self._folds.get(name)
            if fold is None:
                fold = _RunFold(run_id=name)
                self._folds[name] = fold
            self._tick(fold, runs_dir / name)
        for gone in set(self._folds) - set(names):
            del self._folds[gone]  # a deleted run dir releases its cursor
        return tuple(self._project(fold) for fold in self._folds.values()), None

    def cursor(self, run_id: str) -> int | None:
        """Current byte offset into one run's events.jsonl (ops/tests)."""
        fold = self._folds.get(run_id)
        return None if fold is None else fold.offset

    # ── tailing ────────────────────────────────────────────────────

    def _tick(self, fold: _RunFold, run_dir: Path) -> None:
        self._refresh_cost(fold, run_dir)
        if fold.error is not None:
            return  # corrupt is sticky: an append-only log cannot heal
        path = run_dir / "events.jsonl"
        try:
            size = path.stat().st_size
        except OSError as exc:
            fold.error = f"cannot read {path}: {exc}"
            return
        if size == 0:
            fold.error = f"{path}: empty event log"
            return
        if size < fold.offset:
            fold.error = (f"{path}: append-only log shrank "
                          f"({size} bytes < cursor {fold.offset})")
            return
        try:
            with path.open("rb") as handle:
                handle.seek(fold.offset)
                chunk = handle.read()
        except OSError as exc:
            fold.error = f"cannot read {path}: {exc}"
            return
        self.chunk_reads += 1
        self.bytes_read += len(chunk)
        end = chunk.rfind(b"\n")
        if end < 0:
            return  # no complete line yet: cursor holds, re-read next tick
        for raw in chunk[: end + 1].split(b"\n")[:-1]:
            fold.line_no += 1
            try:
                self._fold_line(fold, raw)
            except _Corrupt as exc:
                fold.error = f"{path}: line {fold.line_no}: {exc}"
                return  # cursor stays at this line's start: fail closed
            fold.offset += len(raw) + 1  # advance-after-commit

    def _fold_line(self, fold: _RunFold, raw: bytes) -> None:
        if not raw.strip():
            return  # blank lines are tolerated (core read_events skips them)
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _Corrupt(f"unparseable: {exc}") from exc
        if not isinstance(event, dict):
            raise _Corrupt("event is not a JSON object")
        if event.get("schema") != RUN_EVENT_SCHEMA:
            raise _Corrupt(f"unknown schema {event.get('schema')!r}")
        if event.get("run_id") != fold.run_id:
            raise _Corrupt(f"belongs to run {event.get('run_id')!r}, "
                           f"expected {fold.run_id!r}")
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise _Corrupt("seq is not an integer")
        if seq != fold.last_seq + 1:
            raise _Corrupt(f"sequence gap: expected {fold.last_seq + 1}, got {seq}")
        kind = event.get("type")
        if not isinstance(kind, str) or kind not in EVENT_TYPES:
            raise _Corrupt(f"unknown event type {kind!r}")
        at = event.get("at")
        if not isinstance(at, str) or not at:
            raise _Corrupt("at is not a string")
        stage = event.get("stage")
        if stage is not None and not isinstance(stage, str):
            raise _Corrupt("stage is not a string")
        payload = event.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise _Corrupt("payload is not an object")
        self._apply(fold, kind, stage, at, payload)
        fold.tail.append(Event(seq=seq, ts=at, kind=kind,
                               summary=_clip(_summary(kind, stage, payload))))
        fold.last_seq = seq

    # ── the fold ───────────────────────────────────────────────────

    def _apply(self, fold: _RunFold, kind: str, stage: str | None,
               at: str, payload: dict[str, Any]) -> None:
        if kind != "run.created" and not fold.stages:
            raise _Corrupt(f"{kind} appears before run.created")
        if kind == "run.created":
            self._apply_created(fold, at, payload)
        elif kind == "run.staged":
            fold.state = "staged"
        elif kind == "stage.started":
            index = self._stage_index(fold, stage, kind)
            fold.stages[index].state = "running"
            fold.state = "running"
        elif kind == "stage.receipt_accepted":
            self._stage_index(fold, stage, kind)
            _digest(payload, "digest", kind)
        elif kind == "stage.artifact_recorded":
            self._stage_index(fold, stage, kind)
            _digest(payload, "digest", kind)
            name = payload.get("name")
            if not isinstance(name, str) or not name:
                raise _Corrupt("stage.artifact_recorded has no string name")
        elif kind in ("stage.gate_passed", "stage.gate_failed"):
            self._apply_gate(fold, kind, stage, at, payload)
        elif kind == "stage.failed":
            index = self._stage_index(fold, stage, kind)
            refused = payload.get("reason") == "product_refused"
            fold.stages[index].state = "blocked" if refused else "failed"
        elif kind == "run.gating":
            fold.state = "gating"
        elif kind == "run.completed":
            fold.state = "completed"
        elif kind in ("run.blocked", "run.halted", "run.failed", "run.cancelled"):
            state = kind.removeprefix("run.")
            if state in _STAGE_STATE_ON_RUN_TERMINAL:
                # Assign the MAPPED stage state: run "cancelled" retires
                # stages to the stage vocab's "halted", never a run-state
                # word leaking into StageSnapshot.state.
                stage_state = _STAGE_STATE_ON_RUN_TERMINAL[state]
                for row in fold.stages:
                    if row.state == "running":
                        row.state = stage_state
            fold.state = state
        elif kind == "run.reconciled":
            fold.state = "reconciled"
        elif kind == "run.resumed":
            fold.state = "resumed"
        else:  # pragma: no cover - guarded by EVENT_TYPES
            raise _Corrupt(f"unknown event type {kind!r}")

    def _apply_created(self, fold: _RunFold, at: str,
                       payload: dict[str, Any]) -> None:
        if fold.stages or fold.last_seq:
            raise _Corrupt("run.created appears more than once")
        declared = payload.get("stages")
        if not isinstance(declared, list) or not declared:
            raise _Corrupt("run.created has no stages array")
        seen: set[str] = set()
        for row in declared:
            if not isinstance(row, dict):
                raise _Corrupt("declared stage is not an object")
            sid = row.get("id")
            product = row.get("product")
            if not isinstance(sid, str) or not sid:
                raise _Corrupt("declared stage has no string id")
            if not isinstance(product, str) or not product:
                raise _Corrupt(f"stage '{sid}' has no string product")
            if sid in seen:
                raise _Corrupt(f"run.created declares duplicate stage '{sid}'")
            seen.add(sid)
            fold.stages.append(_StageFold(id=sid, product=product))
        pipeline = payload.get("pipeline")
        if isinstance(pipeline, dict) and isinstance(pipeline.get("id"), str):
            fold.pipeline_id = pipeline["id"]
        fold.created_at = at

    def _apply_gate(self, fold: _RunFold, kind: str, stage: str | None,
                    at: str, payload: dict[str, Any]) -> None:
        index = self._stage_index(fold, stage, kind)
        digest = _digest(payload, "record_sha256", kind)
        gate_id = payload.get("gate_id")
        if not isinstance(gate_id, str) or not gate_id:
            gate_id = stage or "?"
        decision = _PASS if kind == "stage.gate_passed" else _FAIL
        fold.stages[index].gate = gate_id
        fold.stages[index].state = "completed" if decision == _PASS else "blocked"
        fold.gates.append(GateSnapshot(
            gate_id=gate_id, stage=stage or "?", decision=decision,
            record_sha256=digest, evaluated_at=at))

    def _stage_index(self, fold: _RunFold, stage: str | None, kind: str) -> int:
        if stage is None:
            raise _Corrupt(f"{kind} names no stage")
        for index, row in enumerate(fold.stages):
            if row.id == stage:
                return index
        raise _Corrupt(f"{kind} names unknown stage '{stage}'")

    # ── projection ─────────────────────────────────────────────────

    def _refresh_cost(self, fold: _RunFold, run_dir: Path) -> None:
        path = run_dir / "cost-ledger.json"
        try:
            stat = path.stat()
        except OSError:
            if fold.cost_stamp is not None:
                fold.cost = None
                fold.cost_stamp = None
            return
        stamp = (stat.st_size, stat.st_mtime_ns)
        if stamp == fold.cost_stamp:
            return
        fold.cost_stamp = stamp
        fold.cost = _read_cost(path)

    def _project(self, fold: _RunFold) -> RunSnapshot:
        """Fresh contract containers per frame (freeze is shallow)."""
        if fold.error is not None:
            return RunSnapshot(
                id=fold.run_id,
                pipeline_id=fold.pipeline_id or "?",
                state="corrupt",
                created_at=fold.created_at,
                error=fold.error,
            )
        return RunSnapshot(
            id=fold.run_id,
            pipeline_id=fold.pipeline_id or "?",
            state=fold.state,
            created_at=fold.created_at,
            stages=tuple(StageSnapshot(id=row.id, product=row.product,
                                       state=row.state, gate=row.gate)
                         for row in fold.stages),
            gates=tuple(fold.gates),
            cost=fold.cost,
            events_tail=tuple(fold.tail),
            events_head_seq=fold.last_seq,
        )
