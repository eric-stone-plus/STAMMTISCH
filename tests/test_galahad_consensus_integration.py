"""Opt-in producer/consumer contract test for GALAHAD consensus bars.

The normal test suite must remain self-contained, so this cross-repository
test runs only when ``STAMMTISCH_IT=1``.  It loads the actual GALAHAD
``consensus.py`` from ``STAMMTISCH_GALAHAD_ROOT`` or, when that variable is
unset, from a sibling ``GALAHAD`` checkout.  No network or provider access is
performed.

Run from the STAMMTISCH repository with both repositories checked out::

    STAMMTISCH_IT=1 python -m unittest \
        tests.test_galahad_consensus_integration -v
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

import pandas as pd

from tui.validated_bars import ManifestLookupError, load_validated_bars


_MODULE_NAME = "_stammtisch_galahad_consensus_integration"
_MAX_PRODUCER_BYTES = 2 * 1024 * 1024


def _galahad_root() -> Path:
    configured = (os.environ.get("STAMMTISCH_GALAHAD_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[2] / "GALAHAD").resolve()


def _load_consensus_module(root: Path) -> ModuleType:
    module_path = root / "quantkit" / "quantkit" / "data" / "consensus.py"
    if not module_path.is_file():
        raise AssertionError(f"GALAHAD consensus producer is missing: {module_path}")
    if module_path.stat().st_size > _MAX_PRODUCER_BYTES:
        raise AssertionError("GALAHAD consensus producer exceeds the test size bound")
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, module_path)
    if spec is None or spec.loader is None:
        raise AssertionError("GALAHAD consensus producer cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


@unittest.skipUnless(
    os.environ.get("STAMMTISCH_IT") == "1",
    "set STAMMTISCH_IT=1 to run the GALAHAD producer integration test",
)
class GalahadConsensusIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.consensus = _load_consensus_module(_galahad_root())

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop(_MODULE_NAME, None)

    @staticmethod
    def _frame(close: float) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "open": [close - 1.0],
                "high": [close + 1.0],
                "low": [close - 2.0],
                "close": [close],
                "volume": [1_000.0],
            },
            index=pd.to_datetime(["2026-08-14"]),
        )

    def _manifest(self, symbol: str, closes: tuple[float, ...]) -> dict:
        producer = self.consensus
        identity = producer.OHLCVIdentity(
            symbol=symbol,
            market="XSHG",
            interval="1d",
            calendar="XSHG",
            currency="CNY",
        )
        security = producer.OHLCVSecurityIdentity(
            symbol=symbol,
            market="XSHG",
            currency="CNY",
        )
        sources = [
            producer.OHLCVSource(
                provider=f"provider-{position}",
                independence_group=f"group-{position}",
                price_basis="raw",
                volume_unit="shares",
                frame=self._frame(close),
                identity=security,
                input_artifact_sha256=hashlib.sha256(
                    f"{symbol}:provider-{position}".encode("utf-8")
                ).hexdigest(),
            )
            for position, close in enumerate(closes, start=1)
        ]
        tolerance = producer.ConsensusTolerance(
            price_pass=0.003,
            price_warning=0.02,
            price_quarantine=0.20,
            volume_pass=0.02,
            volume_warning=0.20,
            volume_quarantine=0.50,
        )
        return producer.build_ohlcv_consensus(
            identity,
            sources,
            sessions=["2026-08-14"],
            tolerance=tolerance,
        ).to_manifest()

    def test_actual_galahad_manifests_are_verified_by_the_store(self) -> None:
        manifests = {
            "accepted": self._manifest("931743.CSI", (100.0, 100.1, 99.9)),
            "partial": self._manifest("932040.CSI", (100.0, 100.2, 180.0)),
            "quarantined": self._manifest("H30184.CSI", (100.0, 106.0)),
        }
        self.assertEqual(
            {name: name for name in manifests},
            {name: manifest["status"] for name, manifest in manifests.items()},
        )

        with tempfile.TemporaryDirectory(prefix="stammtisch-galahad-it-") as raw_root:
            store = Path(raw_root)
            for name, manifest in manifests.items():
                payload = json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                self.assertLessEqual(len(payload), 256 * 1024)
                (store / f"{name}.json").write_bytes(payload)

            accepted = load_validated_bars(
                store, "931743.CSI", market="XSHG"
            )
            self.assertEqual("accepted", accepted.manifest["status"])
            self.assertEqual(1, len(accepted.records))

            for symbol, status in (
                ("932040.CSI", "partial"),
                ("H30184.CSI", "quarantined"),
            ):
                with self.subTest(status=status):
                    with self.assertRaisesRegex(
                        ManifestLookupError, "is not accepted"
                    ):
                        load_validated_bars(store, symbol, market="XSHG")
                    loaded = load_validated_bars(
                        store,
                        symbol,
                        market="XSHG",
                        accepted_only=False,
                    )
                    self.assertEqual(status, loaded.manifest["status"])

            partial_sources = manifests["partial"]["diagnostics"][0]["sources"]
            excluded = next(
                source for source in partial_sources if source["status"] == "excluded"
            )
            self.assertEqual(
                ["beyond_quarantine_band", "group_outlier"],
                excluded["reason_codes"],
            )


if __name__ == "__main__":
    unittest.main()
