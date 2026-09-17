"""CLI driver lifecycle and envelope-contract tests."""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from tui.driver import StammtischDriver


class DriverTest(unittest.TestCase):
    def _script(self, root: Path, body: str) -> str:
        path = root / "fake-core"
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def test_run_budget_covers_all_declared_stage_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "pipeline.json"
            spec.write_text(json.dumps({
                "stages": [
                    {"timeout_seconds": 10},
                    {},
                    {"timeout_seconds": 25},
                ]
            }), encoding="utf-8")
            # Raw budget would be 3710s; the absolute ceiling clamps it.
            self.assertEqual(1800.0, StammtischDriver._pipeline_run_timeout(str(spec)))

    def test_run_budget_covers_a2a_poll_and_final_read_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "pipeline.json"
            spec.write_text(json.dumps({
                "stages": [{
                    "timeout_seconds": 1,
                    "poll_seconds": 3600,
                    "runtime": {"protocol": "a2a", "endpoint": "http://example.invalid"},
                }]
            }), encoding="utf-8")
            self.assertEqual(426.0, StammtischDriver._pipeline_run_timeout(str(spec)))

    def test_run_budget_covers_poll_sleep_larger_than_stage_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "pipeline.json"
            spec.write_text(json.dumps({
                "stages": [{
                    "timeout_seconds": 3600,
                    "poll_seconds": 7200,
                    "runtime": {"protocol": "a2a", "endpoint": "http://example.invalid"},
                }]
            }), encoding="utf-8")
            self.assertEqual(1800.0, StammtischDriver._pipeline_run_timeout(str(spec)))

    def test_bad_envelope_shape_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = self._script(root, "print('[]')\n")
            result = StammtischDriver(binary=binary)._run(["status"])
            self.assertFalse(result.ok)
            self.assertIn("not an object", result.error_message or "")

    @unittest.skipUnless(os.name == "posix", "process-group lifecycle is POSIX-specific")
    def test_close_stops_only_owned_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "started"
            binary = self._script(root, f"""
                import json, pathlib, sys, time
                if 'cancel' in sys.argv:
                    print(json.dumps({{"ok": True, "command": "cancel", "data": {{"sealed": []}}}}))
                    raise SystemExit(0)
                pathlib.Path({str(marker)!r}).write_text('started')
                time.sleep(60)
            """)
            driver = StammtischDriver(binary=binary)
            holder: list[object] = []
            thread = threading.Thread(
                target=lambda: holder.append(driver._run(["status"], timeout=None)),
                daemon=True,
            )
            thread.start()
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            driver.close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(1, len(holder))
            self.assertFalse(holder[0].ok)

    def test_close_seals_abandoned_runs_via_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = root / "seen"
            binary = self._script(root, f"""
                import json, pathlib, sys
                pathlib.Path({str(seen)!r}).write_text(' '.join(sys.argv[1:]))
                print(json.dumps({{"ok": True, "command": "cancel", "data": {{"sealed": []}}}}))
            """)
            driver = StammtischDriver(binary=binary, state_root=str(root / "state"))
            driver.close()
            self.assertTrue(seen.exists())
            self.assertIn("cancel", seen.read_text())
            self.assertIn("--abandoned", seen.read_text())


if __name__ == "__main__":
    unittest.main()
