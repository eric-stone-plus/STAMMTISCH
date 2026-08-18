"""Domain board driver tests — subprocess + JSON contract, fail closed."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest

from tui.domaindata import (DomainDriver, validate_board, validate_spval_board,
                            validate_spval_board_v2)


VALID_BOARD = {
    "ok": True,
    "schema": "mktdaily.sgx-board.v1",
    "asof": "2026-08-14",
    "source": "SGX daily settlement file (links.sgx.com derivatives-daily)",
    "instruments": [
        {
            "code": "CWF",
            "group": "FFA time charter",
            "name": "Capesize 5TC basket",
            "unit": "USD/day",
            "front_month": "2026-08",
            "settle": 39343.0,
            "volume": 847.0,
            "open_interest": 1000.0,
            "change": 143.0,
            "change_pct": 0.36,
            "curve": [{"month": "2026-08", "settle": 39343.0}],
            "recent": [{"date": "2026-08-14", "settle": 39343.0}],
        }
    ],
    "warnings": [],
}


def _script(body: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    )
    handle.write(textwrap.dedent(body))
    handle.close()
    return handle.name


class ValidateBoardTest(unittest.TestCase):
    def test_valid_board_passes(self) -> None:
        self.assertIsNone(validate_board(VALID_BOARD))

    def test_extra_keys_pass(self) -> None:
        payload = json.loads(json.dumps(VALID_BOARD))
        payload["days_cached"] = 31
        payload["instruments"][0]["anything"] = {"future": True}
        self.assertIsNone(validate_board(payload))

    def test_wrong_schema_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_BOARD))
        payload["schema"] = "something-else.v9"
        self.assertIsNotNone(validate_board(payload))

    def test_non_finite_settle_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_BOARD))
        payload["instruments"][0]["settle"] = "39343.0"
        self.assertIsNotNone(validate_board(payload))

    def test_null_change_allowed(self) -> None:
        payload = json.loads(json.dumps(VALID_BOARD))
        payload["instruments"][0]["change"] = None
        payload["instruments"][0]["change_pct"] = None
        self.assertIsNone(validate_board(payload))

    def test_malformed_curve_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_BOARD))
        payload["instruments"][0]["curve"] = [{"month": "2026-08"}]
        self.assertIsNotNone(validate_board(payload))

    def test_not_a_dict_rejected(self) -> None:
        self.assertIsNotNone(validate_board([1, 2, 3]))


class DomainDriverTest(unittest.TestCase):
    def test_no_command_fails_closed(self) -> None:
        result = DomainDriver(()).board()
        self.assertFalse(result["ok"])
        self.assertIn("shipping_cmd", result["error"])

    def test_valid_board_roundtrip(self) -> None:
        script = _script(
            f"""
            import json
            print(json.dumps({VALID_BOARD!r}))
            """
        )
        self.addCleanup(os.unlink, script)
        result = DomainDriver((sys.executable, script)).board()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["asof"], "2026-08-14")
        self.assertEqual(result["instruments"][0]["code"], "CWF")

    def test_non_json_output_fails_closed(self) -> None:
        script = _script("print('not json at all')\n")
        self.addCleanup(os.unlink, script)
        result = DomainDriver((sys.executable, script)).board()
        self.assertFalse(result["ok"])
        self.assertIn("not JSON", result["error"])

    def test_nonzero_exit_fails_closed(self) -> None:
        script = _script(
            """
            import json, sys
            print(json.dumps({"ok": False, "error": "upstream down"}))
            sys.exit(1)
            """
        )
        self.addCleanup(os.unlink, script)
        result = DomainDriver((sys.executable, script)).board()
        self.assertFalse(result["ok"])
        self.assertIn("upstream down", result["error"])

    def test_schema_mismatch_fails_closed(self) -> None:
        script = _script('print(json.dumps({"ok": True, "schema": "x", "asof": "2026-08-14", "instruments": []}))\n')
        self.addCleanup(os.unlink, script)
        result = DomainDriver((sys.executable, script)).board()
        self.assertFalse(result["ok"])
        self.assertIn("schema", result["error"])

    def test_missing_binary_fails_closed(self) -> None:
        result = DomainDriver(("/nonexistent/binary", "--json")).board()
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])


VALID_SPVAL_BOARD = {
    "ok": True,
    "schema": "stammtisch.spval-board.v1",
    "asof": "2026-07-26",
    "source": "test fixture",
    "baseline": {"price": 12000000.0, "scenario": "B"},
    "kpis": {"ret_med": 2.9, "ret_p5": -60.5, "ret_p95": 85.7,
             "p_loss": 47.3, "es10": -63.0, "irr_med_pct": 0.5,
             "payback_med_yr": 8.0, "ebitda_y1_med": 1754865.0},
    "grid": [{"key": "A@11M", "scenario": "A", "price": 11000000.0,
              "ret_med": 4.7, "ret_p5": -70.0, "p_loss": 46.3,
              "es10": -73.7, "irr_med_pct": 0.8}],
    "maxbid": {"m1": 0.85, "m2": 1.05, "m3": 1.0, "m4": 0.85, "m5": 0.6,
               "pim": 0.455175, "base": 18000000.0, "d1": 500000.0,
               "d2": 2890000.0, "value": 4803150.0},
    "greeks": [{"factor": "TCE ±10%", "down": -23.0, "up": 23.0}],
}


class ValidateSpvalBoardTest(unittest.TestCase):
    def test_valid_board_passes(self) -> None:
        self.assertIsNone(validate_spval_board(VALID_SPVAL_BOARD))

    def test_extra_keys_pass(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD))
        payload["future_field"] = {"anything": True}
        self.assertIsNone(validate_spval_board(payload))

    def test_wrong_schema_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD))
        payload["schema"] = "mktdaily.sgx-board.v1"
        self.assertIsNotNone(validate_spval_board(payload))

    def test_bad_kpi_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD))
        payload["kpis"]["ret_med"] = "2.9"
        self.assertIsNotNone(validate_spval_board(payload))

    def test_empty_grid_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD))
        payload["grid"] = []
        self.assertIsNotNone(validate_spval_board(payload))

    def test_bad_maxbid_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD))
        payload["maxbid"]["value"] = None
        self.assertIsNotNone(validate_spval_board(payload))

    def test_bad_greeks_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD))
        payload["greeks"] = [{"factor": "X"}]
        self.assertIsNotNone(validate_spval_board(payload))


class DomainDriverSpvalTest(unittest.TestCase):
    def test_spval_roundtrip_with_validator(self) -> None:
        script = _script(
            f"""
            import json
            print(json.dumps({VALID_SPVAL_BOARD!r}))
            """
        )
        self.addCleanup(os.unlink, script)
        result = DomainDriver((sys.executable, script),
                              validator=validate_spval_board).board()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["maxbid"]["value"], 4803150.0)

    def test_default_validator_still_sgx(self) -> None:
        script = _script(
            f"""
            import json
            print(json.dumps({VALID_SPVAL_BOARD!r}))
            """
        )
        self.addCleanup(os.unlink, script)
        result = DomainDriver((sys.executable, script)).board()
        self.assertFalse(result["ok"])
        self.assertIn("schema", result["error"])


class DomainDriverSpvalTest(unittest.TestCase):
    def test_spval_roundtrip_with_validator(self) -> None:
        script = _script(
            f"""
            import json
            print(json.dumps({VALID_SPVAL_BOARD!r}))
            """
        )
        self.addCleanup(os.unlink, script)
        result = DomainDriver((sys.executable, script),
                              validator=validate_spval_board).board()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["maxbid"]["value"], 4803150.0)

    def test_default_validator_still_sgx(self) -> None:
        script = _script(
            f"""
            import json
            print(json.dumps({VALID_SPVAL_BOARD!r}))
            """
        )
        self.addCleanup(os.unlink, script)
        result = DomainDriver((sys.executable, script)).board()
        self.assertFalse(result["ok"])
        self.assertIn("schema", result["error"])


VALID_SPVAL_BOARD_V2 = dict(VALID_SPVAL_BOARD, **{
    "schema": "stammtisch.spval-board.v2",
    "market": {
        "cycle_annual_tc": {"2007": 54135, "2026": 16304},
        "route_last24m": {"2026-07-31": 11961},
        "tc_stats": {"med": 13000.0},
        "scrap_premium": {"last": 2.3},
        "vol_by_year": {"2026": 3372},
        "ou": {"half_life_weeks": 9.6},
    },
    "risk": {
        "tail_matrix": [
            {"key": "A@11M", "ret_med": 4.7, "ret_p5": -70.0,
             "ret_p95": 100.0, "p_loss": 46.3, "es10": -73.7,
             "irr_med_pct": 0.8},
        ],
        "counterfactual": {"base": -6},
        "sens_matrix": {"12": {"7000": -75}},
    },
})


class ValidateSpvalBoardV2Test(unittest.TestCase):
    def test_valid_v2_passes(self) -> None:
        self.assertIsNone(validate_spval_board_v2(VALID_SPVAL_BOARD_V2))

    def test_v1_schema_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD_V2))
        payload["schema"] = "stammtisch.spval-board.v1"
        self.assertIsNotNone(validate_spval_board_v2(payload))

    def test_missing_market_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD_V2))
        del payload["market"]
        self.assertIsNotNone(validate_spval_board_v2(payload))

    def test_empty_tail_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD_V2))
        payload["risk"]["tail_matrix"] = []
        self.assertIsNotNone(validate_spval_board_v2(payload))

    def test_bad_sens_rejected(self) -> None:
        payload = json.loads(json.dumps(VALID_SPVAL_BOARD_V2))
        payload["risk"]["sens_matrix"]["12"]["7000"] = "x"
        self.assertIsNotNone(validate_spval_board_v2(payload))

    def test_v2_roundtrip_with_validator(self) -> None:
        script = _script(
            f"""
            import json
            print(json.dumps({VALID_SPVAL_BOARD_V2!r}))
            """
        )
        self.addCleanup(os.unlink, script)
        result = DomainDriver((sys.executable, script),
                              validator=validate_spval_board_v2).board()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["risk"]["tail_matrix"][0]["key"], "A@11M")


if __name__ == "__main__":
    unittest.main()
