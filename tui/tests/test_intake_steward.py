"""Intake steward decision tests; no network, no model calls."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tui.intake_steward import IntakeSteward


def _app(tmp, *, intake_cmd=None, capturing=False, ai_available=False, plain=False):
    argv = ()
    if plain:
        argv = ("fixture-intake",)
    elif intake_cmd:
        argv = ("fixture-intake", "--firecrawl-url", intake_cmd)
    config = SimpleNamespace(
        intake_argv=argv, workspace_root=os.path.join(tmp, "ws")
    )
    supervisor = SimpleNamespace(capturing=capturing, snapshot=lambda: None)
    ai = SimpleNamespace(available=ai_available)
    return SimpleNamespace(config=config, intake_supervisor=supervisor, ai=ai)


class IntakeStewardTest(unittest.TestCase):
    def test_gate_blocks_without_intake_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steward = IntakeSteward()
            self.assertEqual(
                steward.deterministic_gate(_app(tmp)),
                "intake command not configured",
            )

    def test_gate_blocks_while_capturing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steward = IntakeSteward()
            app = _app(tmp, intake_cmd="http://127.0.0.1:1/", capturing=True)
            with mock.patch(
                "tui.intake_steward.supervisor_for",
                return_value=SimpleNamespace(capturing=True),
            ):
                self.assertEqual(
                    steward.deterministic_gate(app), "capture already running"
                )

    def test_gate_blocks_when_endpoint_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steward = IntakeSteward()
            app = _app(tmp, intake_cmd="http://127.0.0.1:1/")
            self.assertEqual(
                steward.deterministic_gate(app), "capture endpoint unreachable"
            )

    def test_gate_passes_without_firecrawl_flag(self) -> None:
        # An intake command without a local capture endpoint must not be
        # gated: reachability is the product's own contract.
        with tempfile.TemporaryDirectory() as tmp:
            steward = IntakeSteward()
            app = _app(tmp, plain=True)
            self.assertIsNone(steward.deterministic_gate(app))

    def test_no_model_falls_back_to_missing_report_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steward = IntakeSteward()
            app = _app(tmp, ai_available=False)
            with mock.patch.object(
                IntakeSteward,
                "digest",
                lambda self, a: {"latest_report_date": "20260818"},
            ):
                decision = steward.tick(app)
            self.assertFalse(decision["capture"])

            with mock.patch.object(
                IntakeSteward,
                "digest",
                lambda self, a: {"latest_report_date": "20260819"},
            ):
                # Same-day report already present: the conservative rule
                # never captures twice.
                steward2 = IntakeSteward()
                decision = steward2.tick(app)
            self.assertFalse(decision["capture"])

    def test_capture_decision_starts_capture_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steward = IntakeSteward()
            app = _app(tmp, intake_cmd="http://127.0.0.1:1/")
            started = []
            fake_supervisor = SimpleNamespace(
                capturing=False,
                snapshot=lambda: None,
                start=lambda a, c, date=None: started.append(date),
            )
            with mock.patch(
                "tui.intake_steward.supervisor_for", return_value=fake_supervisor
            ), mock.patch(
                "tui.intake_steward._firecrawl_reachable", return_value=True
            ), mock.patch.object(
                IntakeSteward,
                "ask_galahad",
                return_value={"capture": True, "reason": "缺今日数据"},
            ):
                decision = steward.tick(app)
                self.assertTrue(decision["capture"])
                self.assertEqual(started, [None])

                # A second decision inside the cooldown window does not
                # capture again.
                again = steward.tick(app)
                self.assertEqual(
                    again["reason"], "captured within the last 30 minutes"
                )
                self.assertEqual(started, [None])


if __name__ == "__main__":
    unittest.main()
