"""SECURITY board watchlist fallback — empty config is not an empty board."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tui.screens.domains import SecurityScreen, security_watchlist


class SecurityWatchlistTest(unittest.TestCase):
    def test_manual_watchlist_wins(self):
        cfg = SimpleNamespace(
            security_symbols=["AAPL", "600098", "600098.SS"],
            recent_symbols=["SOHU"],
            state_root="/no/such/state",
        )
        self.assertEqual(security_watchlist(cfg), ["AAPL", "600098.SS"])

    def test_empty_manual_uses_latest_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = os.path.join(tmp, "decisions")
            os.makedirs(latest)
            with open(os.path.join(latest, "latest.json"), "w") as handle:
                json.dump({
                    "decision_version": 1,
                    "zones": {
                        "A-SHARE": {"positions": [
                            {"symbol": "002289.SZ"},
                            {"symbol": "601869.SS"},
                            {"symbol": "688012.SS", "action": "cut"},
                        ]},
                        "US": {"positions": [{"symbol": "LITE"}]},
                    }
                }, handle)
            cfg = SimpleNamespace(
                security_symbols=[],
                recent_symbols=["SOHU"],
                state_root=tmp,
            )
            self.assertEqual(
                security_watchlist(cfg),
                ["002289.SZ", "601869.SS", "LITE", "SOHU"],
            )

    def test_unknown_decision_version_falls_back_to_recents(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = os.path.join(tmp, "decisions")
            os.makedirs(latest)
            with open(os.path.join(latest, "latest.json"), "w") as handle:
                json.dump({
                    "decision_version": 99,
                    "zones": {"US": {"positions": [{"symbol": "LITE"}]}},
                }, handle)
            cfg = SimpleNamespace(
                security_symbols=[], recent_symbols=["AAPL"], state_root=tmp,
            )
            self.assertEqual(security_watchlist(cfg), ["AAPL"])

    def test_no_decision_falls_back_to_recents(self):
        cfg = SimpleNamespace(
            security_symbols=[],
            recent_symbols=["600098", "AAPL"],
            state_root="/no/such/state",
        )
        self.assertEqual(security_watchlist(cfg), ["600098.SS", "AAPL"])

    def test_effective_driver_state_root_overrides_config_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            configured = os.path.join(tmp, "configured")
            effective = os.path.join(tmp, "effective")
            for root, symbol in (
                (configured, "600519.SS"),
                (effective, "002289.SZ"),
            ):
                latest = os.path.join(root, "decisions")
                os.makedirs(latest)
                with open(os.path.join(latest, "latest.json"), "w") as handle:
                    json.dump({
                        "decision_version": 1,
                        "zones": {"A-SHARE": {"positions": [
                            {"symbol": symbol, "action": "hold"},
                        ]}},
                    }, handle)
            cfg = SimpleNamespace(
                security_symbols=[], recent_symbols=[], state_root=configured,
            )
            self.assertEqual(
                security_watchlist(cfg, effective),
                ["002289.SZ"],
            )

    def test_open_screen_refreshes_a_new_or_replaced_latest_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(
                security_symbols=[], recent_symbols=[], state_root="/wrong/root",
            )
            driver = SimpleNamespace(state_root=tmp)
            screen = SecurityScreen(driver, None, None, cfg, "security.json", None)
            self.assertEqual(screen._symbols, [])

            latest = os.path.join(tmp, "decisions")
            os.makedirs(latest)

            def write(symbol):
                with open(os.path.join(latest, "latest.json"), "w") as handle:
                    json.dump({
                        "decision_version": 1,
                        "zones": {"A-SHARE": {"positions": [
                            {"symbol": symbol, "action": "hold"},
                        ]}},
                    }, handle)

            with mock.patch("tui.screens.domains._run_async"):
                write("002289.SZ")
                screen.action_refresh()
                self.assertEqual(screen._symbols, ["002289.SZ"])

                write("600519.SS")
                screen.action_refresh()
                self.assertEqual(screen._symbols, ["600519.SS"])

    def test_none_config_is_empty(self):
        self.assertEqual(security_watchlist(None), [])


if __name__ == "__main__":
    unittest.main()
