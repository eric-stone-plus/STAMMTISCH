"""Offline tests for the fail-closed daily intake subprocess contract."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tui.intake import IntakeDriver


DATE = "20260814"
RUN_ID = "20260814T090000.000000Z-a1b2c3d4"
URL = "https://example.com/markets/rates"
SOURCE_TITLE = "\u6765\u6e90\u6807\u9898"
SOURCE_SUMMARY = "\u6765\u6e90\u6458\u8981"
REPORT_SUMMARY = "\u65e5\u62a5\u7f16\u8f91\u6458\u8981"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _write(path: Path, payload: bytes) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


class WorkspaceFixture:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.run = self.root / "runs" / RUN_ID
        self.evidence = self.root / "evidence" / "us" / "example" / "capture.json"
        self.manifest = self.run / "evidence-manifest.json"
        self.canonical = self.run / "canonical-dataset.json"
        self.report = self.run / f"fin-daily-{DATE}.json"
        self.html = self.run / f"fin-daily-{DATE}.html"
        self.envelope: dict[str, object] = {}
        self.build()

    def build(self) -> None:
        evidence_meta = _write(
            self.evidence,
            _json_bytes(
                {
                    "success": True,
                    "data": {
                        "markdown": f"[{SOURCE_TITLE}]({URL})",
                        "links": [URL],
                    },
                }
            ),
        )
        relative_evidence = self.evidence.relative_to(self.root).as_posix()
        manifest_document = {
            "schema": "stammtisch.daily-intake-evidence.v1",
            "revision": 1,
            "append_only": True,
            "date": DATE,
            "run_id": RUN_ID,
            "captured_at": "2026-08-14T09:00:00Z",
            "mode": "live",
            "captures": [
                {
                    "source": "example",
                    "url": "https://example.com/",
                    "status": "succeeded",
                    "market": "us",
                    "phase": "overseas",
                    "path": relative_evidence,
                    "sha256": evidence_meta["sha256"],
                    "bytes": evidence_meta["bytes"],
                },
                {
                    "source": "unavailable",
                    "url": "https://unavailable.example/",
                    "status": "failed",
                    "market": "us",
                    "phase": "overseas",
                },
                {
                    "source": "retired",
                    "url": "https://retired.example/",
                    "status": "pruned",
                    "market": "us",
                    "phase": "overseas",
                },
            ],
            "counts": {"expected": 3, "succeeded": 1, "failed": 1, "pruned": 1},
            "raw_attempts": [],
        }
        manifest_meta = _write(self.manifest, _json_bytes(manifest_document))
        evidence_ref = {
            "source": "example",
            "path": relative_evidence,
            "sha256": evidence_meta["sha256"],
        }
        canonical_document = {
            "schema": "stammtisch.daily-dataset.v1",
            "revision": 1,
            "date": DATE,
            "run_id": RUN_ID,
            "captured_at": "2026-08-14T09:00:00Z",
            "mode": "live",
            "lineage": {
                "evidence_manifest": {
                    "input": "evidence_manifest",
                    "input_sha256": manifest_meta["sha256"],
                }
            },
            "records": [
                {
                    "id": "record-1",
                    "market": "us",
                    "source": "example",
                    "source_label": "Example",
                    "source_language": "zh",
                    "title": SOURCE_TITLE,
                    "summary": SOURCE_SUMMARY,
                    "url": URL,
                    "sources": ["example"],
                    "source_labels": ["Example"],
                    "evidence_refs": [evidence_ref],
                }
            ],
            "brief": [{"text": REPORT_SUMMARY, "sources": ["Example"]}],
            "notes": [],
            "source_counts": {"example": 1},
            "market_counts": {"us": 1},
            "evidence_counts": {"expected": 3, "succeeded": 1, "failed": 1, "pruned": 1},
        }
        canonical_meta = _write(self.canonical, _json_bytes(canonical_document))
        report_document = {
            "schema": "stammtisch.daily-report.v1",
            "revision": 1,
            "date": DATE,
            "run_id": RUN_ID,
            "captured_at": "2026-08-14T09:00:00Z",
            "lineage": {
                "canonical_dataset": {
                    "input": "canonical_dataset",
                    "input_sha256": canonical_meta["sha256"],
                }
            },
            "brief": [{"text": REPORT_SUMMARY, "sources": ["Example"]}],
            "markets": {
                "ashare": [],
                "hk": [],
                "us": [
                    {
                        "id": "record-1",
                        "source": "example",
                        "source_label": "Example",
                        "source_language": "zh",
                        "title": SOURCE_TITLE,
                        "summary": REPORT_SUMMARY,
                        "url": URL,
                        "sources": ["example"],
                        "source_labels": ["Example"],
                        "evidence_refs": [evidence_ref],
                    }
                ],
                "crypto": [],
            },
            "notes": [],
            "source_counts": {"example": 1},
            "market_counts": {"us": 1},
            "intake": {"expected": 3, "succeeded": 1, "failed": 1, "pruned": 1},
        }
        report_meta = _write(self.report, _json_bytes(report_document))
        html_meta = _write(
            self.html,
            (
                "<!doctype html><html><head>"
                '<meta name="stammtisch-input-sha256" '
                f'content="{report_meta["sha256"]}">'
                f"</head><body>{REPORT_SUMMARY}</body></html>\n"
            ).encode(),
        )
        self.envelope = {
            "schema": "stammtisch.daily-intake.v1",
            "revision": 1,
            "ok": True,
            "date": DATE,
            "run_id": RUN_ID,
            "workspace_root": str(self.root),
            "workspace_path": str(self.root),
            "mode": "live",
            "artifacts": {
                "evidence_manifest": manifest_meta,
                "canonical_dataset": canonical_meta,
                "report_json": report_meta,
                "report_html": html_meta,
            },
            "counts": {
                "expected": 3,
                "succeeded": 1,
                "failed": 1,
                "pruned": 1,
                "canonical_records": 1,
                "sources": 1,
                "markets": 1,
            },
            "source_counts": {"example": 1},
            "market_counts": {"us": 1},
            "quality": {"status": "passed"},
            "lineage": {
                "report_json": {
                    "input": "canonical_dataset",
                    "input_sha256": canonical_meta["sha256"],
                },
                "report_html": {
                    "input": "report_json",
                    "input_sha256": report_meta["sha256"],
                },
            },
        }

    def rewrite_json(self, path: Path, mutate) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document)
        metadata = _write(path, _json_bytes(document))
        artifact_key = {
            self.manifest: "evidence_manifest",
            self.canonical: "canonical_dataset",
            self.report: "report_json",
        }[path]
        self.envelope["artifacts"][artifact_key] = metadata  # type: ignore[index]

    def completed(self, *, stdout: bytes | None = None, returncode: int = 0) -> dict:
        payload = stdout if stdout is not None else _json_bytes(self.envelope)
        return {"ok": True, "error": "", "stdout": payload,
                "stderr": b"", "returncode": returncode}

    def attach_intraday_quote_baseline(self) -> None:
        provider_time = dt.datetime(2026, 8, 14, 18, 0, tzinfo=dt.timezone.utc)
        captured_at = "2026-08-14T18:30:00Z"
        response = self.root / "captures" / "market-quotes-us-20260814" / "spy.json"
        evidence = _write(
            response,
            _json_bytes(
                {
                    "c": 645.2,
                    "pc": 642.1,
                    "o": 643.0,
                    "h": 646.0,
                    "l": 641.5,
                    "t": int(provider_time.timestamp()),
                }
            ),
        )
        evidence["path"] = response.relative_to(self.root).as_posix()
        baseline = {
            "schema": "stammtisch.market-quotes.v1",
            "provider": "finnhub",
            "market": "us",
            "purpose": "latest_completed_session_cross_check",
            "non_blocking": True,
            "instrument_type": "ETF proxy",
            "official_index_close": False,
            "exchange_timezone": "America/New_York",
            "captured_at": captured_at,
            "proxy": "disabled",
            "status": "not_completed_session",
            "quotes": [
                {
                    "provider": "finnhub",
                    "symbol": "SPY",
                    "instrument_type": "ETF proxy",
                    "tracks": "S&P 500 exposure",
                    "session_date": "2026-08-14",
                    "exchange_timezone": "America/New_York",
                    "open": 643.0,
                    "high": 646.0,
                    "low": 641.5,
                    "current": 645.2,
                    "previous_close": 642.1,
                    "captured_at": captured_at,
                    "provider_timestamp": "2026-08-14T18:00:00Z",
                    "delay_seconds": 1800,
                    "delay_status": "delayed",
                    "proxy": "disabled",
                    "status": "not_completed_session",
                    "evidence_ref": copy.deepcopy(evidence),
                }
            ],
            "attempts": [
                {
                    "provider": "finnhub",
                    "symbol": "SPY",
                    "captured_at": captured_at,
                    "http_status": "200",
                    "proxy": "disabled",
                    "status": "not_completed_session",
                    "evidence_ref": copy.deepcopy(evidence),
                }
            ],
        }
        self.sync_quote_baseline(baseline)

    def sync_quote_baseline(self, baseline: dict[str, object]) -> None:
        def update_manifest(document):
            document["market_quote_baseline"] = copy.deepcopy(baseline)

        self.rewrite_json(self.manifest, update_manifest)
        manifest_digest = self.envelope["artifacts"]["evidence_manifest"]["sha256"]

        def update_canonical(document):
            document["market_quote_baseline"] = copy.deepcopy(baseline)
            document["lineage"]["evidence_manifest"]["input_sha256"] = manifest_digest

        self.rewrite_json(self.canonical, update_canonical)
        canonical_digest = self.envelope["artifacts"]["canonical_dataset"]["sha256"]

        def update_report(document):
            document["market_quote_baseline"] = copy.deepcopy(baseline)
            document["lineage"]["canonical_dataset"]["input_sha256"] = canonical_digest

        self.rewrite_json(self.report, update_report)
        report_digest = self.envelope["artifacts"]["report_json"]["sha256"]
        self.envelope["artifacts"]["report_html"] = _write(
            self.html,
            (
                "<!doctype html><meta name=\"stammtisch-input-sha256\" "
                f"content=\"{report_digest}\">"
            ).encode(),
        )
        self.envelope["market_quote_baseline"] = copy.deepcopy(baseline)
        self.envelope["lineage"]["report_json"]["input_sha256"] = canonical_digest
        self.envelope["lineage"]["report_html"]["input_sha256"] = report_digest

    @staticmethod
    def us_post_close_session(
        *, readiness: str = "complete"
    ) -> dict[str, object]:
        previous = {
            "session_date": "2026-08-14",
            "open": "2026-08-14T09:30:00-04:00",
            "close": "2026-08-14T16:00:00-04:00",
            "open_utc": "2026-08-14T13:30:00Z",
            "close_utc": "2026-08-14T20:00:00Z",
        }
        next_session = {
            "session_date": "2026-08-17",
            "open": "2026-08-17T09:30:00-04:00",
            "close": "2026-08-17T16:00:00-04:00",
            "open_utc": "2026-08-17T13:30:00Z",
            "close_utc": "2026-08-17T20:00:00Z",
        }
        return {
            "schema": "stammtisch.market-calendar.v1",
            "revision": 1,
            "market": "us",
            "captured_at": "2026-08-14T20:15:00Z",
            "calendar_status": "available",
            "calendar_provider": "exchange_calendars",
            "calendar_version": "4.13.2",
            "calendar_id": "XNYS",
            "calendar_source": {
                "provider": "exchange_calendars",
                "version": "4.13.2",
                "calendar_id": "XNYS",
                "status": "available",
            },
            "reason": None,
            "error_type": None,
            "exchange_timezone": "America/New_York",
            "as_of": "2026-08-14T16:15:00-04:00",
            "session_date": "2026-08-14",
            "session_open": previous["open"],
            "session_close": previous["close"],
            "session_open_utc": previous["open_utc"],
            "session_close_utc": previous["close_utc"],
            "cutoff_type": "post_close",
            "calendar_completeness": "complete",
            "is_open": False,
            "has_session_on_capture_date": True,
            "previous_session": previous,
            "next_session": next_session,
            "completeness": readiness,
        }

    @staticmethod
    def us_unavailable_session() -> dict[str, object]:
        session = {
            "schema": "stammtisch.market-calendar.v1",
            "revision": 1,
            "market": "us",
            "captured_at": "2026-08-14T20:15:00Z",
            "calendar_status": "unavailable",
            "calendar_provider": "exchange_calendars",
            "calendar_version": None,
            "calendar_id": "XNYS",
            "calendar_source": {
                "provider": "exchange_calendars",
                "version": None,
                "calendar_id": "XNYS",
                "status": "unavailable",
            },
            "reason": "provider_not_installed",
            "error_type": "ModuleNotFoundError",
            "exchange_timezone": None,
            "as_of": None,
            "session_date": None,
            "session_open": None,
            "session_close": None,
            "session_open_utc": None,
            "session_close_utc": None,
            "cutoff_type": "calendar_unavailable",
            "calendar_completeness": "partial",
            "is_open": None,
            "has_session_on_capture_date": None,
            "previous_session": None,
            "next_session": None,
            "completeness": "partial",
        }
        return session

    def sync_single_session(
        self,
        session: dict[str, object],
        *,
        quality_status: str = "passed",
    ) -> None:
        quality = {
            "status": quality_status,
            "complete": quality_status == "passed",
            "scope": "market_session",
            "markets": [session["market"]],
            "issues": [],
            "session": copy.deepcopy(session),
        }
        quality.update(copy.deepcopy(session))
        self.envelope["mode"] = "firecrawl"
        self.envelope["session_markets"] = [session["market"]]
        self.envelope["session"] = copy.deepcopy(session)
        self.envelope["quality"] = quality

        def update_manifest(document):
            document["captured_at"] = session["captured_at"]
            document["mode"] = "firecrawl"
            document["session"] = copy.deepcopy(session)
            for item in document["captures"] + document["raw_attempts"]:
                item.update(copy.deepcopy(session))

        self.rewrite_json(self.manifest, update_manifest)
        manifest_digest = self.envelope["artifacts"]["evidence_manifest"]["sha256"]

        def update_canonical(document):
            document["captured_at"] = session["captured_at"]
            document["mode"] = "firecrawl"
            document["session"] = copy.deepcopy(session)
            document["session_quality"] = copy.deepcopy(quality)
            document["lineage"]["evidence_manifest"]["input_sha256"] = manifest_digest

        self.rewrite_json(self.canonical, update_canonical)
        canonical_digest = self.envelope["artifacts"]["canonical_dataset"]["sha256"]

        def update_report(document):
            document["captured_at"] = session["captured_at"]
            document["session"] = copy.deepcopy(session)
            document["session_quality"] = copy.deepcopy(quality)
            document["lineage"]["canonical_dataset"]["input_sha256"] = canonical_digest

        self.rewrite_json(self.report, update_report)
        report_digest = self.envelope["artifacts"]["report_json"]["sha256"]
        self.envelope["artifacts"]["report_html"] = _write(
            self.html,
            (
                "<!doctype html><meta name=\"stammtisch-input-sha256\" "
                f"content=\"{report_digest}\">"
            ).encode(),
        )
        self.envelope["lineage"]["report_json"]["input_sha256"] = canonical_digest
        self.envelope["lineage"]["report_html"]["input_sha256"] = report_digest

    def sync_assembled_session(self, session: dict[str, object]) -> None:
        child_quality = {
            "status": "passed",
            "complete": True,
            "scope": "market_session",
            "markets": [session["market"]],
            "issues": [],
            "session": copy.deepcopy(session),
        }
        child_quality.update(copy.deepcopy(session))
        child_result = {
            "market": session["market"],
            "accepted": True,
            "run_id": "child-us",
            "quality": child_quality,
            "counts": copy.deepcopy(self.envelope["counts"]),
            "canonical_sha256": "1" * 64,
            "session": copy.deepcopy(session),
        }
        market_sessions = {str(session["market"]): copy.deepcopy(session)}
        quality = {
            "status": "passed",
            "complete": True,
            "scope": "market_session_assembly",
            "markets": [session["market"]],
            "accepted_markets": [session["market"]],
            "rejected_markets": [],
            "issues": [],
            "sessions": [copy.deepcopy(child_result)],
            "market_sessions": copy.deepcopy(market_sessions),
        }
        self.envelope["mode"] = "market_session_assembly"
        self.envelope["session_markets"] = [session["market"]]
        self.envelope["market_sessions"] = copy.deepcopy(market_sessions)
        self.envelope["quality"] = quality

        def update_manifest(document):
            document["mode"] = "market_session_assembly"
            document["sessions"] = [copy.deepcopy(child_result)]
            document["market_sessions"] = copy.deepcopy(market_sessions)
            for item in document["captures"] + document["raw_attempts"]:
                item.update(copy.deepcopy(session))

        self.rewrite_json(self.manifest, update_manifest)
        manifest_digest = self.envelope["artifacts"]["evidence_manifest"]["sha256"]

        def update_canonical(document):
            document["mode"] = "market_session_assembly"
            document["market_sessions"] = copy.deepcopy(market_sessions)
            document["session_quality"] = copy.deepcopy(quality)
            document["lineage"]["evidence_manifest"]["input_sha256"] = manifest_digest

        self.rewrite_json(self.canonical, update_canonical)
        canonical_digest = self.envelope["artifacts"]["canonical_dataset"]["sha256"]

        def update_report(document):
            document["market_sessions"] = copy.deepcopy(market_sessions)
            document["session_quality"] = copy.deepcopy(quality)
            document["lineage"]["canonical_dataset"]["input_sha256"] = canonical_digest

        self.rewrite_json(self.report, update_report)
        report_digest = self.envelope["artifacts"]["report_json"]["sha256"]
        self.envelope["artifacts"]["report_html"] = _write(
            self.html,
            (
                "<!doctype html><meta name=\"stammtisch-input-sha256\" "
                f"content=\"{report_digest}\">"
            ).encode(),
        )
        self.envelope["lineage"]["report_json"]["input_sha256"] = canonical_digest
        self.envelope["lineage"]["report_html"]["input_sha256"] = report_digest


class IntakeDriverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = WorkspaceFixture(self.root)
        self.driver = IntakeDriver(["intake-program", "capture"], self.root, timeout_seconds=12)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_completed(self, completed: dict, date: str | None = DATE):
        with mock.patch("tui.intake.run_bounded", return_value=completed) as invoked:
            result = self.driver.run(date)
        return result, invoked

    def test_accepts_verified_workspace_and_preserves_artifact_bytes(self) -> None:
        original = self.fixture.canonical.read_bytes()
        result, invoked = self.run_completed(self.fixture.completed())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.date, DATE)
        self.assertEqual(set(result.artifacts), {
            "evidence_manifest", "canonical_dataset", "report_json", "report_html"
        })
        self.assertEqual(result.artifacts["canonical_dataset"], self.fixture.canonical.resolve())
        self.assertEqual(result.counts["canonical_records"], 1)
        self.assertEqual(self.fixture.canonical.read_bytes(), original)
        argv = invoked.call_args.args[0]
        self.assertEqual(argv[:2], ["intake-program", "capture"])
        self.assertEqual(argv[-2:], ["--date", DATE])
        self.assertIn("--workspace-root", argv)
        self.assertIn("--json", argv)
        self.assertEqual(invoked.call_args.args[1], 12)
        self.assertIs(invoked.call_args.kwargs["text"], False)

    def test_date_is_optional_and_invalid_date_never_launches(self) -> None:
        result, invoked = self.run_completed(self.fixture.completed(), None)
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("--date", invoked.call_args.args[0])
        with mock.patch("tui.intake.run_bounded") as never:
            invalid = self.driver.run("20260230")
        self.assertFalse(invalid.ok)
        self.assertEqual(invalid.returncode, 3)
        never.assert_not_called()

    def test_constructor_requires_tokenized_argv_and_bounded_timeout(self) -> None:
        with self.assertRaises(ValueError):
            IntakeDriver("intake-program --unsafe", self.root)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            IntakeDriver(["intake-program", "--workspace-root", "/tmp/x"], self.root)
        with self.assertRaises(ValueError):
            IntakeDriver(["intake-program"], self.root, timeout_seconds=3601)

    def test_nonzero_timeout_and_bad_stdout_fail_closed(self) -> None:
        nonzero, _ = self.run_completed(
            {"ok": True, "error": "", "stdout": b"{}", "stderr": b"capture failed", "returncode": 7}
        )
        self.assertFalse(nonzero.ok)
        self.assertEqual(nonzero.returncode, 7)
        self.assertIn("capture failed", nonzero.error or "")

        with mock.patch(
            "tui.intake.run_bounded",
            return_value={"ok": False, "error": "daily intake command timed out after 12s",
                          "stdout": b"", "stderr": b"", "returncode": None},
        ):
            timed_out = self.driver.run(DATE)
        self.assertFalse(timed_out.ok)
        self.assertEqual(timed_out.returncode, 124)

        for payload in (b"not json", b"{}\n{}\n", b"[]", b'{"ok":true,"ok":true}'):
            with self.subTest(payload=payload):
                bad, _ = self.run_completed(self.fixture.completed(stdout=payload))
                self.assertFalse(bad.ok)
                self.assertEqual(bad.returncode, 2)

    def test_nonzero_quality_envelope_preserves_diagnostics_but_never_reports(self) -> None:
        envelope = copy.deepcopy(self.fixture.envelope)
        envelope["ok"] = False
        envelope["session_markets"] = ["us"]
        envelope["quality"] = {
            "status": "failed",
            "scope": "market_session",
            "markets": ["us"],
            "complete": False,
            "issues": ["selected market session produced no canonical records: us"],
        }
        del envelope["artifacts"]["report_json"]
        del envelope["artifacts"]["report_html"]
        envelope["lineage"] = {}

        rejected, _ = self.run_completed(
            {"ok": True, "error": "", "stdout": _json_bytes(envelope),
             "stderr": b"quality gate rejected", "returncode": 1}
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(1, rejected.returncode)
        self.assertEqual(
            {"evidence_manifest", "canonical_dataset"}, set(rejected.artifacts)
        )
        self.assertIn("no canonical records", rejected.error or "")
        self.assertNotIn("report_json", rejected.artifacts)

    def test_rejected_envelope_cannot_smuggle_report_artifacts(self) -> None:
        envelope = copy.deepcopy(self.fixture.envelope)
        envelope["ok"] = False
        envelope["quality"] = {"status": "failed", "issues": ["rejected"]}
        rejected, _ = self.run_completed(
            {"ok": True, "error": "", "stdout": _json_bytes(envelope),
             "stderr": b"", "returncode": 1}
        )
        self.assertFalse(rejected.ok)
        self.assertEqual({}, rejected.artifacts)
        self.assertIn("exited with status 1", rejected.error or "")

    def test_unknown_schema_revision_or_false_success_is_rejected(self) -> None:
        for key, value in (("schema", "stammtisch.daily-intake.v2"), ("revision", 2), ("ok", False)):
            with self.subTest(key=key):
                envelope = copy.deepcopy(self.fixture.envelope)
                envelope[key] = value
                bad, _ = self.run_completed(self.fixture.completed(stdout=_json_bytes(envelope)))
                self.assertFalse(bad.ok)

    def test_success_envelope_requires_an_accepted_quality_status(self) -> None:
        for status in ("failed", "rejected", "pending", "complete", "", None):
            with self.subTest(status=status):
                envelope = copy.deepcopy(self.fixture.envelope)
                envelope["quality"]["status"] = status
                bad, _ = self.run_completed(
                    self.fixture.completed(stdout=_json_bytes(envelope))
                )
                self.assertFalse(bad.ok)
                self.assertEqual(2, bad.returncode)
                self.assertIn("quality status is not accepted", bad.error or "")

    def test_success_envelope_requires_quality_metadata(self) -> None:
        envelope = copy.deepcopy(self.fixture.envelope)
        del envelope["quality"]
        bad, _ = self.run_completed(
            self.fixture.completed(stdout=_json_bytes(envelope))
        )
        self.assertFalse(bad.ok)
        self.assertEqual(2, bad.returncode)
        self.assertIn("envelope quality must be an object", bad.error or "")

    def test_missing_artifact_and_path_escape_are_rejected(self) -> None:
        missing = copy.deepcopy(self.fixture.envelope)
        del missing["artifacts"]["canonical_dataset"]
        bad, _ = self.run_completed(self.fixture.completed(stdout=_json_bytes(missing)))
        self.assertFalse(bad.ok)
        self.assertIn("missing artifacts", bad.error or "")

        outside = self.root.parent / f"{self.root.name}-outside.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            escaped = copy.deepcopy(self.fixture.envelope)
            payload = outside.read_bytes()
            escaped["artifacts"]["report_json"] = {
                "path": str(outside),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
            bad, _ = self.run_completed(self.fixture.completed(stdout=_json_bytes(escaped)))
        finally:
            outside.unlink()
        self.assertFalse(bad.ok)
        self.assertIn("escapes the workspace", bad.error or "")

    def test_artifact_digest_and_byte_count_are_verified(self) -> None:
        for field, value in (("sha256", "0" * 64), ("bytes", 1)):
            with self.subTest(field=field):
                envelope = copy.deepcopy(self.fixture.envelope)
                envelope["artifacts"]["report_html"][field] = value
                bad, _ = self.run_completed(self.fixture.completed(stdout=_json_bytes(envelope)))
                self.assertFalse(bad.ok)

    def test_manifest_must_be_append_only_and_counts_must_match(self) -> None:
        self.fixture.rewrite_json(self.fixture.manifest, lambda doc: doc.update(append_only=False))
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("append_only", bad.error or "")

        self.fixture = WorkspaceFixture(self.root)
        self.fixture.envelope["counts"]["expected"] = 4
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("counts", bad.error or "")

    def test_canonical_record_requires_url_in_bound_evidence(self) -> None:
        payload = _json_bytes({"success": True, "data": {"markdown": "no link"}})
        self.fixture.evidence.write_bytes(payload)
        evidence_digest = hashlib.sha256(payload).hexdigest()
        evidence_bytes = len(payload)

        def mutate_manifest(doc):
            doc["captures"][0]["sha256"] = evidence_digest
            doc["captures"][0]["bytes"] = evidence_bytes

        self.fixture.rewrite_json(self.fixture.manifest, mutate_manifest)
        manifest_digest = self.fixture.envelope["artifacts"]["evidence_manifest"]["sha256"]

        def mutate_canonical(doc):
            doc["lineage"]["evidence_manifest"]["input_sha256"] = manifest_digest
            doc["records"][0]["evidence_refs"][0]["sha256"] = evidence_digest

        self.fixture.rewrite_json(self.fixture.canonical, mutate_canonical)
        canonical_digest = self.fixture.envelope["artifacts"]["canonical_dataset"]["sha256"]
        self.fixture.envelope["lineage"]["report_json"]["input_sha256"] = canonical_digest
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("URL is absent", bad.error or "")

    def test_report_cannot_change_canonical_source_identity(self) -> None:
        self.fixture.rewrite_json(
            self.fixture.report,
            lambda doc: doc["markets"]["us"][0].update(title="Invented title"),
        )
        report_digest = self.fixture.envelope["artifacts"]["report_json"]["sha256"]
        self.fixture.envelope["lineage"]["report_html"]["input_sha256"] = report_digest
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("source identities", bad.error or "")

    def test_lineage_and_html_digest_binding_are_verified(self) -> None:
        envelope = copy.deepcopy(self.fixture.envelope)
        envelope["lineage"]["report_json"]["input_sha256"] = "0" * 64
        bad, _ = self.run_completed(self.fixture.completed(stdout=_json_bytes(envelope)))
        self.assertFalse(bad.ok)
        self.assertIn("lineage digest", bad.error or "")

        wrong_html = (
            "<!doctype html><meta name=\"stammtisch-input-sha256\" "
            f"content=\"{'0' * 64}\">"
        ).encode()
        self.fixture.envelope["artifacts"]["report_html"] = _write(self.fixture.html, wrong_html)
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("report HTML", bad.error or "")

    def test_single_market_calendar_metadata_is_verified_across_every_layer(self) -> None:
        self.fixture.sync_single_session(self.fixture.us_post_close_session())

        result, _ = self.run_completed(self.fixture.completed())

        self.assertTrue(result.ok, result.error)
        self.assertEqual("XNYS", result.envelope["session"]["calendar_id"])

    def test_calendar_layer_drift_is_rejected(self) -> None:
        for layer in ("manifest", "canonical", "report"):
            with self.subTest(layer=layer):
                self.fixture = WorkspaceFixture(self.root)
                self.fixture.sync_single_session(self.fixture.us_post_close_session())
                path = getattr(self.fixture, layer)
                self.fixture.rewrite_json(
                    path,
                    lambda document: document["session"].update(
                        calendar_version="4.13.1"
                    ),
                )
                if layer == "manifest":
                    manifest_digest = self.fixture.envelope["artifacts"][
                        "evidence_manifest"
                    ]["sha256"]
                    self.fixture.rewrite_json(
                        self.fixture.canonical,
                        lambda document: document["lineage"][
                            "evidence_manifest"
                        ].update(input_sha256=manifest_digest),
                    )
                    canonical_digest = self.fixture.envelope["artifacts"][
                        "canonical_dataset"
                    ]["sha256"]
                    self.fixture.rewrite_json(
                        self.fixture.report,
                        lambda document: document["lineage"][
                            "canonical_dataset"
                        ].update(input_sha256=canonical_digest),
                    )
                elif layer == "canonical":
                    canonical_digest = self.fixture.envelope["artifacts"][
                        "canonical_dataset"
                    ]["sha256"]
                    self.fixture.rewrite_json(
                        self.fixture.report,
                        lambda document: document["lineage"][
                            "canonical_dataset"
                        ].update(input_sha256=canonical_digest),
                    )
                report_digest = self.fixture.envelope["artifacts"]["report_json"][
                    "sha256"
                ]
                self.fixture.envelope["artifacts"]["report_html"] = _write(
                    self.fixture.html,
                    (
                        "<!doctype html><meta name=\"stammtisch-input-sha256\" "
                        f"content=\"{report_digest}\">"
                    ).encode(),
                )
                self.fixture.envelope["lineage"]["report_json"][
                    "input_sha256"
                ] = self.fixture.envelope["artifacts"]["canonical_dataset"][
                    "sha256"
                ]
                self.fixture.envelope["lineage"]["report_html"][
                    "input_sha256"
                ] = report_digest

                bad, _ = self.run_completed(self.fixture.completed())

                self.assertFalse(bad.ok)
                self.assertIn("calendar metadata differs", bad.error or "")

    def test_capture_calendar_metadata_cannot_drift_from_its_session(self) -> None:
        self.fixture.sync_single_session(self.fixture.us_post_close_session())
        self.fixture.rewrite_json(
            self.fixture.manifest,
            lambda document: document["captures"][0].update(
                cutoff_type="intraday"
            ),
        )
        manifest_digest = self.fixture.envelope["artifacts"]["evidence_manifest"][
            "sha256"
        ]
        self.fixture.rewrite_json(
            self.fixture.canonical,
            lambda document: document["lineage"]["evidence_manifest"].update(
                input_sha256=manifest_digest
            ),
        )
        canonical_digest = self.fixture.envelope["artifacts"]["canonical_dataset"][
            "sha256"
        ]
        self.fixture.rewrite_json(
            self.fixture.report,
            lambda document: document["lineage"]["canonical_dataset"].update(
                input_sha256=canonical_digest
            ),
        )
        report_digest = self.fixture.envelope["artifacts"]["report_json"]["sha256"]
        self.fixture.envelope["artifacts"]["report_html"] = _write(
            self.fixture.html,
            (
                "<!doctype html><meta name=\"stammtisch-input-sha256\" "
                f"content=\"{report_digest}\">"
            ).encode(),
        )
        self.fixture.envelope["lineage"]["report_json"][
            "input_sha256"
        ] = canonical_digest
        self.fixture.envelope["lineage"]["report_html"][
            "input_sha256"
        ] = report_digest

        bad, _ = self.run_completed(self.fixture.completed())

        self.assertFalse(bad.ok)
        self.assertIn("calendar metadata differs", bad.error or "")

    def test_unavailable_calendar_is_partial_and_cannot_claim_session_data(self) -> None:
        unavailable = self.fixture.us_unavailable_session()
        self.fixture.sync_single_session(
            unavailable,
            quality_status="degraded",
        )
        accepted, _ = self.run_completed(self.fixture.completed())
        self.assertTrue(accepted.ok, accepted.error)

        leaked = copy.deepcopy(unavailable)
        leaked["session_date"] = "2026-08-14"
        self.fixture = WorkspaceFixture(self.root)
        self.fixture.sync_single_session(leaked, quality_status="degraded")
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("uncertified session data", bad.error or "")

    def test_calendar_status_and_readiness_conflicts_fail_closed(self) -> None:
        session = self.fixture.us_post_close_session(readiness="partial")
        self.fixture.sync_single_session(session, quality_status="passed")
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("completeness conflicts", bad.error or "")

        self.fixture = WorkspaceFixture(self.root)
        partial = self.fixture.us_unavailable_session()
        partial["completeness"] = "complete"
        self.fixture.sync_single_session(partial, quality_status="passed")
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("cannot pass with a partial calendar verdict", bad.error or "")

    def test_unknown_calendar_version_or_status_is_rejected_even_without_drift(self) -> None:
        for field, value, message in (
            ("calendar_version", "4.13.1", "unsupported for revision 1"),
            ("calendar_status", "stale", "calendar_status is invalid"),
        ):
            with self.subTest(field=field):
                self.fixture = WorkspaceFixture(self.root)
                session = self.fixture.us_post_close_session()
                session[field] = value
                source_field = (
                    "version" if field == "calendar_version" else "status"
                )
                session["calendar_source"][source_field] = value
                self.fixture.sync_single_session(session)

                bad, _ = self.run_completed(self.fixture.completed())

                self.assertFalse(bad.ok)
                self.assertIn(message, bad.error or "")

    def test_assembled_market_sessions_bind_child_and_parent_layers(self) -> None:
        session = self.fixture.us_post_close_session()
        self.fixture.sync_assembled_session(session)
        accepted, _ = self.run_completed(self.fixture.completed())
        self.assertTrue(accepted.ok, accepted.error)

        self.fixture.rewrite_json(
            self.fixture.manifest,
            lambda document: document["sessions"][0]["session"].update(
                calendar_id="XNAS"
            ),
        )
        manifest_digest = self.fixture.envelope["artifacts"]["evidence_manifest"][
            "sha256"
        ]
        self.fixture.rewrite_json(
            self.fixture.canonical,
            lambda document: document["lineage"]["evidence_manifest"].update(
                input_sha256=manifest_digest
            ),
        )
        canonical_digest = self.fixture.envelope["artifacts"]["canonical_dataset"][
            "sha256"
        ]
        self.fixture.rewrite_json(
            self.fixture.report,
            lambda document: document["lineage"]["canonical_dataset"].update(
                input_sha256=canonical_digest
            ),
        )
        report_digest = self.fixture.envelope["artifacts"]["report_json"]["sha256"]
        self.fixture.envelope["artifacts"]["report_html"] = _write(
            self.fixture.html,
            (
                "<!doctype html><meta name=\"stammtisch-input-sha256\" "
                f"content=\"{report_digest}\">"
            ).encode(),
        )
        self.fixture.envelope["lineage"]["report_json"][
            "input_sha256"
        ] = canonical_digest
        self.fixture.envelope["lineage"]["report_html"][
            "input_sha256"
        ] = report_digest

        bad, _ = self.run_completed(self.fixture.completed())

        self.assertFalse(bad.ok)
        self.assertIn("calendar_id is invalid", bad.error or "")

    def test_quote_baseline_verifies_nested_raw_evidence(self) -> None:
        self.fixture.attach_intraday_quote_baseline()
        result, _ = self.run_completed(self.fixture.completed())
        self.assertTrue(result.ok, result.error)

        baseline = copy.deepcopy(self.fixture.envelope["market_quote_baseline"])
        baseline["quotes"][0]["evidence_ref"]["sha256"] = "0" * 64
        baseline["attempts"][0]["evidence_ref"]["sha256"] = "0" * 64
        self.fixture.sync_quote_baseline(baseline)
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("SHA-256", bad.error or "")

    def test_quote_evidence_cannot_escape_workspace_or_expose_a_key(self) -> None:
        self.fixture.attach_intraday_quote_baseline()
        baseline = copy.deepcopy(self.fixture.envelope["market_quote_baseline"])
        baseline["api_key"] = "must-not-appear"
        self.fixture.sync_quote_baseline(baseline)
        leaked, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(leaked.ok)
        self.assertIn("credential-like", leaked.error or "")

        self.fixture = WorkspaceFixture(self.root)
        self.fixture.attach_intraday_quote_baseline()
        outside = self.root.parent / f"{self.root.name}-quote.json"
        outside_meta = _write(outside, _json_bytes({"c": 1}))
        try:
            baseline = copy.deepcopy(self.fixture.envelope["market_quote_baseline"])
            baseline["quotes"][0]["evidence_ref"] = copy.deepcopy(outside_meta)
            baseline["attempts"][0]["evidence_ref"] = copy.deepcopy(outside_meta)
            self.fixture.sync_quote_baseline(baseline)
            escaped, _ = self.run_completed(self.fixture.completed())
        finally:
            outside.unlink()
        self.assertFalse(escaped.ok)
        self.assertIn("escapes the workspace", escaped.error or "")

    def test_quote_raw_evidence_cannot_contain_a_credential(self) -> None:
        self.fixture.attach_intraday_quote_baseline()
        baseline = copy.deepcopy(self.fixture.envelope["market_quote_baseline"])
        evidence_path = self.root / baseline["attempts"][0]["evidence_ref"]["path"]
        raw = json.loads(evidence_path.read_text(encoding="utf-8"))
        raw["request_url"] = "https://provider.example/quote?token=must-not-appear"
        evidence_meta = _write(evidence_path, _json_bytes(raw))
        evidence_meta["path"] = evidence_path.relative_to(self.root).as_posix()
        baseline["attempts"][0]["evidence_ref"] = copy.deepcopy(evidence_meta)
        baseline["quotes"][0]["evidence_ref"] = copy.deepcopy(evidence_meta)
        self.fixture.sync_quote_baseline(baseline)
        leaked, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(leaked.ok)
        self.assertIn("credential-like", leaked.error or "")

    def test_quote_baseline_must_match_every_derived_layer(self) -> None:
        self.fixture.attach_intraday_quote_baseline()
        self.fixture.rewrite_json(
            self.fixture.report,
            lambda document: document["market_quote_baseline"].update(status="complete"),
        )
        report_digest = self.fixture.envelope["artifacts"]["report_json"]["sha256"]
        self.fixture.envelope["lineage"]["report_html"]["input_sha256"] = report_digest
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("differs from the canonical dataset", bad.error or "")

    def test_intraday_quote_cannot_be_promoted_to_completed_close(self) -> None:
        self.fixture.attach_intraday_quote_baseline()
        baseline = copy.deepcopy(self.fixture.envelope["market_quote_baseline"])
        baseline["status"] = "complete"
        baseline["quotes"][0]["status"] = "complete"
        baseline["attempts"][0]["status"] = "complete"
        self.fixture.sync_quote_baseline(baseline)
        bad, _ = self.run_completed(self.fixture.completed())
        self.assertFalse(bad.ok)
        self.assertIn("misrepresents", bad.error or "")

    def test_prior_day_intraday_quote_cannot_be_promoted_to_close(self) -> None:
        self.fixture.attach_intraday_quote_baseline()
        baseline = copy.deepcopy(self.fixture.envelope["market_quote_baseline"])
        baseline["captured_at"] = "2026-08-15T14:00:00Z"
        baseline["status"] = "complete"
        baseline["quotes"][0]["captured_at"] = baseline["captured_at"]
        baseline["quotes"][0]["delay_seconds"] = 72000
        baseline["quotes"][0]["delay_status"] = "delayed"
        baseline["quotes"][0]["status"] = "complete"
        baseline["attempts"][0]["captured_at"] = baseline["captured_at"]
        baseline["attempts"][0]["status"] = "complete"
        self.fixture.sync_quote_baseline(baseline)

        bad, _ = self.run_completed(self.fixture.completed())

        self.assertFalse(bad.ok)
        self.assertIn("misrepresents", bad.error or "")

    def test_rejected_envelope_does_not_bypass_quote_evidence_verification(self) -> None:
        self.fixture.attach_intraday_quote_baseline()
        envelope = copy.deepcopy(self.fixture.envelope)
        envelope["ok"] = False
        envelope["quality"] = {"status": "failed", "issues": ["rejected"]}
        del envelope["artifacts"]["report_json"]
        del envelope["artifacts"]["report_html"]
        envelope["lineage"] = {}
        envelope["market_quote_baseline"]["quotes"][0]["evidence_ref"][
            "bytes"
        ] = 1
        rejected, _ = self.run_completed(
            {"ok": True, "error": "", "stdout": _json_bytes(envelope),
             "stderr": b"", "returncode": 1}
        )
        self.assertFalse(rejected.ok)
        self.assertEqual({}, rejected.artifacts)
        self.assertIn("exited with status 1", rejected.error or "")


if __name__ == "__main__":
    unittest.main()
