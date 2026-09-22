"""Frozen snapshot contract for the STAMMTISCH interface.

The single interface between data collectors and every UI tier. Rules
(carry-overs from the reviewed MOTOKO interface design, adapted to this
workstation):

- Every dataclass is ``frozen=True``; the UI reads whole consistent frames
  and needs no locks. Collectors hand FRESH containers per frame (freeze is
  shallow; convention plus collector discipline, one known limit).
- Contract growth is ADDITIVE only: new fields carry defaults so existing
  constructors and tests keep working.
- Failure is a field, not an exception: ``collector_error`` rides the frame;
  collectors degrade to empty facts and never raise into the UI.
- Semantic thresholds live here so all tiers share one definition.
- ``SnapshotProvider`` is the whole seam: anything callable returning one
  consistent frame. Tests and the demo inject plain functions.

Domain vocabulary mirrors the Rust core's run-event schema (16 event
types), the gate-record / cost-ledger schemas, and the intake session
states observed on disk.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

#: Letter flags, FIXED ORDER (render/flags.py renders this width everywhere:
#: active letters colored, inactive as a dim placeholder — grep-friendly and
#: colorblind-safe).
FLAG_LETTERS: tuple[str, ...] = ("R", "H", "G", "F", "D", "C")

#: A quote older than this renders the F (stale feed) flag. Market data on
#: this workstation is a glance surface, not a tick stream; 90s is well past
#: the healthy polling cadences (5s boards, 30s glance).
QUOTE_STALE_S = 90.0

#: Run lifecycle states as projected from events.jsonl (run-event schema
#: types) plus the two derived states the CLI surfaces: ``corrupt`` (status
#: marks a run whose manifest fails to load) and ``interrupted`` (an intake
#: session left ``capturing`` on disk by a dead host).
#:
#: DIVERGENCE, deliberate (grilling adjudication 3): the core's manifest
#: fold leaves state.code unchanged on run.reconciled/run.resumed (its enum
#: cannot carry the words), so a dead host's run keeps its pre-death state
#: in `stammtisch status`. We project the named states — a dead host must
#: not render "running" forever. Engine-side gap recorded in
#: REVIEWS/GRILLING.md; revisit if the core's fold changes.
RUN_STATES: tuple[str, ...] = (
    "created", "staged", "running", "gating",
    "completed", "blocked", "failed", "halted", "cancelled",
    "reconciled", "resumed", "corrupt",
)
TERMINAL_RUN_STATES = frozenset(
    {"completed", "failed", "halted", "cancelled", "blocked"}
)


@dataclass(frozen=True)
class Event:
    """One events.jsonl line projected for the activity feed."""

    seq: int
    ts: str
    kind: str  # run-event schema type, e.g. "stage.gate_failed"
    summary: str  # compact, already presentation-safe


@dataclass(frozen=True)
class StageSnapshot:
    """One pipeline stage of a run."""

    id: str
    product: str  # doctrine/highball/a2a product name
    state: str  # idle | running | completed | failed | blocked | halted
    gate: str | None = None  # gate id evaluated at this stage, if any


@dataclass(frozen=True)
class GateSnapshot:
    """One gate record (gate-record schema projection).

    ``record_sha256`` is the gate RECORD digest carried by the event
    payload (grilling adjudication 2: the old name ``artifact_sha256``
    lied — the evaluated artifact's digest lives in
    ``gates/<stage>.gate.json``, readable lazily by detail views, never
    needed by the fold).
    """

    gate_id: str
    stage: str
    decision: str  # PASS | FAIL | ...
    record_sha256: str
    evaluated_at: str


@dataclass(frozen=True)
class StageUsage:
    """One stage's invocation usage, None-honest (a schema-legal missing
    value is None, never an invented 0 — grilling adjudication 1)."""

    stage: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_total: int | None = None
    wall_s: float | None = None


@dataclass(frozen=True)
class CostSnapshot:
    """Aggregated per-stage invocation usage (cost-ledger projection).

    cost-ledger v0 carries token counts + wall seconds, not money. The
    money-shaped ``total``/``by_stage``/``currency`` fields remain as
    token-sum aliases for back-compat; honest consumers read
    ``unit``/``token_total``/``wall_s``/``usage`` (adjudication 1).
    """

    total: float = 0.0
    by_stage: Mapping[str, float] = field(default_factory=dict)
    currency: str = "tokens"
    unit: str = "tokens"
    token_total: int | None = None
    wall_s: float | None = None
    usage: tuple[StageUsage, ...] = ()
    generated_at: str = ""  # ledger generated_at, ISO


@dataclass(frozen=True)
class RunSnapshot:
    """Everything the UI shows about one pipeline run, one consistent frame."""

    id: str
    pipeline_id: str
    state: str  # RUN_STATES
    created_at: str  # ISO date-time from the core
    stages: tuple[StageSnapshot, ...] = ()
    gates: tuple[GateSnapshot, ...] = ()
    cost: CostSnapshot | None = None
    events_tail: tuple[Event, ...] = ()  # newest last, ascending seq
    events_head_seq: int = 0
    error: str | None = None  # e.g. the corrupt-run error from status


@dataclass(frozen=True)
class IntakeSessionSnapshot:
    """One daily-intake session row (supervisor-live or on-disk)."""

    id: str
    state: str  # capturing | accepted | rejected | interrupted | unknown
    updated_at: str
    report_date: str = ""


@dataclass(frozen=True)
class QuoteSnapshot:
    """One glance symbol with explicit provenance and age."""

    symbol: str
    label: str
    last: float
    change_pct: float
    source: str  # the feed that actually served this value
    age_s: float


@dataclass(frozen=True)
class ServiceStatus:
    """One optional service plane (core CLI, quantkit, AI, feeds registry)."""

    name: str
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class WorkstationSnapshot:
    """One frame of the whole workstation; posted to the UI per tick."""

    taken_at: float
    runs: tuple[RunSnapshot, ...] = ()
    intake: tuple[IntakeSessionSnapshot, ...] = ()
    quotes: tuple[QuoteSnapshot, ...] = ()
    services: tuple[ServiceStatus, ...] = ()
    collector_error: str | None = None


class SnapshotProvider(Protocol):
    """The data-layer seam: any zero-arg callable returning one frame."""

    def __call__(self) -> WorkstationSnapshot: ...


def run_flags(run: RunSnapshot, *, feed_stale: bool = False,
              degraded: bool = False) -> dict[str, bool]:
    """Derive the fixed-order letter flags for one run row.

    ``R`` running · ``H`` halted/blocked · ``G`` gating · ``F`` stale feed ·
    ``D`` degraded services · ``C`` intake capturing (row-level only for
    intake rows; False for runs).
    """
    return {
        "R": run.state == "running",
        "H": run.state in ("halted", "blocked"),
        "G": run.state == "gating",
        "F": feed_stale,
        "D": degraded,
        "C": False,
    }


def workstation_flags(snapshot: WorkstationSnapshot) -> dict[str, bool]:
    """Workstation-level flags for the status banner (F/D/C triage)."""
    feed_stale = any(q.age_s > QUOTE_STALE_S for q in snapshot.quotes)
    degraded = any(not s.available for s in snapshot.services)
    capturing = any(s.state == "capturing" for s in snapshot.intake)
    return {
        "R": any(r.state == "running" for r in snapshot.runs),
        "H": any(r.state in ("halted", "blocked") for r in snapshot.runs),
        "G": any(r.state == "gating" for r in snapshot.runs),
        "F": feed_stale,
        "D": degraded,
        "C": capturing,
    }
