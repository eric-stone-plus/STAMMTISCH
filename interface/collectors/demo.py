"""Deterministic demo collector — the M0 data source and test fixture.

A pure function of ``now``: the same instant produces the same snapshot;
successive instants stream (event seqs advance, a run crosses gating ->
completed, a cooldown-like quote ages past the staleness threshold). The
fixtures are engineered to exercise BOTH sides of every flag threshold so
UI code paths are testable without the Rust core, quantkit, or any network.

No I/O, no imports beyond the contract + stdlib.
"""

from __future__ import annotations

import time

from interface.snapshot import (
    QUOTE_STALE_S,
    CostSnapshot,
    Event,
    GateSnapshot,
    IntakeSessionSnapshot,
    QuoteSnapshot,
    RunSnapshot,
    ServiceStatus,
    StageSnapshot,
    WorkstationSnapshot,
)

_SEQ_BASE = 4_100


def _ts(offset_s: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(time.time() - offset_s))


def _events(seq: int, cycle: int) -> tuple[Event, ...]:
    """A synthetic tail (newest last) in lifecycle order.

    ``seq`` is the head; ``cycle`` rotates which catalog entry leads so
    consecutive frames visibly move while staying realistic.
    """
    catalog = (
        ("run.staged", "pipeline staged for execution"),
        ("stage.started", "stage fetch started"),
        ("stage.receipt_accepted", "receipt accepted digest ok"),
        ("stage.gate_passed", "gate floor-pass"),
        ("stage.gate_failed", "gate drawdown-cap"),
        ("stage.artifact_recorded", "artifact recorded"),
        ("run.gating", "final gates evaluating"),
        ("run.completed", "bundle sealed"),
    )
    start = cycle % len(catalog)
    rotated = catalog[start:] + catalog[:start]
    return tuple(
        Event(seq=seq - len(rotated) + i, ts=_ts(i * 3.0),
              kind=kind, summary=summary)
        for i, (kind, summary) in enumerate(rotated)
    )


def _running_run(now: float) -> RunSnapshot:
    seq = _SEQ_BASE + int(now) % 900
    return RunSnapshot(
        id="demo-run-live",
        pipeline_id="demo.momentum",
        state="running",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1_800)),
        stages=(
            StageSnapshot("ingest", "highball", "completed", gate="data.floor"),
            StageSnapshot("review", "doctrine", "running", gate=None),
        ),
        gates=(GateSnapshot("data.floor", "ingest", "PASS",
                            "a1b2c3d4e5f6" * 4, _ts(600.0)),),
        events_tail=_events(seq, int(now)),
        events_head_seq=seq,
    )


def _gating_or_completed_run(now: float) -> RunSnapshot:
    """Crosses gating -> completed 240s into its window, then stays final."""
    elapsed_phase = (now % 600.0)
    done = elapsed_phase > 240.0
    return RunSnapshot(
        id="demo-run-final",
        pipeline_id="demo.meanrev",
        state="completed" if done else "gating",
        created_at=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - elapsed_phase - 120.0)),
        stages=(
            StageSnapshot("ingest", "highball", "completed", gate="data.floor"),
            StageSnapshot("review", "doctrine", "completed", gate="risk.cap"),
        ),
        gates=(
            GateSnapshot("data.floor", "ingest", "PASS", "b2c3d4e5f6a1" * 4,
                         _ts(elapsed_phase)),
            GateSnapshot("risk.cap", "review",
                         "PASS" if done else "FAIL", "c3d4e5f6a1b2" * 4,
                         _ts(max(0.0, elapsed_phase - 60.0))),
        ),
        cost=CostSnapshot(
            total=0.0184,
            by_stage={"ingest": 0.0041, "review": 0.0143},
            generated_at=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - elapsed_phase)),
        ),
        events_tail=(),
    )


def _corrupt_run(now: float) -> RunSnapshot:
    return RunSnapshot(
        id="demo-run-corrupt",
        pipeline_id="?",
        state="corrupt",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 86_400)),
        error="events.jsonl digest mismatch (fixture)",
    )


def _quotes(now: float) -> tuple[QuoteSnapshot, ...]:
    def _age(base: float) -> float:
        # One symbol drifts through QUOTE_STALE_S on a 240s cycle so the F
        # flag is exercisable on both sides (20s fresh .. 259s stale).
        return base + (now % 240.0)

    return (
        QuoteSnapshot("000001.SS", "SH COMP", 3_289.4, 0.62, "livefeed",
                      _age(2.0)),
        QuoteSnapshot("HSI", "HSI", 24_103.7, -0.31, "livefeed", _age(3.0)),
        QuoteSnapshot("QQQ", "QQX(US)", 512.06, 0.94, "cached",
                      _age(20.0)),  # 20s..259s: crosses QUOTE_STALE_S
    )


def demo_snapshot(now: float | None = None) -> WorkstationSnapshot:
    """One deterministic demo frame (pure function of ``now``)."""
    current = time.time() if now is None else now
    elapsed_day = current % 86_400.0
    return WorkstationSnapshot(
        taken_at=current,
        runs=(_running_run(current), _gating_or_completed_run(current),
              _corrupt_run(current)),
        intake=(
            IntakeSessionSnapshot(
                id="demo-intake-live", state="capturing",
                updated_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(current - 5.0)),
                report_date=time.strftime("%Y-%m-%d", time.gmtime(current)),
            ),
            IntakeSessionSnapshot(
                id="demo-intake-old", state="interrupted",
                updated_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(current - 259_200.0)),
            ),
        ),
        quotes=_quotes(current),
        services=(
            ServiceStatus("core", True, "stammtisch-core ready"),
            ServiceStatus("quantkit", True, ""),
            ServiceStatus("ai", elapsed_day > 43_200.0,
                          "no API key (demo alternates)"),
        ),
        collector_error=None,
    )


# Exercisability guards: the demo MUST keep both sides of each threshold
# reachable, or the UI paths it feeds become untestable.
assert any(q.age_s <= QUOTE_STALE_S for q in _quotes(0.0))
assert any(q.age_s > QUOTE_STALE_S for q in _quotes(120.0))
