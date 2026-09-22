"""IntakeResultFormatter — daily-intake result parsing, out of the screen.

Extracted (copy + adapt, no ``tui.`` import) from DailyIntakeScreen's
``_format_result`` and its artifact helpers (tui/screens/daily_intake.py:
254-414): re-parses the intake session's canonical-dataset and
evidence-manifest artifacts and reshapes the verified ``IntakeResult``
into render-ready structures — the inventory filed this as
"report-presentation logic that belongs next to the intake service".

Adaptations:

- Input stays duck-typed (``result.ok / .envelope / .counts /
  .artifacts / .error``), exactly like the old static methods — the
  old-tree ``IntakeResult`` (tui/intake.py:143) and any test double
  satisfy it; no tui import needed to serve it.
- Output is a frozen ``IntakeReportView`` (rows, not one formatted
  string): the M5 screen renders the sections; the old prose lines
  survive as the ``notes`` tuple so no information is lost.
- The two artifact reads stay file-path-in / data-out: reading the
  canonical dataset and evidence manifest JSON is the re-parse this
  service exists for (bounded to paths the verified result carries).
- Defensive shapes are kept clause-for-clause (every ``isinstance``
  guard from daily_intake.py:255-414): malformed artifacts degrade to
  named error fields (``canonical_error`` / ``evidence_error``), never
  to an exception.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ArtifactLine",
    "CanonicalRecord",
    "EvidenceExceptions",
    "IntakeReportView",
    "SourceCounts",
    "artifact_document",
    "canonical_records",
    "evidence_exceptions",
    "format_intake_result",
]

#: Artifact labels, in the old display order (daily_intake.py:279, 350).
DIAGNOSTIC_ARTIFACTS = ("evidence_manifest", "canonical_dataset")
LINEAGE_ARTIFACTS = ("evidence_manifest", "canonical_dataset",
                     "report_json", "report_html")


@dataclass(frozen=True)
class SourceCounts:
    """The sources + records count line (old daily_intake.py:300-310).

    A missing count renders as ``"?"`` (the old literal), never an
    invented 0 — schema-legal unknowns stay visibly unknown.
    """

    succeeded: str = "?"
    expected: str = "?"
    failed: int = 0
    pruned: int = 0
    canonical_records: str = "?"


@dataclass(frozen=True)
class CanonicalRecord:
    """One canonical dataset record row (old daily_intake.py:342-343)."""

    index: int  # 1-based display rank
    market: str
    title: str


@dataclass(frozen=True)
class EvidenceExceptions:
    """Failed/pruned capture sources from the evidence manifest."""

    failed: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactLine:
    """One verified lineage row: label, path, sha256 prefix (16 hex)."""

    label: str
    path: str
    sha256_prefix: str = ""


@dataclass(frozen=True)
class IntakeReportView:
    """The render-ready intake result: ACCEPTED or REJECTED, one frame."""

    ok: bool
    status: str  # "ACCEPTED" | "REJECTED"
    error: str | None = None
    date: str = "?"
    quality_label: str = "UNKNOWN"
    quality_issues: tuple[str, ...] = ()
    counts: SourceCounts = field(default_factory=SourceCounts)
    #: Sorted (market, count) rows; empty + not ``market_counts_from_envelope``
    #: renders the old "(reported in the canonical dataset)" line.
    market_counts: tuple[tuple[str, Any], ...] = ()
    market_counts_from_envelope: bool = False
    session_markets: tuple[str, ...] = ()
    canonical_records: tuple[CanonicalRecord, ...] = ()
    canonical_error: str | None = None
    evidence: EvidenceExceptions = field(default_factory=EvidenceExceptions)
    evidence_error: str | None = None
    #: The verified-artifact rows the old report listed, in order.
    artifacts: tuple[ArtifactLine, ...] = ()
    #: Trailing prose lines (derivation chain, report-open hint, …).
    notes: tuple[str, ...] = ()


def _envelope_of(result: Any) -> dict[str, Any]:
    envelope = getattr(result, "envelope", None)
    return envelope if isinstance(envelope, dict) else {}


def _artifacts_of(result: Any) -> Mapping[str, Any]:
    artifacts = getattr(result, "artifacts", None)
    return artifacts if isinstance(artifacts, dict) else {}


def artifact_document(
    artifacts: Mapping[str, Any], key: str,
) -> tuple[dict[str, Any], str | None]:
    """Read + parse one artifact JSON; defensive (old daily_intake.py:365-376)."""
    path = artifacts.get(key)
    if path is None:
        return {}, f"{key} is unavailable"
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, str(exc)
    if not isinstance(document, dict):
        return {}, f"{key} root is not an object"
    return document, None


def canonical_records(
    artifacts: Mapping[str, Any],
) -> tuple[list[CanonicalRecord], str | None]:
    """Re-parse the canonical dataset into display rows (old 379-396)."""
    document, error = artifact_document(artifacts, "canonical_dataset")
    if error:
        return [], error
    raw_records = document.get("records")
    if not isinstance(raw_records, list):
        return [], "canonical dataset records are unavailable"
    records: list[CanonicalRecord] = []
    for record in raw_records:
        if not isinstance(record, dict):
            continue
        title = record.get("title")
        market = record.get("market")
        if (isinstance(title, str) and title.strip()
                and isinstance(market, str) and market):
            # Title exactly as stored; validation already established it
            # is a canonical source field (old daily_intake.py:393-394).
            records.append(CanonicalRecord(
                index=len(records) + 1, market=market, title=title))
    return records, None


def evidence_exceptions(
    artifacts: Mapping[str, Any],
) -> tuple[EvidenceExceptions, str | None]:
    """Re-parse the evidence manifest's failed/pruned sources (old 398-414)."""
    document, error = artifact_document(artifacts, "evidence_manifest")
    if error:
        return EvidenceExceptions(), error
    captures = document.get("captures")
    if not isinstance(captures, list):
        return EvidenceExceptions(), (
            "evidence manifest captures are unavailable")
    failed: list[str] = []
    pruned: list[str] = []
    for capture in captures:
        if not isinstance(capture, dict):
            continue
        status = capture.get("status")
        source = capture.get("source")
        if (status == "failed" and isinstance(source, str) and source):
            failed.append(source)
        elif (status == "pruned" and isinstance(source, str) and source):
            pruned.append(source)
    return EvidenceExceptions(failed=tuple(failed), pruned=tuple(pruned)), None


