"""IntakeResultFormatter tests — render-ready reshaping of a verified
intake result (tui/screens/daily_intake.py:254-414) with synthetic
artifact files; every defensive clause from the old screen is pinned."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from interface.services.intake_format import (
    artifact_document,
    canonical_records,
    evidence_exceptions,
    format_intake_result,
)


@dataclass
class FakeResult:
    ok: bool
    envelope: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Path] = field(default_factory=dict)
    error: str | None = None


def _write(path: Path, payload: Any) -> Path:
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8")
    return path


def _artifacts(tmp_path: Path) -> dict[str, Path]:
    canonical = _write(tmp_path / "canonical.json", {
        "records": [
            {"title": "CN morning note", "market": "CN"},
            {"title": "  ", "market": "CN"},        # blank title skipped
            "not-a-dict",                            # non-dict skipped
            {"title": "US tape", "market": "US"},
        ]})
    evidence = _write(tmp_path / "evidence.json", {
        "captures": [
            {"status": "failed", "source": "wire-a"},
            {"status": "pruned", "source": "wire-b"},
            {"status": "ok", "source": "wire-c"},
            {},                                      # shapeless skipped
        ]})
    report_json = _write(tmp_path / "report.json", {"date": "2026-09-22"})
    return {"evidence_manifest": evidence, "canonical_dataset": canonical,
            "report_json": report_json}


def _accepted_result(tmp_path: Path) -> FakeResult:
    return FakeResult(
        ok=True,
        envelope={
            "date": "2026-09-22",
            "market_counts": {"us": 4, "cn": 7},
            "quality": {"status": "passed", "issues": []},
            "artifacts": {"report_json": {"sha256": "ab" * 32}},
        },
        counts={"expected": 12, "succeeded": 10, "failed": 1, "pruned": 1,
                "canonical_records": 7},
        artifacts=_artifacts(tmp_path),
    )


# ── accepted frame (old daily_intake.py:286-363) ──────────────────────


def test_accepted_frame_shapes(tmp_path: Path) -> None:
    view = format_intake_result(_accepted_result(tmp_path))
    assert (view.ok, view.status, view.date) == (True, "ACCEPTED",
                                                 "2026-09-22")
    assert view.quality_label == "PASSED"
    # PASSED with no issues → the old "None reported." fallback line.
    assert view.quality_issues == ("None reported.",)
    assert [f"{k}={v}" for k, v in view.market_counts] == ["cn=7", "us=4"]
    assert view.market_counts_from_envelope
    assert (view.counts.succeeded, view.counts.expected) == ("10", "12")
    assert (view.counts.failed, view.counts.pruned,
            view.counts.canonical_records) == (1, 1, "7")
    assert [(r.index, r.market, r.title) for r in view.canonical_records] == [
        (1, "CN", "CN morning note"), (2, "US", "US tape")]
    assert view.canonical_error is None
    assert view.evidence.failed == ("wire-a",)
    assert view.evidence.pruned == ("wire-b",)
    labels = [a.label for a in view.artifacts]
    assert labels == ["evidence_manifest", "canonical_dataset", "report_json"]
    report_row = view.artifacts[-1]
    assert report_row.sha256_prefix == "ab" * 8  # first 16 hex chars
    assert report_row.path.endswith("report.json")
    assert view.notes == (
        "Report JSON was derived from the canonical dataset.",
        "Report HTML was derived from that report JSON.")


def test_quality_gate_issues_and_unknown_fallback(tmp_path: Path) -> None:
    result = _accepted_result(tmp_path)
    result.envelope["quality"] = {"status": "warn", "issues": [
        "wire-a stale", 42, ""]}
    view = format_intake_result(result)
    assert (view.quality_label, view.quality_issues) == (
        "WARN", ("wire-a stale",))  # non-str/blank issues filtered (old 299)
    result.envelope["quality"] = {}  # no status at all
    view = format_intake_result(result)
    assert view.quality_label == "UNKNOWN"
    assert "did not provide" in view.quality_issues[0]  # old 319 fallback


def test_missing_counts_render_unknown_not_zero(tmp_path: Path) -> None:
    result = _accepted_result(tmp_path)
    result.counts = {}
    view = format_intake_result(result)
    assert (view.counts.succeeded, view.counts.expected,
            view.counts.canonical_records) == ("?", "?", "?")
    assert (view.counts.failed, view.counts.pruned) == (0, 0)


def test_no_market_counts_means_dataset_reporting(tmp_path: Path) -> None:
    result = _accepted_result(tmp_path)
    del result.envelope["market_counts"]
    view = format_intake_result(result)
    assert view.market_counts == () and not view.market_counts_from_envelope


# ── rejected frame (old daily_intake.py:256-284) ──────────────────────


def test_rejected_frame_shapes(tmp_path: Path) -> None:
    result = _accepted_result(tmp_path)
    result.ok = False
    result.error = "wire-a contract violation"
    result.envelope = {
        "session_markets": ["CN", "US"],
        "quality": {"issues": ["wire-a stale"]},
    }
    view = format_intake_result(result)
    assert (view.status, view.error) == ("REJECTED",
                                         "wire-a contract violation")
    assert view.session_markets == ("CN", "US")
    assert view.quality_issues == ("wire-a stale",)
    assert view.evidence.failed == ("wire-a",)
    assert [a.label for a in view.artifacts] == [
        "evidence_manifest", "canonical_dataset"]  # diagnostic labels only
    assert view.notes == ("No report JSON or HTML was published.",)


def test_rejected_error_defaults_and_evidence_gate(tmp_path: Path) -> None:
    result = FakeResult(ok=False)  # no error, no artifacts at all
    view = format_intake_result(result)
    assert view.error == "Unknown intake error"  # old 260
    assert view.evidence_error is None  # old 275: gated on artifacts
    assert view.artifacts == ()


# ── artifact re-parse defensiveness (old 365-414) ─────────────────────


def test_canonical_errors_are_named_not_raised(tmp_path: Path) -> None:
    assert canonical_records({})[1] == "canonical_dataset is unavailable"
    missing = {"canonical_dataset": tmp_path / "nope.json"}  # path, no file
    assert "nope.json" in (canonical_records(missing)[1] or "")
    bad = {"canonical_dataset": _write(tmp_path / "bad.json", "{oops")}
    assert "Expecting" in (canonical_records(bad)[1] or "")
    listed = {"canonical_dataset": _write(tmp_path / "list.json", "[1, 2]")}
    assert canonical_records(listed)[1] == (
        "canonical_dataset root is not an object")
    no_records = {"canonical_dataset": _write(
        tmp_path / "empty.json", '{"records": {}}')}
    assert canonical_records(no_records)[1] == (
        "canonical dataset records are unavailable")


def test_evidence_errors_are_named_not_raised(tmp_path: Path) -> None:
    missing = {"evidence_manifest": None}
    assert evidence_exceptions(missing)[1] == (
        "evidence_manifest is unavailable")
    bad = {"evidence_manifest": _write(tmp_path / "root.json", '"str"')}
    assert evidence_exceptions(bad)[1] == (
        "evidence_manifest root is not an object")
    no_captures = {"evidence_manifest": _write(tmp_path / "nc.json", "{}")}
    assert evidence_exceptions(no_captures)[1] == (
        "evidence manifest captures are unavailable")


def test_artifact_document_read_error(tmp_path: Path) -> None:
    artifacts = {"canonical_dataset": tmp_path / "gone.json"}
    _doc, error = artifact_document(artifacts, "canonical_dataset")
    assert "gone.json" in error  # OSError string names the path
