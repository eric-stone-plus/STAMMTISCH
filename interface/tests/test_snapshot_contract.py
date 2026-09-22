"""Contract tests: frozen snapshots, flag derivation, threshold semantics."""

from __future__ import annotations

import dataclasses

import pytest

from interface.collectors.demo import demo_snapshot
from interface.snapshot import (
    FLAG_LETTERS,
    QUOTE_STALE_S,
    RunSnapshot,
    run_flags,
    workstation_flags,
)


def test_frames_are_frozen() -> None:
    frame = demo_snapshot(1_000.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.taken_at = 2.0  # type: ignore[misc]
    run = frame.runs[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        run.state = "completed"  # type: ignore[misc]


def test_flag_letters_fixed_order() -> None:
    assert FLAG_LETTERS == ("R", "H", "G", "F", "D", "C")


def test_run_flag_derivation() -> None:
    def run(state: str) -> RunSnapshot:
        return RunSnapshot(id="r", pipeline_id="p", state=state, created_at="")

    assert run_flags(run("running"))["R"]
    assert run_flags(run("halted"))["H"]
    assert run_flags(run("blocked"))["H"]
    assert run_flags(run("gating"))["G"]
    assert not any(run_flags(run("completed")).values())


def test_workstation_flags_from_demo() -> None:
    frame = demo_snapshot(1_000_000.0)
    flags = workstation_flags(frame)
    assert flags["R"], "demo always has a running run"
    assert flags["C"], "demo always has a capturing intake session"
    # 1_000_000 % 240 = 160 -> QQQ age 20+160 > QUOTE_STALE_S -> stale feed.
    assert flags["F"] == any(q.age_s > QUOTE_STALE_S for q in frame.quotes)


def test_quote_threshold_both_sides_reachable() -> None:
    fresh = demo_snapshot(0.0)
    stale = demo_snapshot(120.0)
    assert any(q.age_s <= QUOTE_STALE_S for q in fresh.quotes)
    assert any(q.age_s > QUOTE_STALE_S for q in stale.quotes)
