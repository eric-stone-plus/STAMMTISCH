"""Tests for `stammtisch config` (tui/config_cli.py) — stdlib unittest, no deps."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tui.config import DEFAULT_CONFIG, Config, default_config_file
from tui import config_cli


def run_cli(*args: str) -> tuple[int, str, str]:
    """Run config_cli.main with args, return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = config_cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


class ConfigCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        self.env_patch = mock.patch.dict(
            os.environ, {"STAMMTISCH_CONFIG": self.cfg_path}, clear=False
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_default_config_file_env_override(self) -> None:
        self.assertEqual(str(default_config_file()), self.cfg_path)

    def test_show_masks_api_key(self) -> None:
        Config().set("ai_api_key", "sk-test-abcdefgh")
        code, out, _ = run_cli()
        self.assertEqual(code, 0)
        self.assertIn("ai_api_key             sk-t********efgh", out)
        self.assertNotIn("abcdefgh", out)

    def test_set_key_writes_0600_file(self) -> None:
        code, out, _ = run_cli("set-key", "sk-1234567890")
        self.assertEqual(code, 0)
        self.assertIn("saved", out)
        self.assertEqual(oct(os.stat(self.cfg_path).st_mode)[-3:], "600")
        with open(self.cfg_path) as f:
            self.assertEqual(json.load(f)["ai_api_key"], "sk-1234567890")

    def test_set_key_prompts_when_omitted(self) -> None:
        with mock.patch.object(config_cli, "_prompt_secret", return_value="sk-prompted"):
            code, _, _ = run_cli("set-key")
        self.assertEqual(code, 0)
        self.assertEqual(Config().get("ai_api_key"), "sk-prompted")

    def test_set_key_empty_is_usage_error(self) -> None:
        with mock.patch.object(config_cli, "_prompt_secret", return_value=""):
            code, _, err = run_cli("set-key")
        self.assertEqual(code, 2)
        self.assertIn("no key", err)

    def test_unset_key(self) -> None:
        Config().set("ai_api_key", "sk-1234567890")
        code, _, _ = run_cli("unset-key")
        self.assertEqual(code, 0)
        self.assertIsNone(Config().ai_api_key)

    def test_set_int_coercion(self) -> None:
        code, _, _ = run_cli("set", "default_fast", "30")
        self.assertEqual(code, 0)
        self.assertEqual(Config().get("default_fast"), 30)

        code, _, _ = run_cli("set", "intake_timeout_seconds", "1200")
        self.assertEqual(code, 0)
        self.assertEqual(Config().intake_timeout_seconds, 1200)

    def test_set_bad_int_is_usage_error(self) -> None:
        code, _, err = run_cli("set", "default_fast", "abc")
        self.assertEqual(code, 2)
        self.assertIn("integer", err)

    def test_set_unknown_key_rejected(self) -> None:
        code, _, err = run_cli("set", "bogus_key", "x")
        self.assertEqual(code, 2)
        self.assertIn("unknown key", err)

    def test_egress_rotation_keys_round_trip(self) -> None:
        code, _, _ = run_cli("set", "egress_proxy_url", "http://proxy.example:8080")
        self.assertEqual(code, 0)
        code, _, _ = run_cli("set", "egress_switch_cmd", "switch --for-site {host}")
        self.assertEqual(code, 0)
        cfg = Config()
        self.assertEqual(cfg.egress_proxy_url, "http://proxy.example:8080")
        self.assertEqual(cfg.egress_switch_cmd, "switch --for-site {host}")
        self.assertEqual(cfg.data_proxy_url, "")

    def test_set_list_key_splits_commas(self) -> None:
        code, _, _ = run_cli("set", "recent_symbols", "AAPL, MSFT,TSLA")
        self.assertEqual(code, 0)
        self.assertEqual(Config().get("recent_symbols"), ["AAPL", "MSFT", "TSLA"])

    def test_get_and_unset(self) -> None:
        Config().set("ai_api_key", "sk-1234567890")
        code, out, _ = run_cli("get", "ai_api_key")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "sk-1*****7890")

        code, _, _ = run_cli("unset", "default_slow")
        self.assertEqual(code, 0)
        self.assertEqual(Config().get("default_slow"), DEFAULT_CONFIG["default_slow"])

    def test_path_and_unknown_subcommand(self) -> None:
        code, out, _ = run_cli("path")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), self.cfg_path)

        code, _, err = run_cli("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("unknown subcommand", err)

    def test_env_key_is_not_persisted(self) -> None:
        """DEEPSEEK_API_KEY overrides at load time but is never written to disk."""
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-from-env"}, clear=False):
            cfg = Config()
            self.assertEqual(cfg.ai_api_key, "sk-from-env")
            cfg.set("ai_api_key", "sk-explicit")
        with open(self.cfg_path) as f:
            self.assertEqual(json.load(f)["ai_api_key"], "sk-explicit")
        with open(self.cfg_path) as handle:
            self.assertNotIn("sk-from-env", handle.read())

    def test_update_batch_persists_once(self) -> None:
        cfg = Config()
        cfg.update({"ai_api_key": "sk-batch", "default_fast": 42})
        with open(self.cfg_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["ai_api_key"], "sk-batch")
        self.assertEqual(saved["default_fast"], 42)

    def test_daily_intake_config_is_device_neutral_and_env_overridable(self) -> None:
        cfg = Config()
        self.assertFalse(cfg.intake_argv)
        self.assertIn("stammtisch", cfg.workspace_root)
        private_marker = "".join(("Documents/", "Private"))
        self.assertNotIn(private_marker, cfg.workspace_root)

        with mock.patch.dict(os.environ, {
            "STAMMTISCH_INTAKE_CMD": "daily-product --profile market",
            "STAMMTISCH_WORKSPACE_ROOT": self.tmp.name,
        }, clear=False):
            env_cfg = Config()
        self.assertEqual(env_cfg.intake_argv, ("daily-product", "--profile", "market"))
        self.assertEqual(env_cfg.workspace_root, self.tmp.name)
        self.assertIn("intake_cmd", env_cfg._from_env)
        self.assertIn("workspace_root", env_cfg._from_env)

    def test_polymarket_proxy_is_explicit_and_env_overridable(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.get("polymarket_proxy_url"), "")

        with mock.patch.dict(os.environ, {
            "STAMMTISCH_POLYMARKET_PROXY": "https://proxy.example",
        }):
            env_cfg = Config()
        self.assertEqual(
            env_cfg.get("polymarket_proxy_url"), "https://proxy.example"
        )
        self.assertIn("polymarket_proxy_url", env_cfg._from_env)

    def test_ohlcv_source_defaults_live_and_has_explicit_env_overrides(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["STAMMTISCH_CONFIG"] = self.cfg_path
            defaults = Config()
        self.assertEqual("live", defaults.ohlcv_mode)
        self.assertEqual("", defaults.validated_bars_root)

        with mock.patch.dict(
            os.environ,
            {
                "STAMMTISCH_OHLCV_MODE": "VALIDATED",
                "STAMMTISCH_VALIDATED_BARS_ROOT": self.tmp.name,
            },
            clear=False,
        ):
            configured = Config()
        self.assertEqual("validated", configured.ohlcv_mode)
        self.assertEqual(self.tmp.name, configured.validated_bars_root)
        self.assertIn("ohlcv_mode", configured._from_env)
        self.assertIn("validated_bars_root", configured._from_env)

    def test_invalid_ohlcv_mode_is_rejected(self) -> None:
        code, _, err = run_cli("set", "ohlcv_mode", "best-effort")
        self.assertEqual(2, code)
        self.assertIn("ohlcv_mode requires", err)

        with mock.patch.dict(
            os.environ, {"STAMMTISCH_OHLCV_MODE": "best-effort"}, clear=False
        ):
            with self.assertRaisesRegex(ValueError, "live.*validated"):
                Config().ohlcv_mode

    def test_unknown_saved_keys_are_not_carried_forward(self) -> None:
        with open(self.cfg_path, "w") as handle:
            json.dump({"default_fast": 31, "obsolete_private_key": "discard"}, handle)
        cfg = Config()
        self.assertEqual(cfg.default_fast, 31)
        self.assertNotIn("obsolete_private_key", cfg._data)
        cfg.save()
        with open(self.cfg_path) as handle:
            self.assertNotIn("obsolete_private_key", json.load(handle))


if __name__ == "__main__":
    unittest.main()