def _quality(envelope: Mapping[str, Any]) -> tuple[str, list[str]]:
    """(label, filtered issues) — old daily_intake.py:289-299, defensive."""
    quality = (envelope.get("quality")
               if isinstance(envelope.get("quality"), dict) else {})
    status = quality.get("status")
    label = (status.upper() if isinstance(status, str) and status.strip()
             else "UNKNOWN")
    raw_issues = quality.get("issues")
    issues = (raw_issues if isinstance(raw_issues, list) else [])
    return label, [i for i in issues if isinstance(i, str) and i]


def _lineage_artifacts(
    result: Any, labels: tuple[str, ...],
) -> tuple[ArtifactLine, ...]:
    """Artifact rows with sha256 prefixes from the envelope (old 349-356)."""
    envelope = _envelope_of(result)
    metadata = (envelope.get("artifacts")
                if isinstance(envelope.get("artifacts"), dict) else {})
    rows: list[ArtifactLine] = []
    for label in labels:
        artifact = _artifacts_of(result).get(label)
        if artifact is None:
            continue
        meta = metadata.get(label)
        digest = (str(meta.get("sha256") or "")
                  if isinstance(meta, dict) else "")
        rows.append(ArtifactLine(
            label=label, path=str(artifact),
            sha256_prefix=digest[:16] if digest else "",
        ))
    return tuple(rows)


def format_intake_result(result: Any) -> IntakeReportView:
    """Reshape one verified intake result; never raises (old 254-363)."""
    if not getattr(result, "ok", False):
        return _rejected_view(result)
    return _accepted_view(result)


def _rejected_view(result: Any) -> IntakeReportView:
    """The REJECTED frame (old daily_intake.py:256-284)."""
    envelope = _envelope_of(result)
    quality = (envelope.get("quality")
               if isinstance(envelope.get("quality"), dict) else {})
    issues = quality.get("issues") if isinstance(
        quality.get("issues"), list) else []
    session_markets = envelope.get("session_markets")
    artifacts = _artifacts_of(result)
    evidence, evidence_error = evidence_exceptions(artifacts)
    return IntakeReportView(
        ok=False,
        status="REJECTED",
        error=str(getattr(result, "error", None) or "Unknown intake error"),
        session_markets=tuple(
            str(v) for v in session_markets
        ) if isinstance(session_markets, list) and session_markets else (),
        quality_issues=tuple(
            i.strip() for i in issues if isinstance(i, str) and i.strip()),
        evidence=evidence,
        evidence_error=evidence_error if artifacts else None,
        artifacts=_lineage_artifacts(result, DIAGNOSTIC_ARTIFACTS),
        notes=(
            "No report JSON or HTML was published.",
        ),
    )


def _accepted_view(result: Any) -> IntakeReportView:
    """The ACCEPTED frame (old daily_intake.py:286-363)."""
    envelope = _envelope_of(result)
    counts = getattr(result, "counts", None)
    counts = counts if isinstance(counts, dict) else {}
    quality_label, quality_issues = _quality(envelope)
    market_counts = (envelope.get("market_counts")
                     if isinstance(envelope.get("market_counts"), dict)
                     else {})
    artifacts = _artifacts_of(result)
    records, canonical_error = canonical_records(artifacts)
    evidence, evidence_error = evidence_exceptions(artifacts)
    source_counts = SourceCounts(
        succeeded=str(counts.get(
            "succeeded", counts.get("successful", "?"))),
        expected=str(counts.get("expected", "?")),
        failed=int(counts.get("failed", 0) or 0),
        pruned=int(counts.get("pruned", 0) or 0),
        canonical_records=str(counts.get(
            "canonical_records", counts.get("records", "?"))),
    )
    # Old quality-issues fallback (daily_intake.py:314-319): PASSED with
    # no issues says "None reported."; a non-PASSED gate with no issue
    # descriptions says the metadata did not provide one.
    if not quality_issues:
        fallback = ("None reported." if quality_label == "PASSED"
                    else "Quality metadata did not provide an issue "
                         "description.")
        quality_issues = (fallback,)
    return IntakeReportView(
        ok=True,
        status="ACCEPTED",
        date=str(envelope.get("date", "?")),
        quality_label=quality_label,
        quality_issues=tuple(quality_issues),
        counts=source_counts,
        market_counts=tuple(sorted(market_counts.items())),
        market_counts_from_envelope=bool(market_counts),
        canonical_records=tuple(records),
        canonical_error=canonical_error,
        evidence=evidence,
        evidence_error=evidence_error,
        artifacts=_lineage_artifacts(result, LINEAGE_ARTIFACTS),
        notes=(
            "Report JSON was derived from the canonical dataset.",
            "Report HTML was derived from that report JSON.",
        ),
    )
