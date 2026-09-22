"""Brokers — gate, refusal rules, signing, filters, parsing (offline)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.brokers import AlpacaBroker, BinanceBroker, BrokerRefused
from services.brokers.gate import ensure_trading_allowed, trading_mode
from services.brokers.keys import alpaca_credentials, binance_credentials, parse_env_file


def _config(trading_mode: str = "", alpaca_env: str = "", binance_env: str = "",
            env: dict[str, str] | None = None):
    data = {"trading_mode": trading_mode, "alpaca_env_file": alpaca_env,
            "binance_env_file": binance_env}
    cfg = SimpleNamespace()
    cfg.get = lambda key, default=None: data.get(key, default)
    return cfg


class GateTest(unittest.TestCase):
    def test_closed_by_default(self):
        self.assertEqual(trading_mode(_config()), "")
        with self.assertRaises(BrokerRefused):
            ensure_trading_allowed(_config(), "order placement")

    def test_paper_mode_opens(self):
        self.assertEqual(trading_mode(_config("paper")), "paper")
        ensure_trading_allowed(_config("paper"), "order placement")  # no raise

    def test_unknown_mode_stays_closed(self):
        self.assertEqual(trading_mode(_config("live")), "")
        self.assertEqual(trading_mode(_config("PAPER")), "paper")


class MainnetRefusalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: v for k, v in __import__("os").environ.items()
                       if "ALPACA" in k or "APCA" in k or "BINANCE" in k}
        for key in self._saved:
            del __import__("os").environ[key]

    def tearDown(self) -> None:
        __import__("os").environ.update(self._saved)

    def test_alpaca_refuses_non_paper_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "alpaca.env"
            env_file.write_text(
                "ALPACA_PAPER_API_KEY=keyid\nALPACA_PAPER_API_SECRET=secret\n")
            cfg = _config("paper", alpaca_env=str(env_file))
            broker = AlpacaBroker(cfg)  # paper base constructs fine
            self.assertEqual(broker.base, "https://paper-api.alpaca.markets")
            with self.assertRaises(BrokerRefused):
                AlpacaBroker(cfg, base="https://api.alpaca.markets")

    def test_binance_refuses_non_testnet_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "binance.env"
            env_file.write_text(
                "BINANCE_TESTNET_API_KEY=key\nBINANCE_TESTNET_API_SECRET=secret\n")
            cfg = _config("paper", binance_env=str(env_file))
            broker = BinanceBroker(cfg)
            self.assertEqual(broker.base, "https://testnet.binance.vision")
            with self.assertRaises(BrokerRefused):
                BinanceBroker(cfg, base="https://api.binance.com")

    def test_binance_futures_refuses_non_testnet_base(self):
        from services.brokers.binance import BinanceFuturesBroker

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "binance.env"
            env_file.write_text(
                "BINANCE_TESTNET_API_KEY=key\nBINANCE_TESTNET_API_SECRET=secret\n")
            cfg = _config("paper", binance_env=str(env_file))
            broker = BinanceFuturesBroker(cfg)
            self.assertEqual(broker.base,
                             "https://testnet.binancefuture.com")
            with self.assertRaises(BrokerRefused):
                BinanceFuturesBroker(cfg, base="https://fapi.binance.com")

    def test_binance_broker_factory_by_kind(self):
        from services.brokers.binance import BinanceFuturesBroker, binance_broker

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "binance.env"
            env_file.write_text(
                "BINANCE_TESTNET_API_KEY=key\nBINANCE_TESTNET_API_SECRET=secret\n")
            spot_cfg = _config("paper", binance_env=str(env_file))
            spot_cfg.get = lambda key, default=None: (
                "futures" if key == "binance_testnet_kind" else
                {"trading_mode": "paper", "binance_env_file": str(env_file)}.get(key, default))
            self.assertIsInstance(binance_broker(spot_cfg), BinanceFuturesBroker)


class KeysTest(unittest.TestCase):
    def setUp(self) -> None:
        import os

        self._saved = {k: v for k, v in os.environ.items()
                       if "ALPACA" in k or "APCA" in k or "BINANCE" in k}
        for key in self._saved:
            del os.environ[key]

    def tearDown(self) -> None:
        import os

        os.environ.update(self._saved)

    def test_env_first_then_env_file(self):
        import os

        os.environ["ALPACA_PAPER_API_KEY"] = "env-key"
        os.environ["ALPACA_PAPER_API_SECRET"] = "env-secret"
        creds = alpaca_credentials(_config())
        self.assertEqual(creds.key, "env-key")

    def test_env_file_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "binance.env"
            env_file.write_text(
                '# testnet creds\nexport BINANCE_TESTNET_API_KEY="tk"\n'
                "BINANCE_TESTNET_API_SECRET='ts'\n")
            creds = binance_credentials(_config(binance_env=str(env_file)))
            self.assertEqual(creds.key, "tk")
            self.assertEqual(creds.secret, "ts")

    def test_missing_credentials_refuse(self):
        with self.assertRaises(BrokerRefused):
            alpaca_credentials(_config())


class BinanceSigningTest(unittest.TestCase):
    def _broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "b.env"
            env_file.write_text(
                "BINANCE_TESTNET_API_KEY=testkey\n"
                "BINANCE_TESTNET_API_SECRET=testsecret\n")
            return BinanceBroker(_config("paper", binance_env=str(env_file)))

    def test_signature_is_deterministic_hmac(self):
        broker = self._broker()
        # Known HMAC-SHA256 vector for the exact query string.
        self.assertEqual(
            broker._sign("symbol=BTCUSDT&side=BUY"),
            __import__("hmac").new(
                b"testsecret", b"symbol=BTCUSDT&side=BUY",
                __import__("hashlib").sha256).hexdigest())

    def test_round_step_truncates_down(self):
        self.assertEqual(BinanceBroker._round_step(123.4567, 0.01), "123.45")
        self.assertEqual(BinanceBroker._round_step(0.001234, 0.0001), "0.0012")
        self.assertEqual(BinanceBroker._round_step(5.0, 1.0), "5")

    def test_conforming_rejects_below_notional(self):
        broker = self._broker()
        with mock.patch.object(broker, "_symbol_filters",
                               return_value={"tick": 0.01, "step": 0.00001,
                                             "min_qty": 0.0001,
                                             "min_notional": 100.0}):
            with self.assertRaises(BrokerRefused):
                broker.conforming("BTCUSDT", 0.001, 20000.0)  # $20 notional

    def test_conforming_rounds_to_filters(self):
        broker = self._broker()
        with mock.patch.object(broker, "_symbol_filters",
                               return_value={"tick": 0.01, "step": 0.00001}):
            qty, price = broker.conforming("BTCUSDT", 0.1234567, 29999.999)
            self.assertEqual(qty, "0.12345")
            self.assertEqual(price, "29999.99")

    def test_futures_notional_key_variant_is_read(self):
        from services.brokers.binance import _extract_filters

        filters = _extract_filters([
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "50.0"},
        ])
        self.assertEqual(filters["min_notional"], 50.0)
        spot = _extract_filters([
            {"filterType": "MIN_NOTIONAL", "minNotional": "5.0"},
        ])
        self.assertEqual(spot["min_notional"], 5.0)

    def test_orders_refused_when_gate_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "b.env"
            env_file.write_text(
                "BINANCE_TESTNET_API_KEY=k\nBINANCE_TESTNET_API_SECRET=s\n")
            broker = BinanceBroker(_config("", binance_env=str(env_file)))
            with self.assertRaises(BrokerRefused):
                broker.place_limit_order("BTCUSDT", "buy", "0.001", "20000")

    def test_invalid_side_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "b.env"
            env_file.write_text(
                "BINANCE_TESTNET_API_KEY=k\nBINANCE_TESTNET_API_SECRET=s\n")
            broker = BinanceBroker(_config("paper", binance_env=str(env_file)))
            with self.assertRaises(BrokerRefused):
                broker.place_limit_order("BTCUSDT", "yolo", "1", "1")


class AlpacaOrderGateTest(unittest.TestCase):
    def test_orders_refused_when_gate_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "a.env"
            env_file.write_text(
                "ALPACA_PAPER_API_KEY=k\nALPACA_PAPER_API_SECRET=s\n")
            broker = AlpacaBroker(_config("", alpaca_env=str(env_file)))
            with self.assertRaises(BrokerRefused):
                broker.place_limit_order("AAPL", "buy", "1", "100.00")

    def test_invalid_side_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "a.env"
            env_file.write_text(
                "ALPACA_PAPER_API_KEY=k\nALPACA_PAPER_API_SECRET=s\n")
            broker = AlpacaBroker(_config("paper", alpaca_env=str(env_file)))
            with self.assertRaises(BrokerRefused):
                broker.place_limit_order("AAPL", "sideways", "1", "100.00")


class RouteSymbolTest(unittest.TestCase):
    def test_usdt_pairs_route_to_binance(self):
        from tui.screens.broker import route_symbol

        self.assertEqual(route_symbol("BTCUSDT"), "binance")
        self.assertEqual(route_symbol("BTC-USD"), "binance")
        self.assertEqual(route_symbol("AAPL"), "alpaca")
        self.assertEqual(route_symbol("600519.SS"), "alpaca")


if __name__ == "__main__":
    unittest.main()


class BrokerScreenSmokeTest(unittest.TestCase):
    """Headless render: gate-closed mode shows READ-ONLY, brokers degrade."""

    def test_render_gate_closed(self):
        import asyncio

        async def scenario():
            from textual.widgets import Static

            from tui.screens.broker import BrokerScreen

            cfg = _config()  # gate closed, no credentials anywhere
            for key in ("ALPACA_PAPER_API_KEY", "APCA_API_KEY_ID",
                        "BINANCE_TESTNET_API_KEY", "BINANCE_API_KEY"):
                import os

                os.environ.pop(key, None)
            host_screen = BrokerScreen(config=cfg)
            from textual.app import App

            class Host(App):
                def __init__(self):
                    super().__init__()
                    self._panel = host_screen

            async with Host().run_test() as pilot:
                host = pilot.app
                host.push_screen(host_screen)
                await pilot.pause()
                await pilot.pause()
                status = host_screen.query_one("#broker-status", Static)
                self.assertIn("READ-ONLY", str(status.render()))

        asyncio.run(scenario())
