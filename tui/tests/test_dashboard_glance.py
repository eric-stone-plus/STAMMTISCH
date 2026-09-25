"""Dashboard glance pin — the Round E residual risk, closed.

E-1 (M7 true merge) broke the glance worker's function-level import
(``from .. import livefeed`` after ``livefeed`` moved to ``services/``);
``_run_async`` degraded the ImportError into an error result and the
sidebar silently rendered empty — every suite stayed green because
nothing asserted glance rows. This test runs the REAL ``_glance_tick``
path headless with offline stubs for the two network legs: if any import
or fetch leg of the worker breaks again, the sidebar falls back to
``(no data)`` and this pin goes red (negative self-check exercised: the
E-1 import shape fails it, the fixed shape passes).

What it pins beyond liveness: the quote rows carry the change percentage
against ``prev_close``, and the ``src:`` line carries the PER-QUOTE
sources that served the batch (the stubbed tencent/yahoo legs). The old
tree's unconditional ``ccidx``/``fin-daily`` suffix is grandfathered
behavior and is deliberately NOT pinned as honest — the interface twin
fixed exactly that in M4 (FIXES.md); this tree is retired-track.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from textual.widgets import Static

from tui.app import StammtischTUI
from tui.screens import DashboardScreen

# Operator shells really export API keys; headless app boots must resolve
# their AI credentials from the fixture config, not the host environment
# (same hygiene as test_tui_smoke).
_ENV_HYGIENE_KEYS = (
    "QIANWEN_TP_PERSONAL_KEY", "ANTHROPIC_API_KEY", "XIAOMI_API_KEY",
    "GLM_API_KEY", "ZHIPU_API_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_KEY",
    "DEEPSEEK_TOKEN", "EIA_API_KEY",
)

#: Canned fetch_batch result — the exact shape _deliver consumes:
#: last / prev_close / source per symbol.
_QUOTES = {
    "000001.SS": {"last": 3100.0, "prev_close": 3000.0,
                  "source": "tencent fixture"},
    "HSI": {"last": 18000.0, "prev_close": 18500.0,
            "source": "tencent fixture"},
    "QQQ": {"last": 480.0, "prev_close": 470.0, "source": "yahoo fixture"},
}


class DashboardGlanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_key_env = {
            k: os.environ[k] for k in _ENV_HYGIENE_KEYS if k in os.environ
        }
        for key in self._saved_key_env:
            del os.environ[key]

    def tearDown(self) -> None:
        os.environ.update(self._saved_key_env)

    def test_glance_worker_renders_rows_and_provenance_offline(self) -> None:
        # Fixture config is written synchronously: the async scenario must
        # carry no blocking file I/O (ruff ASYNC230).
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({"state_root": os.path.join(tmp, "state")}, f)
            asyncio.run(self._scenario(tmp, cfg_path))

    async def _scenario(self, tmp: str, cfg_path: str) -> None:
        with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}), \
                mock.patch("services.livefeed.fetch_batch",
                           return_value=dict(_QUOTES)) as fetch, \
                mock.patch("tui.ccifeed.feed",
                           return_value=SimpleNamespace(snapshot={})):
            app = StammtischTUI(binary="/nonexistent/stammtisch-core",
                                skip_boot=True)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                dashboard = app.screen
                self.assertIsInstance(dashboard, DashboardScreen)
                glance = dashboard.query_one("#dash-glance", Static)
                self.assertNotIn("SH COMP", str(glance.render()))
                # The REAL worker path: thread + call_from_thread
                # delivery, exactly as the 30s interval drives it.
                dashboard._glance_tick()
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    await pilot.pause()
                    if "SH COMP" in str(glance.render()):
                        break
                text = str(glance.render())
                fetch.assert_called_once()
                self.assertIn("SH COMP", text)
                self.assertIn("HSI", text)
                self.assertIn("QQX(US)", text)
                # The change percentage renders against prev_close.
                self.assertIn("+3.33%", text)
                self.assertIn("-2.70%", text)
                # Provenance: the per-quote sources that served this batch.
                self.assertIn("src:", text)
                self.assertIn("tencent", text)
                self.assertIn("yahoo", text)
                # A broken worker degrades to this — never accept it.
                self.assertNotIn("(no data)", text)


if __name__ == "__main__":
    unittest.main()
