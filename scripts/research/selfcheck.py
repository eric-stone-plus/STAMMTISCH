"""Full-workstation self-verification — every integrated feature, really used.

Not unit tests (pytest covers those): this script exercises each feature
the way the operator does — live feeds, real paper accounts, the engine
subprocess, the web page, the MR loop journal, the TUI boot — and prints
one PASS/FAIL line per feature. Exit code is non-zero if anything fails.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, str(detail)[:70]))
    except Exception as exc:
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}")[:2] + (str(exc)[:70],))


def main() -> int:
    from tui.config import Config
    from tui.datafeeds.http import configure_data_proxy, configure_proxy_fallback

    config = Config()
    configure_data_proxy(config.data_proxy_url)
    configure_proxy_fallback(config.egress_proxy_url)
    root = config.state_root or str(Path.home() / ".local/share/stammtisch")

    def quotes():
        from tui.datafeeds import service
        q = service.quotes(["AAPL", "600519.SS"])
        assert q, "no quotes served"
        assert all("source" in v for v in q.values())
        return f"{len(q)} quotes " + ",".join(v['source'].split()[0] for v in q.values())

    def candles():
        from tui.datafeeds import service
        bars = service.daily_candles("AAPL")
        assert len(bars) > 100
        return f"{len(bars)} bars to {bars[-1]['time']}"

    def crypto_board():
        from tui.datafeeds import service
        board = service.crypto_board(limit=30)
        assert len(board["rows"]) >= 10
        return f"{len(board['rows'])} coins, dom {board['btc_dominance'] and round(board['btc_dominance'],1)}%"

    def timing():
        from tui.timing import status
        out = status(config)
        states = {r["symbol"]: r["state"] for r in out["rows"]}
        assert states, "no timing rows"
        return str(states)

    def alpaca_account():
        from tui.brokers.alpaca import AlpacaBroker
        acct = AlpacaBroker(config).account()
        assert acct.get("status") == "ACTIVE"
        orders = AlpacaBroker(config).open_orders()
        return f"equity {float(acct['equity']):,.0f}, open orders {len(orders)}"

    def binance_account():
        from tui.brokers.binance import binance_broker
        b = binance_broker(config)
        acct = b.account()
        positions = b.positions()
        assert acct.get("canTrade")
        return (f"wallet {float(acct['totalWalletBalance']):,.0f}, "
                f"positions {len(positions)}")

    def engine_bridge():
        from types import SimpleNamespace
        from tui import cryptobacktest
        cfg = SimpleNamespace()
        real = config.get("crypto_backtest_cmd")
        cfg.get = lambda k, d=None: (real + " --source synthetic") if (
            k == "crypto_backtest_cmd" and real) else (
            config.get(k, d) if k != "crypto_backtest_cmd" else None)
        payload = cryptobacktest.run(cfg, symbol="BTC/USDT", timeframe="1d",
                                     start="2025-06-01", strategy="bollinger")
        assert payload["schema"] == "crypto.backtest.v1"
        return f"{len(payload['results'])} strategy result(s)"

    def astock_web():
        # localhost must bypass the ambient proxy (a loopback request
        # routed through the egress answers 502).
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:8787/api/card", timeout=10) as r:
            card = json.loads(r.read())
        assert "error" not in card, f"card error: {card.get('error')}"
        assert card.get("weights"), "card has no BUY rows"
        with opener.open("http://127.0.0.1:8787/", timeout=10) as r:
            html = r.read().decode()
        assert "BUY" in html
        # Freshness: the CN-direct path must keep the card within one
        # trading day (allow 4 calendar days for weekends/holidays).
        from datetime import date, timedelta
        asof = date.fromisoformat(str(card["asof"]))
        assert asof >= date.today() - timedelta(days=4), \
            f"card stale: asof {asof}"
        return f"card {card.get('asof')} (fresh), buys {len(card['weights'])}"

    def mr_loop_journal():
        path = Path(root) / "intel" / "mr-loop.jsonl"
        assert path.is_file(), "journal missing"
        age = time.time() - path.stat().st_mtime
        assert age < 900, f"journal stale ({age/60:.0f} min) — loop not running"
        return f"fresh ({age/60:.1f} min ago)"

    def strategy_record():
        path = Path(root) / "intel" / "strategy" / "sandbox-v3.json"
        payload = json.loads(path.read_text())
        assert payload["schema"] == "sandbox.strategy.v3"
        return "v3 formalized"

    def tui_boot():
        import asyncio

        async def scenario():
            from tui.app import StammtischTUI

            app = StammtischTUI()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                assert app.screen is not None
                return "booted"

        return asyncio.run(scenario())

    def daemons():
        out = subprocess.run(
            ["bash", str(Path(__file__).resolve().parent / "start_daemons.sh"),
             "status"], capture_output=True, text=True, timeout=15)
        lines = [l for l in out.stdout.splitlines() if ": " in l]
        down = [l for l in lines if l.endswith(": down")]
        assert not down, f"daemons down: {down}"
        return f"{len(lines)} up (" + "; ".join(
            l.split(":")[0] for l in lines) + ")"

    def engine_topk():
        from tui.engine import QuantEngine
        engine = QuantEngine.from_config(config)
        assert engine.quantkit_tree, "evolved quantkit tree not loaded"
        return "tree: " + engine.quantkit_tree[-30:]

    for name, fn in [
        ("live quotes (fallback chain)", quotes),
        ("daily candles (stooq->yahoo)", candles),
        ("crypto board (coingecko->binance)", crypto_board),
        ("market timing preset (alpaca IEX)", timing),
        ("alpaca paper account", alpaca_account),
        ("binance futures testnet", binance_account),
        ("crypto engine bridge [B]", engine_bridge),
        ("astock signal web page", astock_web),
        ("MR loop journal freshness", mr_loop_journal),
        ("24/7 daemons under PID mgmt", daemons),
        ("strategy record v3", strategy_record),
        ("evolved quantkit tree", engine_topk),
        ("TUI boot (real config)", tui_boot),
    ]:
        check(name, fn)

    width = max(len(name) for name, _ok, _d in RESULTS)
    failures = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        failures += 0 if ok else 1
        print(f"  [{mark}] {name:<{width}}  {detail}")
    print(f"\n{len(RESULTS) - failures}/{len(RESULTS)} features verified")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
