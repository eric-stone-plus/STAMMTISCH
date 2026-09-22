"""Demo collector tests: determinism, streaming, state crossing, honesty."""

from __future__ import annotations

from interface.collectors.demo import demo_snapshot


def test_same_instant_same_frame() -> None:
    assert demo_snapshot(1_000.0) == demo_snapshot(1_000.0)


def test_later_instant_streams_seq() -> None:
    early = demo_snapshot(1_000.0)
    late = demo_snapshot(1_001.5)
    assert late.runs[0].events_head_seq != early.runs[0].events_head_seq
    tail = late.runs[0].events_tail
    assert [e.seq for e in tail] == sorted(e.seq for e in tail), "ascending"


def test_gating_to_completed_crossing() -> None:
    before = demo_snapshot(240.0)  # exactly at the boundary stays gating
    after = demo_snapshot(241.0)
    assert before.runs[1].state == "gating"
    assert after.runs[1].state == "completed"
    gate_before = before.runs[1].gates[1].decision
    gate_after = after.runs[1].gates[1].decision
    assert (gate_before, gate_after) == ("FAIL", "PASS")


def test_corrupt_run_carries_error_not_fake_state() -> None:
    corrupt = demo_snapshot(1_000.0).runs[2]
    assert corrupt.state == "corrupt"
    assert corrupt.error and "digest mismatch" in corrupt.error


def test_intake_both_states_present() -> None:
    frame = demo_snapshot(1_000.0)
    states = {s.state for s in frame.intake}
    assert {"capturing", "interrupted"} <= states


def test_services_degradation_alternates() -> None:
    up = demo_snapshot(1_000.0)  # elapsed_day < 43200 -> ai unavailable
    down = demo_snapshot(50_000.0)
    ai_up = next(s for s in up.services if s.name == "ai")
    ai_down = next(s for s in down.services if s.name == "ai")
    assert (ai_up.available, ai_down.available) == (False, True)
