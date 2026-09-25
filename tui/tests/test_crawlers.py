"""Crawler panel unit tests: source parsing/toggling, offline only."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tui import crawlers
from tui.crawlers import _fit, parse_sources, toggle_source

CONF = """\
# SCOPE annotation line
# 格式: phase|folder|name|url   （| 分隔，无空格；# 开头为注释）
domestic|fin-ashare|eastmoney|https://www.eastmoney.com/
domestic|fin-ashare|sina|https://finance.sina.com.cn/
#overseas|fin-hk|nikkei|https://www.nikkei.com/markets/
overseas|fin-us|cnbc|https://www.cnbc.com/markets/
"""


class ParseSourcesTest(unittest.TestCase):
    def test_parse_marks_enabled_and_toggleable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.conf"
            path.write_text(CONF, encoding="utf-8")
            entries = parse_sources(str(path))
        toggleable = [e for e in entries if e["toggleable"]]
        self.assertEqual(len(entries), 6)
        self.assertEqual(len(toggleable), 4)
        # The commented-out nikkei row stays a toggleable, disabled source.
        states = {(e["name"], e["enabled"]) for e in toggleable}
        self.assertIn(("eastmoney", True), states)
        self.assertIn(("nikkei", False), states)
        # Annotation prose — including the header comment whose format
        # description contains four |-separated words — is never a source.
        annotations = [e for e in entries if not e["toggleable"]]
        self.assertEqual(len(annotations), 2)
        self.assertIn("SCOPE", annotations[0]["name"])
        self.assertIn("格式", annotations[1]["name"])

    def test_toggle_round_trip_preserves_other_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.conf"
            path.write_text(CONF, encoding="utf-8")
            # Disable cnbc (line index 5).
            self.assertTrue(toggle_source(5, str(path)))
            after = path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(after[5].lstrip().startswith("#"))
            self.assertIn("cnbc", after[5])
            # Annotation and other rows are untouched.
            self.assertEqual(after[0], "# SCOPE annotation line")
            self.assertIn("格式", after[1])
            self.assertEqual(after[2].split("|")[2], "eastmoney")
            # Re-enable restores the exact original line.
            self.assertTrue(toggle_source(5, str(path)))
            self.assertEqual(
                path.read_text(encoding="utf-8"), CONF
            )

    def test_toggle_refuses_prose_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.conf"
            path.write_text(CONF, encoding="utf-8")
            self.assertFalse(toggle_source(0, str(path)), "annotation is not a source")
            self.assertFalse(toggle_source(1, str(path)), "header prose is not a source")
            self.assertFalse(toggle_source(99, str(path)))
            self.assertFalse(toggle_source(0, str(Path(tmp) / "missing.conf")))

    def test_fit_collapses_and_truncates(self) -> None:
        self.assertEqual(_fit("  a   b  ", 10), "a b")
        long = "x" * 80
        self.assertEqual(len(_fit(long, 20)), 20)
        self.assertTrue(_fit(long, 20).endswith("…"))


class ContainerCountsTest(unittest.TestCase):
    """The header counts must describe the COMPOSE PROJECT, not every
    rootless container on the host (cross-attack M1 after the firecrawl
    restoration: a bare `podman ps` rendered 6/23 where 17 were foreign
    exited containers)."""

    def test_counts_are_scoped_to_the_compose_project(self) -> None:
        cmds: list[list[str]] = []

        def fake_run(cmd, cwd=None, timeout=90.0):
            cmds.append(cmd)
            return 0, "firecrawl-api-1\nfirecrawl-redis-1"

        with mock.patch.object(crawlers, "_run", side_effect=fake_run):
            counts = crawlers.container_counts("/srv/tools/firecrawl")
        self.assertEqual(counts, (2, 2))
        self.assertEqual(len(cmds), 2)
        for cmd in cmds:
            self.assertIn("label=com.docker.compose.project=firecrawl", cmd)
        self.assertNotIn("-a", cmds[0])  # running pass
        self.assertIn("-a", cmds[1])  # total pass

    def test_unconfigured_dir_counts_nothing_and_spawns_nothing(self) -> None:
        with mock.patch.object(crawlers, "_run",
                               side_effect=AssertionError("must not spawn")):
            self.assertEqual(crawlers.container_counts(""), (0, 0))


@unittest.skipUnless(crawlers.CrawlerPanelScreen is not None,
                     "panel tests need textual")
class ConfirmGateTest(unittest.TestCase):
    """An armed panel (live compose dir) must not let a stray keypress
    reach the stack: [S]/[T]/[R] open a confirm gate; Cancel/Esc refuses
    with a notice; only Confirm runs the op (cross-attack M2)."""

    def _config(self):
        # Mock, not a plain dict: _snapshot also reads attribute-style
        # config (intake_argv), matching the racing tests' harness.
        cfg = {
            "crawler_compose_dir": "/srv/tools/firecrawl",
            "crawler_url": "http://127.0.0.1:1/",  # never probed: patched
        }
        config = mock.Mock()
        config.get = lambda key, default="": cfg.get(key, default)
        config.intake_argv = None
        return config

    def _mount(self):
        screen = crawlers.CrawlerPanelScreen(config=self._config())
        from textual.app import App

        class Host(App):
            def on_mount(self):
                self.push_screen(screen)

        return Host(), screen

    def test_destructive_keys_are_gated_and_cancel_refuses(self) -> None:
        async def run():
            host, screen = self._mount()
            cmds: list[list[str]] = []

            def fake_run(cmd, cwd=None, timeout=90.0):
                cmds.append(cmd)
                return 0, ""

            with mock.patch.object(crawlers, "_run", side_effect=fake_run), \
                 mock.patch.object(crawlers, "probe_endpoint",
                                   return_value=(True, 1)), \
                 mock.patch.object(crawlers, "watch_timer_active",
                                   return_value=True):
                async with host.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    for key in ("s", "t", "r"):
                        await pilot.press(key)
                        await pilot.pause()
                        self.assertIsInstance(host.screen,
                                              crawlers.ConfirmOpScreen)
                        # fail-closed focus: Enter on the fresh modal
                        # hits Cancel, never Confirm
                        focused = host.screen.focused
                        self.assertIsNotNone(focused)
                        self.assertEqual(focused.id, "confirm-op-no")
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIs(host.screen, screen)
                    # nothing stack-affecting ran: no `podman compose …`,
                    # no `podman restart …`, no timer enable/disable.
                    # Shape-matched on argv, not substrings: the refresh
                    # path's own `--filter label=com.docker.compose.…`
                    # contains the word "compose".
                    podman_cmds = [c for c in cmds if c and c[0] == "podman"]
                    self.assertFalse(
                        any(len(c) > 1 and c[1] == "compose"
                            for c in podman_cmds), cmds)
                    self.assertFalse(
                        any(len(c) > 1 and c[1] == "restart"
                            for c in podman_cmds), cmds)
                    systemctl_cmds = [c for c in cmds
                                      if c and c[0] == "systemctl"]
                    self.assertFalse(
                        any(("disable" in c or "enable" in c)
                            for c in systemctl_cmds), cmds)
                    notes = [n.message for n in host._notifications]
                    self.assertTrue(any("cancelled" in m for m in notes), notes)

        asyncio.run(run())

    def test_confirm_runs_the_gated_op(self) -> None:
        async def run():
            host, _screen = self._mount()
            cmds: list[list[str]] = []

            def fake_run(cmd, cwd=None, timeout=90.0):
                cmds.append(cmd)
                return 0, ""

            with mock.patch.object(crawlers, "_run", side_effect=fake_run), \
                 mock.patch.object(crawlers, "probe_endpoint",
                                   return_value=(True, 1)):
                async with host.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("s")
                    await pilot.pause()
                    self.assertIsInstance(host.screen,
                                          crawlers.ConfirmOpScreen)
                    await pilot.click("#confirm-op-yes")
                    for _ in range(40):
                        await pilot.pause()
                        await asyncio.sleep(0.05)
                        if any("stop" in c for c in cmds):
                            break
                    self.assertIn(
                        ["podman", "compose", "stop",
                         *crawlers.STACK_SERVICES],
                        cmds,
                    )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
