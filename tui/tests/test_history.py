"""Report-history index (SQLite) — offline fixtures only."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tui.history import INTAKE, LEGACY, HistoryStore


def _native_doc(day: str, run_id: str, counts: dict[str, int]) -> dict:
    return {
        "schema": "stammtisch.daily-report.v1",
        "revision": 1,
        "date": day,
        "run_id": run_id,
        "captured_at": "2026-08-17T09:49:21Z",
        "brief": [{"text": "中文日报摘要", "sources": ["Fixture"]}],
        "markets": {key: [] for key in counts},
        "market_counts": counts,
        "intake": {"expected": 14, "succeeded": 10, "failed": 4, "pruned": 0},
    }


def _legacy_doc(day: str, markets: dict[str, list]) -> dict:
    return {
        "date": day,
        "model": "fixture-model",
        "brief": [{"text": "中文日报摘要", "sources": ["Fixture"]}],
        "markets": markets,
        "notes": [],
    }


def _write_native_run(workspace: Path, run_id: str, day: str, counts: dict[str, int]) -> Path:
    run_dir = workspace / "runs" / run_id
    run_dir.mkdir(parents=True)
    path = run_dir / f"fin-daily-{day}.json"
    path.write_text(
        json.dumps(_native_doc(day, run_id, counts), ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / f"fin-daily-{day}.html").write_text("<html></html>", encoding="utf-8")
    return path


def _write_legacy(root: Path, day: str, markets: dict[str, list], *, refined: bool = True) -> Path:
    output = root / day / "output"
    output.mkdir(parents=True)
    kind = "refined" if refined else "filtered"
    path = output / f"fin-daily-{day}.{kind}.json"
    path.write_text(json.dumps(_legacy_doc(day, markets), ensure_ascii=False), encoding="utf-8")
    (output / f"fin-daily-{day}.html").write_text("<html></html>", encoding="utf-8")
    return path


class HistoryStoreTest(unittest.TestCase):
    def test_index_workspace_and_legacy_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            legacy = root / "legacy"
            _write_native_run(workspace, "20260817T094921Z-a1b2c3d4", "20260817", {"ashare": 78, "hk": 99})
            _write_legacy(legacy, "20260814", {"ashare": [{"t": 1}], "us": [{"t": 1}, {"t": 2}]})

            store = HistoryStore(root / "history.db")
            stats = store.index_all(workspace, legacy)

            self.assertEqual(stats["scanned"], 2)
            self.assertEqual(stats["indexed"], 2)
            self.assertEqual(stats["skipped"], 0)
            entries = store.list_reports()
            self.assertEqual([entry.report_date for entry in entries], ["20260817", "20260814"])

            native = entries[0]
            self.assertEqual(native.origin, INTAKE)
            self.assertEqual(native.run_id, "20260817T094921Z-a1b2c3d4")
            self.assertEqual(native.markets, {"ashare": 78, "hk": 99})
            self.assertEqual(native.items_total, 177)
            self.assertEqual((native.sources_succeeded, native.sources_expected), (10, 14))
            self.assertTrue(native.html_path.endswith("fin-daily-20260817.html"))
            self.assertEqual(len(native.sha256), 64)

            imported = entries[1]
            self.assertEqual(imported.origin, LEGACY)
            self.assertEqual(imported.markets, {"ashare": 1, "us": 2})
            self.assertEqual(imported.items_total, 3)
            self.assertEqual(imported.model, "fixture-model")
            self.assertEqual((imported.sources_succeeded, imported.sources_expected), (0, 0))

    def test_intake_outranks_legacy_for_the_same_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            legacy = root / "legacy"
            native_path = _write_native_run(workspace, "20260814T160007Z-28d7559f", "20260814", {"ashare": 33})
            _write_legacy(legacy, "20260814", {"ashare": [{"t": 1}]})

            store = HistoryStore(root / "history.db")
            store.index_all(workspace, legacy)
            entries = store.list_reports()

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].origin, INTAKE)
            self.assertEqual(entries[0].json_path, str(native_path))

    def test_latest_intake_run_wins_within_one_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            _write_native_run(workspace, "20260817T090000Z-aaaaaaaa", "20260817", {"ashare": 1})
            newer = _write_native_run(workspace, "20260817T100000Z-bbbbbbbb", "20260817", {"ashare": 2})

            store = HistoryStore(root / "history.db")
            store.index_workspace(workspace)
            entry = store.get("20260817")

            self.assertIsNotNone(entry)
            self.assertEqual(entry.json_path, str(newer))
            self.assertEqual(entry.markets, {"ashare": 2})

    def test_malformed_and_mismatched_files_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            good = _write_native_run(workspace, "20260817T094921Z-a1b2c3d4", "20260817", {"hk": 5})
            run_dir = workspace / "runs" / "20260817T100000Z-bbbbbbbb"
            run_dir.mkdir(parents=True)
            (run_dir / "fin-daily-20260817.json").write_text("{not json", encoding="utf-8")
            odd_dir = workspace / "runs" / "20260816T100000Z-cccccccc"
            odd_dir.mkdir(parents=True)
            # Filename date and document date disagree: not a trustworthy report.
            (odd_dir / "fin-daily-20260816.json").write_text(
                json.dumps(_native_doc("20260815", "x", {"us": 1})), encoding="utf-8"
            )

            store = HistoryStore(root / "history.db")
            stats = store.index_workspace(workspace)

            self.assertEqual(stats["scanned"], 3)
            self.assertEqual(stats["indexed"], 1)
            self.assertEqual(stats["skipped"], 2)
            self.assertEqual([entry.json_path for entry in store.list_reports()], [str(good)])

    def test_reindex_is_incremental_and_update_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            path = _write_native_run(workspace, "20260817T094921Z-a1b2c3d4", "20260817", {"ashare": 1})

            store = HistoryStore(root / "history.db")
            first = store.index_workspace(workspace)
            self.assertEqual(first["indexed"], 1)
            second = store.index_workspace(workspace)
            self.assertEqual(second["unchanged"], 1)
            self.assertEqual(second["indexed"], 0)
            self.assertEqual(len(store.list_reports()), 1)

            # A rewritten artifact (new mtime) is re-parsed, not duplicated.
            path.write_text(
                json.dumps(_native_doc("20260817", "20260817T094921Z-a1b2c3d4", {"ashare": 9})),
                encoding="utf-8",
            )
            os.utime(path, None)
            third = store.index_workspace(workspace)
            self.assertEqual(third["indexed"], 1)
            entries = store.list_reports()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].markets, {"ashare": 9})

    def test_latest_and_get_on_empty_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "history.db")
            self.assertIsNone(store.latest())
            self.assertIsNone(store.get("20260817"))
            self.assertEqual(store.list_reports(), [])

    def test_legacy_filtered_is_used_when_refined_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            filtered = _write_legacy(legacy, "20260813", {"hk": [{"t": 1}]}, refined=False)

            store = HistoryStore(root / "history.db")
            stats = store.index_legacy_tree(legacy)

            self.assertEqual(stats["indexed"], 1)
            entry = store.get("20260813")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.json_path, str(filtered))
            self.assertEqual(entry.origin, LEGACY)

    def test_legacy_without_doc_date_uses_filename_anchor(self) -> None:
        # Early legacy report JSON carried no in-doc date field; the
        # directory/filename date is the legacy contract's anchor.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "legacy" / "20260811" / "output"
            output.mkdir(parents=True)
            path = output / "fin-daily-20260811.refined.json"
            path.write_text(json.dumps({
                "model": "fixture",
                "brief": [],
                "markets": {"ashare": [{"t": 1}], "hk": [{"t": 1}, {"t": 2}]},
                "notes": [],
            }), encoding="utf-8")

            store = HistoryStore(root / "history.db")
            stats = store.index_legacy_tree(root / "legacy")

            self.assertEqual(stats["indexed"], 1)
            entry = store.get("20260811")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.report_date, "20260811")
            self.assertEqual(entry.items_total, 3)

            # A contradicting in-doc date stays rejected (fail-closed).
            path.write_text(json.dumps({"date": "20260810", "markets": {}}), encoding="utf-8")
            os.utime(path, None)
            stats = store.index_legacy_tree(root / "legacy")
            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(store.get("20260811").items_total, 3)

    def test_row_text_summarizes_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            _write_native_run(workspace, "20260817T094921Z-a1b2c3d4", "20260817", {"ashare": 78, "hk": 99})
            store = HistoryStore(root / "history.db")
            store.index_workspace(workspace)
            text = store.latest().row_text()
            self.assertIn("2026-08-17", text)
            self.assertIn(INTAKE, text)
            self.assertIn("ashare 78", text)
            self.assertIn("hk 99", text)
            self.assertIn("177 items", text)
            self.assertIn("src 10/14", text)


if __name__ == "__main__":
    unittest.main()
