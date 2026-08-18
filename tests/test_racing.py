"""Racing plugin tests — driver contract and screen render, all offline."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from tui.racing import BOARD_SCHEMA, RacingDriver, RacingScreen


def _fake_cmd(body: str, exit_code: int = 0) -> str:
    d = tempfile.mkdtemp(prefix="stammtisch-fake-racing-")
    script = os.path.join(d, "fake-racing")
    with open(script, "w") as f:
        f.write("#!/bin/sh\necho '%s'\nexit %d\n" % (body.replace("'", r"'\''"), exit_code))
    os.chmod(script, 0o755)
    return script


def _board_payload() -> dict:
    return {
        "ok": True,
        "schema": BOARD_SCHEMA,
        "asof": "2026-08-17",
        "source": "hkjc-results-cache",
        "meetings": [
            {"date": "2026-07-12", "course": "ST", "races": 11},
            {"date": "2026-07-15", "course": "HV", "races": 9},
        ],
        "race": {
            "date": "2026-07-15",
            "course": "HV",
            "race_no": 9,
            "runners": [
                {
                    "no": 1,
                    "horse": "TEST HORSE",
                    "jockey": "J Smith",
                    "trainer": "T Jones",
                    "weight_lb": 126,
                    "draw": 4,
                    "win_odds": 5.2,
                    "finish": 2,
                    "model_prob": 0.18,
                    "edge": 0.03,
                    "ev_per_unit": 0.11,
                    "kelly_stake": 0.02,
                },
                {
                    "no": 2,
                    "horse": "NO DATA",
                    "jockey": "A Lee",
                    "trainer": "B Chan",
                    "weight_lb": 120,
                    "draw": 7,
                    "win_odds": None,
                    "finish": 5,
                    "model_prob": None,
                    "edge": None,
                    "ev_per_unit": None,
                    "kelly_stake": None,
                },
            ],
        },
        "model": {"status": "fitted", "races_used": 61, "walk_forward_logloss": 2.09},
    }


class RacingDriverTest(unittest.TestCase):
    def test_unconfigured(self):
        r = RacingDriver(()).board()
        self.assertFalse(r["ok"])
        self.assertIn("racing_cmd", r["error"])

    def test_not_json(self):
        r = RacingDriver((_fake_cmd("not json"),)).board()
        self.assertFalse(r["ok"])
        self.assertIn("not JSON", r["error"])

    def test_nonzero_exit_with_error_body(self):
        body = json.dumps({"ok": False, "error": "cache empty"})
        r = RacingDriver((_fake_cmd(body, exit_code=1),)).board()
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "cache empty")

    def test_wrong_schema_rejected(self):
        body = json.dumps({"ok": True, "schema": "other.v1"})
        r = RacingDriver((_fake_cmd(body),)).board()
        self.assertFalse(r["ok"])
        self.assertIn("schema", r["error"])

    def test_valid_payload_passes(self):
        r = RacingDriver((_fake_cmd(json.dumps(_board_payload())),)).board()
        self.assertTrue(r["ok"])
        self.assertEqual(r["race"]["race_no"], 9)


class RacingScreenTest(unittest.TestCase):
    def test_screen_renders_board(self):
        cmd = _fake_cmd(json.dumps(_board_payload()))
        config = mock.Mock()
        config.racing_argv = (cmd,)

        async def run():
            screen = RacingScreen(config)
            app = mock.Mock()
            # run_test needs a real app context; mount through a host app
            from textual.app import App

            class Host(App):
                def on_mount(self):
                    self.push_screen(screen)

            host = Host()
            async with host.run_test(size=(120, 40)) as pilot:
                for _ in range(20):
                    await pilot.pause()
                    await asyncio.sleep(0.05)
                    if screen.query_one("#race-runner-table").row_count:
                        break
                runners = screen.query_one("#race-runner-table")
                meetings = screen.query_one("#race-meeting-table")
                return runners.row_count, meetings.row_count, screen.query_one(
                    "#race-status"
                ).render()

        rows, meeting_rows, status = asyncio.run(run())
        self.assertEqual(rows, 2)
        self.assertEqual(meeting_rows, 2)
        self.assertIn("model fitted on 61 races", str(status))

    def test_screen_shows_driver_error(self):
        config = mock.Mock()
        config.racing_argv = (_fake_cmd("garbage"),)

        async def run():
            screen = RacingScreen(config)
            from textual.app import App

            class Host(App):
                def on_mount(self):
                    self.push_screen(screen)

            host = Host()
            async with host.run_test(size=(120, 40)) as pilot:
                for _ in range(20):
                    await pilot.pause()
                    await asyncio.sleep(0.05)
                    if "board failed" in str(
                        screen.query_one("#race-status").render()
                    ):
                        break
                return str(screen.query_one("#race-status").render())

        status = asyncio.run(run())
        self.assertIn("board failed", status)
        self.assertIn("not JSON", status)

    def test_screen_shows_loading_while_in_flight(self):
        # a slow command keeps the status line on a loading hint instead of
        # leaving the board looking empty/broken until the payload lands
        d = tempfile.mkdtemp(prefix="stammtisch-slow-racing-")
        script = os.path.join(d, "slow-racing")
        with open(script, "w") as f:
            f.write("#!/bin/sh\nsleep 0.4\necho '%s'\n"
                    % json.dumps(_board_payload()).replace("'", r"'\''"))
        os.chmod(script, 0o755)
        config = mock.Mock()
        config.racing_argv = (script,)

        async def run():
            screen = RacingScreen(config)
            from textual.app import App

            class Host(App):
                def on_mount(self):
                    self.push_screen(screen)

            host = Host()
            async with host.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                early = str(screen.query_one("#race-status").render())
                for _ in range(40):
                    await pilot.pause()
                    await asyncio.sleep(0.05)
                    if screen.query_one("#race-runner-table").row_count:
                        break
                final = str(screen.query_one("#race-status").render())
                return early, final

        early, final = asyncio.run(run())
        self.assertIn("loading", early)
        self.assertIn("model fitted on 61 races", final)

    def _mounted_screen(self, payload=None):
        cmd = _fake_cmd(json.dumps(payload or _board_payload()))
        config = mock.Mock()
        config.racing_argv = (cmd,)
        screen = RacingScreen(config)
        from textual.app import App

        class Host(App):
            def on_mount(self):
                self.push_screen(screen)

        return Host(), screen

    def test_meetings_latest_first(self):
        async def run():
            host, screen = self._mounted_screen()
            async with host.run_test(size=(120, 40)) as pilot:
                for _ in range(20):
                    await pilot.pause()
                    await asyncio.sleep(0.05)
                    if screen.query_one("#race-meeting-table").row_count:
                        break
                table = screen.query_one("#race-meeting-table")
                return [str(c) for c in table.get_row_at(0)]

        first_row = asyncio.run(run())
        self.assertEqual(first_row[0], "2026-07-15")  # latest on top
        self.assertEqual(first_row[1], "HV")

    def test_meeting_row_select_jumps_to_last_race(self):
        # fake command logs its argv so we can see the --race hop
        d = tempfile.mkdtemp(prefix="stammtisch-argv-racing-")
        script = os.path.join(d, "argv-racing")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s/argv.log\necho '%s'\n"
                    % (d, json.dumps(_board_payload()).replace("'", r"'\''")))
        os.chmod(script, 0o755)
        config = mock.Mock()
        config.racing_argv = (script,)

        async def run():
            screen = RacingScreen(config)
            from textual.app import App

            class Host(App):
                def on_mount(self):
                    self.push_screen(screen)

            host = Host()
            async with host.run_test(size=(120, 40)) as pilot:
                for _ in range(20):
                    await pilot.pause()
                    await asyncio.sleep(0.05)
                    if screen.query_one("#race-meeting-table").row_count:
                        break
                event = mock.Mock()
                event.data_table = screen.query_one("#race-meeting-table")
                event.row_key.value = "2026-07-12|ST"
                screen.on_data_table_row_selected(event)
                for _ in range(20):
                    await pilot.pause()
                    await asyncio.sleep(0.05)
                return screen._requested

        requested = asyncio.run(run())
        self.assertEqual(requested, ("2026-07-12", "ST", 11))  # last race
        with open(os.path.join(d, "argv.log")) as fh:
            lines = fh.read().strip().splitlines()
        self.assertEqual(lines[-1], "--race 2026-07-12 ST 11")


if __name__ == "__main__":
    unittest.main()
