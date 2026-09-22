"""Live E2E for the broker layer — sandbox only, fully reversible.

Sequence per broker: read-only account verification first, then ONE
limit order priced 50% below the market (cannot fill while the test
runs), verified as open, then cancelled and verified gone. Mainnet is
never contacted: the broker classes structurally refuse it.
"""

import sys
import time

from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

from services.config import Config
from services.datafeeds.http import configure_data_proxy
from services.brokers import AlpacaBroker, BrokerError, BrokerRefused
from services.brokers.binance import binance_broker

config = Config()
# The configured data proxy (127.0.0.1:46617) is down; use the live
# egress proxy for this run so the sandbox endpoints are reachable.
configure_data_proxy(config.egress_proxy_url)

print("=" * 72)
print("BROKER LIVE E2E — alpaca paper + binance spot testnet (sandbox only)")
print("=" * 72)

# ── 1. Alpaca paper: read-only verification ──────────────────────────
alpaca = AlpacaBroker(config)
print("\n[1] ALPACA PAPER (read-only)")
clock = alpaca.clock()
print(f"  clock: is_open={clock.get('is_open')}  next_open={clock.get('next_open')}")
account = alpaca.account()
print(f"  account: {account.get('status')}  equity={float(account.get('equity', 0)):,.2f}"
      f"  cash={float(account.get('cash', 0)):,.2f}"
      f"  buying_power={float(account.get('buying_power', 0)):,.2f}")
positions = alpaca.positions()
print(f"  positions: {len(positions)}")
for position in positions[:5]:
    print(f"    {position.get('symbol'):<8} qty={position.get('qty')} "
          f"avg={position.get('avg_entry_price')} "
          f"p&l={position.get('unrealized_pl')}")

# ── 2. Binance testnet: read-only verification ───────────────────────
binance = binance_broker(config)
print(f"\n[2] {getattr(binance, 'LABEL', 'BINANCE TESTNET')} (read-only)")
binance_account = binance.account()
print(f"  account: canTrade={binance_account.get('canTrade')}  "
      f"wallet={float(binance_account.get('totalWalletBalance') or 0):,.2f}  "
      f"available={float(binance_account.get('availableBalance') or 0):,.2f}")
for position in binance.positions()[:5]:
    print(f"    position {position.get('symbol')} amt={position.get('positionAmt')} "
          f"entry={position.get('entryPrice')} uPnL={position.get('unRealizedProfit')}")
print(f"  open orders: {len(binance.open_orders())}")

# ── 3. Alpaca E2E: deep limit order + cancel ─────────────────────────
print("\n[3] ALPACA E2E — limit buy 1 AAPL @ 50% of market, then cancel")
try:
    from tui import livefeed
    quote = livefeed.fetch_batch(["AAPL"]).get("AAPL") or {}
    last = float(quote.get("last") or 332.0)
    limit = f"{last * 0.5:.2f}"
    print(f"  market ref {last:,.2f} ({quote.get('source', 'n/a')}) -> limit {limit}")
    order = alpaca.place_limit_order("AAPL", "buy", "1", limit)
    order_id = order.get("id", "?")
    print(f"  placed: id={order_id}  status={order.get('status')}")
    time.sleep(1.0)
    alpaca.cancel_order(str(order_id))
    print(f"  cancelled: id={order_id}")
    time.sleep(1.0)
    still_open = any(str(o.get("id")) == str(order_id) for o in alpaca.open_orders())
    print(f"  verified gone from open orders: {not still_open}")
except BrokerRefused as exc:
    print(f"  REFUSED: {exc}")
except BrokerError as exc:
    print(f"  BROKER ERROR: {str(exc)[:140]}")

# ── 4. Binance E2E: deep limit order + cancel ────────────────────────
print("\n[4] BINANCE E2E — limit buy 0.005 BTCUSDT @ 50% of market, then cancel")
try:
    ticker = binance._get("/fapi/v1/ticker/price", {"symbol": "BTCUSDT"})
    market = float(ticker["price"])
    limit_price = f"{market * 0.5:.2f}"
    print(f"  market ref {market:,.2f} (testnet) -> limit {limit_price}")
    order = binance.place_limit_order("BTCUSDT", "buy", "0.005", limit_price)
    order_id = str(order.get("orderId", "?"))
    print(f"  placed: orderId={order_id}  status={order.get('status')}")
    time.sleep(1.0)
    cancelled = binance.cancel_order("BTCUSDT", order_id)
    print(f"  cancelled: status={cancelled.get('status')}")
    time.sleep(1.0)
    still_open = any(str(o.get("orderId")) == order_id
                     for o in binance.open_orders("BTCUSDT"))
    print(f"  verified gone from open orders: {not still_open}")
except BrokerRefused as exc:
    print(f"  REFUSED: {exc}")
except BrokerError as exc:
    print(f"  BROKER ERROR: {str(exc)[:140]}")

# ── 5. Final state ───────────────────────────────────────────────────
print("\n[5] FINAL OPEN ORDERS")
print(f"  alpaca paper: {len(alpaca.open_orders())}")
print(f"  binance testnet: {len(binance.open_orders())}")
print("\nDone. Every action ran against the pinned sandbox endpoints; "
      "mainnet was never contacted.")
