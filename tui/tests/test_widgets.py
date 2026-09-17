"""Widget render tests — candlestick chart and gate card, no app required."""

from __future__ import annotations

import unittest

from tui.widgets import GateCard


class GateCardTest(unittest.TestCase):
    def test_scalar_threshold_renders(self):
        # Scalar thresholds are legal gate-record shapes and must not crash
        # the run inspector.
        card = GateCard()
        card.gate = {
            "gate_id": "g1", "decision": "fail", "kind": "metric_threshold",
            "threshold": 0.42, "observed": 0.1,
        }
        self.assertIn("threshold: 0.42", str(card.render()))

    def test_dict_threshold_renders(self):
        card = GateCard()
        card.gate = {
            "gate_id": "g1", "decision": "pass", "kind": "metric_threshold",
            "threshold": {"op": ">=", "value": 2}, "observed": 2,
        }
        self.assertIn(">= 2", str(card.render()))

    def test_missing_gate_renders_placeholder(self):
        card = GateCard()
        self.assertIn("(no gate data)", str(card.render()))


if __name__ == "__main__":
    unittest.main()
