"""Offline contract tests for the GALAHAD consensus-bar loader."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from tui.validated_bars import (
    MANIFEST_SCHEMA,
    ManifestLookupError,
    ManifestValidationError,
    StoreConfigurationError,
    ValidatedBarsStore,
    load_validated_bars,
)


def _domain_digest(domain: str, value) -> str:
    payload = domain.encode("ascii") + b"\0" + json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _manifest(
    *,
    symbol: str = "600584",
    market: str = "ashare",
    interval: str = "1d",
    status: str = "accepted",
) -> dict:
    currency = "CNY" if market == "ashare" else "USD"
    bars = [
        {
            "date": "2026-08-13",
            "open": 51.2,
            "high": 52.0,
            "low": 50.8,
            "close": 51.7,
            "volume": 1_200_000,
        },
        {
            "date": "2026-08-14",
            "open": 51.8,
            "high": 53.1,
            "low": 51.5,
            "close": 52.9,
            "volume": 1_500_000,
        },
    ]
    sources = [
        {
            "source_id": "provider-a",
            "provider": "provider-a",
            "independence_group": "group-a",
            "price_basis": "raw",
            "volume_unit": "shares",
            "identity": {
                "symbol": symbol,
                "market": market,
                "currency": currency,
            },
            "input_artifact_sha256": "a" * 64,
            "frame_sha256": "c" * 64,
            "status": "ready",
            "reason_codes": [],
        },
        {
            "source_id": "provider-b",
            "provider": "provider-b",
            "independence_group": "group-b",
            "price_basis": "raw",
            "volume_unit": "shares",
            "identity": {
                "symbol": symbol,
                "market": market,
                "currency": currency,
            },
            "input_artifact_sha256": "b" * 64,
            "frame_sha256": "d" * 64,
            "status": "ready",
            "reason_codes": [],
        },
    ]
    sessions = [record["date"] for record in bars]
    diagnostic_status = "warning" if status == "partial" else "accepted"
    field_status = "accepted"
    diagnostics = [
        {
            "date": record["date"],
            "status": diagnostic_status,
            "reason_codes": ["incomplete_source_support"]
            if diagnostic_status == "warning"
            else [],
            "supporting_groups": ["group-a", "group-b"],
            "sources": [
                {
                    "source_id": source["source_id"],
                    "provider": source["provider"],
                    "independence_group": source["independence_group"],
                    "status": (
                        "excluded"
                        if status == "partial" and source["source_id"] == "provider-b"
                        else "contributed"
                    ),
                    "reason_codes": (
                        ["group_outlier"]
                        if status == "partial" and source["source_id"] == "provider-b"
                        else []
                    ),
                }
                for source in sources
            ],
            "fields": {
                column: {
                    "status": field_status,
                    "consensus": record[column],
                    "relative_spread": 0.0,
                    "contributing_sources": (
                        ["provider-a"]
                        if status == "partial"
                        else ["provider-a", "provider-b"]
                    ),
                    "excluded_sources": (
                        ["provider-b"] if status == "partial" else []
                    ),
                }
                for column in ("open", "high", "low", "close", "volume")
            },
        }
        for record in bars
    ]
    if status == "partial":
        # A publishable consensus still requires two independent groups.  Use
        # the second date as a quarantined calendar session instead of
        # inventing a one-source accepted bar.
        bars = bars[:1]
        diagnostics[0] = {
            **diagnostics[0],
            "reason_codes": ["field_tolerance_warning"],
            "supporting_groups": ["group-a", "group-b"],
            "sources": [
                {
                    **decision,
                    "status": "contributed",
                    "reason_codes": [],
                }
                for decision in diagnostics[0]["sources"]
            ],
            "fields": {
                column: {
                    **field,
                    "status": "warning" if column == "close" else "accepted",
                    "relative_spread": 0.002 if column == "close" else 0.0,
                    "contributing_sources": ["provider-a", "provider-b"],
                    "excluded_sources": [],
                }
                for column, field in diagnostics[0]["fields"].items()
            },
        }
        diagnostics[1] = {
            "date": sessions[1],
            "status": "quarantined",
            "reason_codes": ["insufficient_independent_groups"],
            "supporting_groups": [],
            "sources": [
                {
                    "source_id": source["source_id"],
                    "provider": source["provider"],
                    "independence_group": source["independence_group"],
                    "status": "missing",
                    "reason_codes": ["source_missing"],
                }
                for source in sources
            ],
            "fields": {
                column: {
                    "status": "quarantined",
                    "consensus": None,
                    "relative_spread": None,
                    "contributing_sources": [],
                    "excluded_sources": ["provider-a", "provider-b"],
                }
                for column in ("open", "high", "low", "close", "volume")
            },
        }
    if status == "quarantined":
        bars = []
        for source_record in sources:
            source_record["status"] = "empty"
            source_record["reason_codes"] = ["empty_source"]
        diagnostics = [
            {
                "date": date,
                "status": "quarantined",
                "reason_codes": ["insufficient_independent_groups"],
                "supporting_groups": [],
                "sources": [
                    {
                        "source_id": source["source_id"],
                        "provider": source["provider"],
                        "independence_group": source["independence_group"],
                        "status": "missing",
                        "reason_codes": ["source_missing"],
                    }
                    for source in sources
                ],
                "fields": {
                    column: {
                        "status": "quarantined",
                        "consensus": None,
                        "relative_spread": None,
                        "contributing_sources": [],
                        "excluded_sources": ["provider-a", "provider-b"],
                    }
                    for column in ("open", "high", "low", "close", "volume")
                },
            }
            for date in sessions
        ]

    document = {
        "schema": MANIFEST_SCHEMA,
        "status": status,
        "identity": {
            "symbol": symbol,
            "market": market,
            "interval": interval,
            "calendar": "XSHG" if market == "ashare" else "XNYS",
            "currency": currency,
            "price_basis": "raw",
            "volume_unit": "shares",
        },
        "policy": {
            "price_relative": {"pass": 0.001, "warning": 0.005, "quarantine": 0.02},
            "volume_relative": {"pass": 0.02, "warning": 0.10, "quarantine": 0.30},
            "minimum_independent_groups": 2,
            "maximum_independent_groups": 3,
            "alignment": "observed_union_no_forward_fill",
            "vote_rule": "hierarchical_independence_group_2_of_3_median",
            "hash_domains": {
                "frame_sha256": "quantkit.ohlcv-source-frame.v1",
                "calendar_sessions_sha256": "quantkit.ohlcv-session-calendar.v1",
                "bars_sha256": "quantkit.ohlcv-accepted-bars.v1",
                "output_sha256": "quantkit.ohlcv-consensus-manifest.v1",
            },
        },
        "calendar": {
            "name": "XSHG" if market == "ashare" else "XNYS",
            "sessions": sessions,
            "sessions_sha256": _domain_digest(
                "quantkit.ohlcv-session-calendar.v1", sessions
            ),
        },
        "bars": bars,
        "bars_sha256": _domain_digest("quantkit.ohlcv-accepted-bars.v1", bars),
        "sources": sources,
        "diagnostics": diagnostics,
        "output_sha256": "",
    }
    _reseal(document)
    return document


def _reseal(document: dict) -> None:
    document["calendar"]["sessions_sha256"] = _domain_digest(
        "quantkit.ohlcv-session-calendar.v1", document["calendar"]["sessions"]
    )
    document["bars_sha256"] = _domain_digest(
        "quantkit.ohlcv-accepted-bars.v1", document["bars"]
    )
    document["output_sha256"] = _domain_digest(
        "quantkit.ohlcv-consensus-manifest.v1",
        {key: value for key, value in document.items() if key != "output_sha256"}
    )


class ValidatedBarsStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="stammtisch-bars-")
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, name: str, document: dict, *, reseal: bool = False) -> Path:
        if reseal:
            _reseal(document)
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return path

    def test_exact_accepted_lookup_returns_verified_dataclass(self) -> None:
        expected = _manifest()
        path = self.write("nested/600584.json", expected)

        result = ValidatedBarsStore(self.root).lookup(
            "600584", market="ashare", interval="1d"
        )

        self.assertEqual(path, result.path)
        self.assertEqual(expected, result.manifest)
        self.assertEqual(tuple(expected["bars"]), result.records)
        self.assertEqual(52.9, result.records[-1]["close"])

    def test_one_shot_loader_and_lazy_frame_conversion(self) -> None:
        self.write("bars.json", _manifest())

        result = load_validated_bars(self.root, "600584", market="ashare")
        frame = result.to_frame()

        self.assertEqual(["open", "high", "low", "close", "volume"], list(frame.columns))
        self.assertEqual("2026-08-14", frame.index[-1].date().isoformat())
        self.assertEqual(52.9, frame.iloc[-1]["close"])

    def test_nonaccepted_status_requires_explicit_opt_out(self) -> None:
        self.write("partial.json", _manifest(status="partial"))
        store = ValidatedBarsStore(self.root)

        with self.assertRaisesRegex(ManifestLookupError, "not accepted"):
            store.lookup("600584", market="ashare")
        result = store.lookup("600584", market="ashare", accepted_only=False)
        self.assertEqual("partial", result.manifest["status"])

    def test_lookup_never_falls_back_or_guesses_an_ambiguous_interval(self) -> None:
        ashare = _manifest(market="ashare")
        us = _manifest(market="us")
        self.write("ashare.json", ashare)
        self.write("us.json", us)
        store = ValidatedBarsStore(self.root)

        with self.assertRaisesRegex(ManifestLookupError, "ambiguous"):
            store.lookup("600584")
        self.assertEqual(
            "ashare",
            store.lookup("600584", market="ashare", interval="1d").manifest[
                "identity"
            ]["market"],
        )
        with self.assertRaisesRegex(ManifestLookupError, "no consensus manifest"):
            store.lookup("MISSING", market="us", interval="1d")

    def test_duplicate_full_identity_is_rejected(self) -> None:
        self.write("one.json", _manifest())
        self.write("two.json", _manifest())

        with self.assertRaisesRegex(ManifestLookupError, "duplicate manifest identity"):
            ValidatedBarsStore(self.root).lookup("600584")

    def test_bars_and_output_hashes_are_both_verified(self) -> None:
        for field in ("bars_sha256", "output_sha256"):
            with self.subTest(field=field):
                document = _manifest()
                document[field] = "0" * 64
                self.write("bars.json", document)
                with self.assertRaisesRegex(ManifestValidationError, field):
                    ValidatedBarsStore(self.root).lookup("600584")
                (self.root / "bars.json").unlink()

    def test_hashes_are_domain_separated_and_calendar_is_bound(self) -> None:
        document = _manifest()
        document["bars_sha256"] = hashlib.sha256(
            json.dumps(
                document["bars"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        document["output_sha256"] = _domain_digest(
            "quantkit.ohlcv-consensus-manifest.v1",
            {key: value for key, value in document.items() if key != "output_sha256"},
        )
        self.write("bars.json", document)
        with self.assertRaisesRegex(ManifestValidationError, "bars_sha256"):
            ValidatedBarsStore(self.root).lookup("600584")
        (self.root / "bars.json").unlink()

        document = _manifest()
        document["calendar"]["sessions_sha256"] = "0" * 64
        document["output_sha256"] = _domain_digest(
            "quantkit.ohlcv-consensus-manifest.v1",
            {key: value for key, value in document.items() if key != "output_sha256"},
        )
        self.write("bars.json", document)
        with self.assertRaisesRegex(ManifestValidationError, "sessions_sha256"):
            ValidatedBarsStore(self.root).lookup("600584")

    def test_policy_source_and_diagnostic_lineage_are_replayed(self) -> None:
        mutations = (
            (
                "hash_domains",
                lambda doc: doc["policy"]["hash_domains"].update(
                    bars_sha256="wrong-domain"
                ),
            ),
            (
                "independence_group differs",
                lambda doc: doc["sources"][1].update(
                    independence_group="different-group"
                ),
            ),
            (
                "provider differs",
                lambda doc: doc["diagnostics"][0]["sources"][0].update(
                    provider="invented-provider"
                ),
            ),
            (
                "consensus differs",
                lambda doc: doc["diagnostics"][0]["fields"]["close"].update(
                    consensus=999.0
                ),
            ),
            (
                "status conflicts",
                lambda doc: doc.update(status="partial"),
            ),
        )
        for expected, mutate in mutations:
            with self.subTest(expected=expected):
                document = _manifest()
                mutate(document)
                self.write("bars.json", document, reseal=True)
                with self.assertRaisesRegex(ManifestValidationError, expected):
                    ValidatedBarsStore(self.root).lookup("600584")
                (self.root / "bars.json").unlink()

    def test_candle_geometry_dates_and_numbers_fail_closed(self) -> None:
        mutations = (
            ("geometry", lambda doc: doc["bars"][0].update(high=50.0)),
            ("strictly ascending", lambda doc: doc["bars"][1].update(date="2026-08-13")),
            ("positive", lambda doc: doc["bars"][0].update(open=0)),
            ("non-negative", lambda doc: doc["bars"][0].update(volume=-1)),
            ("JSON number", lambda doc: doc["bars"][0].update(close=True)),
        )
        for expected, mutate in mutations:
            with self.subTest(expected=expected):
                document = _manifest()
                mutate(document)
                self.write("bars.json", document, reseal=True)
                with self.assertRaisesRegex(ManifestValidationError, expected):
                    ValidatedBarsStore(self.root).lookup("600584")
                (self.root / "bars.json").unlink()

    def test_sources_need_independence_and_matching_units(self) -> None:
        mutations = (
            (
                "independent groups",
                lambda doc: doc["sources"][1].update(independence_group="group-a"),
            ),
            (
                "price_basis differs",
                lambda doc: doc["sources"][1].update(price_basis="adjusted"),
            ),
            (
                "volume_unit differs",
                lambda doc: doc["sources"][1].update(volume_unit="lots"),
            ),
        )
        for expected, mutate in mutations:
            with self.subTest(expected=expected):
                document = _manifest()
                mutate(document)
                self.write("bars.json", document, reseal=True)
                with self.assertRaisesRegex(ManifestValidationError, expected):
                    ValidatedBarsStore(self.root).lookup("600584")
                (self.root / "bars.json").unlink()

    def test_strict_shape_and_duplicate_json_keys_are_rejected(self) -> None:
        unknown = _manifest()
        unknown["fallback_provider"] = "forbidden"
        self.write("unknown.json", unknown, reseal=True)
        with self.assertRaisesRegex(ManifestValidationError, "unknown fallback_provider"):
            ValidatedBarsStore(self.root).lookup("600584")
        (self.root / "unknown.json").unlink()

        duplicate = json.dumps(_manifest(), separators=(",", ":"))
        duplicate = duplicate[:-1] + ',"status":"accepted"}'
        (self.root / "duplicate.json").write_text(duplicate, encoding="utf-8")
        with self.assertRaisesRegex(ManifestValidationError, "duplicate JSON key"):
            ValidatedBarsStore(self.root).lookup("600584")

    def test_file_count_size_bar_count_and_entry_count_are_bounded(self) -> None:
        self.write("one.json", _manifest(symbol="ONE"))
        self.write("two.json", _manifest(symbol="TWO"))
        with self.assertRaisesRegex(StoreConfigurationError, "file limit"):
            ValidatedBarsStore(self.root, max_files=1).lookup("ONE")

        (self.root / "two.json").unlink()
        size = (self.root / "one.json").stat().st_size
        with self.assertRaisesRegex(StoreConfigurationError, "byte limit"):
            ValidatedBarsStore(self.root, max_file_bytes=size - 1).lookup("ONE")

        with self.assertRaisesRegex(ManifestValidationError, "record limit"):
            ValidatedBarsStore(self.root, max_bars=1).lookup("ONE")

        (self.root / "note.txt").write_text("not a manifest", encoding="utf-8")
        with self.assertRaisesRegex(StoreConfigurationError, "entry limit"):
            ValidatedBarsStore(self.root, max_entries=1).lookup("ONE")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_root_and_nested_symlinks_are_rejected(self) -> None:
        self.write("bars.json", _manifest())
        linked_root = self.root.parent / f"{self.root.name}-link"
        linked_root.symlink_to(self.root, target_is_directory=True)
        try:
            with self.assertRaisesRegex(StoreConfigurationError, "root must not be a symlink"):
                ValidatedBarsStore(linked_root)
        finally:
            linked_root.unlink()

        (self.root / "alias.json").symlink_to(self.root / "bars.json")
        with self.assertRaisesRegex(StoreConfigurationError, "contains a symlink"):
            ValidatedBarsStore(self.root).lookup("600584")

    def test_hardlinked_candidates_are_rejected_as_path_ambiguity(self) -> None:
        original = self.write("bars.json", _manifest())
        alias = self.root / "alias.json"
        try:
            os.link(original, alias)
        except OSError as exc:  # pragma: no cover - unusual test filesystem
            self.skipTest(f"hard links are unavailable: {exc}")
        with self.assertRaisesRegex(StoreConfigurationError, "duplicate file alias"):
            ValidatedBarsStore(self.root).lookup("600584")

    def test_manifest_copy_is_not_shared_between_loads(self) -> None:
        self.write("bars.json", _manifest())
        store = ValidatedBarsStore(self.root)
        first = store.lookup("600584")
        first.manifest["diagnostics"][0]["status"] = "quarantined"

        second = store.lookup("600584")
        self.assertEqual("accepted", second.manifest["diagnostics"][0]["status"])

    def test_diagnostics_are_structured_and_reason_codes_are_unique(self) -> None:
        document = _manifest()
        document["diagnostics"] = {"date": "2026-08-13"}
        self.write("bars.json", document, reseal=True)
        with self.assertRaisesRegex(ManifestValidationError, "must be an array"):
            ValidatedBarsStore(self.root).lookup("600584")
        (self.root / "bars.json").unlink()

        document = _manifest()
        document["diagnostics"][0]["reason_codes"] = ["same", "same"]
        self.write("bars.json", document, reseal=True)
        with self.assertRaisesRegex(ManifestValidationError, "sorted and unique"):
            ValidatedBarsStore(self.root).lookup("600584")

    def test_diagnostic_contributors_and_supporting_groups_are_bound(self) -> None:
        mutations = (
            (
                "supporting_groups do not match",
                lambda doc: doc["diagnostics"][0].update(
                    supporting_groups=["group-a"]
                ),
            ),
            (
                "contributing_sources differ",
                lambda doc: (
                    doc["diagnostics"][0]["fields"]["close"].update(
                        contributing_sources=["provider-a"]
                    ),
                    doc["diagnostics"][0]["fields"]["close"].update(
                        excluded_sources=["provider-b"]
                    ),
                ),
            ),
            (
                "contributed source cannot have reason codes",
                lambda doc: doc["diagnostics"][0]["sources"][0].update(
                    reason_codes=["invented_warning"]
                ),
            ),
        )
        for expected, mutate in mutations:
            with self.subTest(expected=expected):
                document = _manifest()
                mutate(document)
                self.write("bars.json", document, reseal=True)
                with self.assertRaisesRegex(ManifestValidationError, expected):
                    ValidatedBarsStore(self.root).lookup("600584")
                (self.root / "bars.json").unlink()

    def test_top_level_status_and_diagnostic_semantics_are_reconciled(self) -> None:
        mutations = (
            (
                "accepted session cannot have reason codes",
                lambda doc: doc["diagnostics"][0].update(
                    reason_codes=["invented_warning"]
                ),
            ),
            (
                "status conflicts with relative_spread",
                lambda doc: doc["diagnostics"][0]["fields"]["open"].update(
                    status="warning"
                ),
            ),
            (
                "does not cover every calendar session",
                lambda doc: doc["diagnostics"].pop(),
            ),
        )
        for expected, mutate in mutations:
            with self.subTest(expected=expected):
                document = _manifest()
                mutate(document)
                self.write("bars.json", document, reseal=True)
                with self.assertRaisesRegex(ManifestValidationError, expected):
                    ValidatedBarsStore(self.root).lookup("600584")
                (self.root / "bars.json").unlink()

    def test_source_identity_status_and_frame_digest_follow_producer_contract(self) -> None:
        document = _manifest()
        document["sources"][0]["identity"]["symbol"] = "IMPOSTOR"
        self.write("bars.json", document, reseal=True)
        with self.assertRaisesRegex(ManifestValidationError, "security identity"):
            ValidatedBarsStore(self.root).lookup("600584")
        (self.root / "bars.json").unlink()

        document = _manifest()
        document["sources"][0]["status"] = "partial"
        document["sources"][0]["reason_codes"] = ["duplicate_session"]
        self.write("bars.json", document, reseal=True)
        self.assertEqual(
            "partial",
            ValidatedBarsStore(self.root).lookup("600584").manifest["sources"][0][
                "status"
            ],
        )
        (self.root / "bars.json").unlink()

        document = _manifest()
        document["sources"][0]["frame_sha256"] = "not-a-digest"
        self.write("bars.json", document, reseal=True)
        with self.assertRaisesRegex(ManifestValidationError, "frame_sha256"):
            ValidatedBarsStore(self.root).lookup("600584")

    def test_explicit_quarantined_load_has_an_empty_frame(self) -> None:
        self.write("bars.json", _manifest(status="quarantined"))
        result = ValidatedBarsStore(self.root).lookup(
            "600584", accepted_only=False
        )

        self.assertEqual((), result.records)
        frame = result.to_frame()
        self.assertTrue(frame.empty)
        self.assertEqual(["open", "high", "low", "close", "volume"], list(frame.columns))


if __name__ == "__main__":
    unittest.main()
