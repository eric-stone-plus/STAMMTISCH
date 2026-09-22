"""BROKER screen — sandbox execution panel (Alpaca paper + Binance testnet).

Structural rule, repeated everywhere: these are the pinned sandbox
endpoints, mainnet is refused in code, and every mutating action passes
the ``trading_mode`` gate. With the gate closed the screen renders
read-only notices and the order modal refuses before any wire traffic.
"""

from __future__ import annotations

import threading
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from ..analysis import _run_async
from services.brokers import (AlpacaBroker, BinanceBroker, BrokerError,
                       BrokerRefused)
from services.brokers.binance import binance_broker
from services.brokers.gate import trading_mode
from ..widgets import CYAN, DIM, GREEN, GRAY, RED, WHITE


def route_symbol(symbol: str) -> str:
    """Route a typed symbol to a broker.

    USDT pairs and Yahoo-style crypto pairs (BTC-USD) route to Binance
    spot testnet; everything else (equities, ETFs, suffixed tickers)
    routes to Alpaca paper. The confirm dialog always shows the routed
    broker before anything is sent.
    """
    text = symbol.strip().upper().replace("-", "")
    if text.endswith("USDT"):
        return "binance"
    if text.endswith("USD"):
        # Yahoo-style crypto pair; the modal labels this as Binance.
        return "binance"
    return "alpaca"


class BrokerScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("b", "order_buy", "Buy"),
        Binding("s", "order_sell", "Sell"),
        Binding("c", "cancel_order", "Cancel order"),
        Binding("question_mark", "show_help", "Keys"),
    ]
    CSS = """
    BrokerScreen { layout: vertical; }
    #broker-status { height: 1; padding: 0 1; color: #808080; }
    #broker-acct-wrap { height: auto; max-height: 9; border: solid #505050; background: #000000; }
    #broker-acct { padding: 0 1; }
    #broker-orders-wrap { height: 1fr; border: solid #505050; background: #000000; }
    #broker-pos-wrap { height: 1fr; border: solid #505050; background: #000000; }
    .broker-label { dock: top; height: 1; padding: 0 1; background: #303030; text-style: bold; color: #ffffff; }
    #broker-bottom { height: 1fr; layout: horizontal; }
    """

    def __init__(self, config: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.config = config

    def compose(self) -> ComposeResult:
        yield Static(
            "  BROKERS  |  ALPACA PAPER + BINANCE TESTNET ONLY — mainnet "
            "refused in code  |  [R] Refresh  [Esc] Back",
            classes="header-bar",
        )
        yield Static("", id="broker-status")
        with Vertical(id="broker-acct-wrap"):
            yield Static("  Accounts", classes="broker-label")
            yield Static("  …", id="broker-acct", markup=False)
        with Vertical(id="broker-orders-wrap"):
            yield Static("  Open orders (sandbox)", classes="broker-label")
            yield DataTable(id="broker-orders", cursor_type="row")
        with Vertical(id="broker-pos-wrap"):
            yield Static("  Positions / balances", classes="broker-label")
            yield DataTable(id="broker-positions", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        orders = self.query_one("#broker-orders", DataTable)
        orders.add_columns("BROKER", "SYMBOL", "SIDE", "QTY", "PRICE", "STATUS", "ID")
        positions = self.query_one("#broker-positions", DataTable)
        positions.add_columns("BROKER", "SYMBOL/ASSET", "QTY", "AVG/PRICE", "P&L")
        self.action_refresh()

    # ── refresh ──────────────────────────────────────────────────────
    def action_refresh(self) -> None:
        enabled = trading_mode(self.config) == "paper"

        def _work() -> dict[str, Any]:
            out: dict[str, Any] = {"enabled": enabled}
            try:
                alpaca = AlpacaBroker(self.config)
                out["alpaca_account"] = alpaca.account()
                out["alpaca_positions"] = alpaca.positions()
                out["alpaca_orders"] = alpaca.open_orders()
            except Exception as exc:
                out["alpaca_error"] = str(exc)
            try:
                binance = binance_broker(self.config)
                out["binance_label"] = getattr(binance, "LABEL",
                                               "BINANCE TESTNET")
                out["binance_account"] = binance.account()
                out["binance_orders"] = binance.open_orders()
                out["binance_positions"] = binance.positions()
            except Exception as exc:
                out["binance_error"] = str(exc)
            return out

        def _deliver(result: dict[str, Any]) -> None:
            if not self.is_mounted:
                return
            self._render_state(result)

        _run_async(self, _work, _deliver, dedup_key="broker-refresh")

    def _render_state(self, result: dict[str, Any]) -> None:
        enabled = result.get("enabled")
        mode = "PAPER GATE OPEN" if enabled else "READ-ONLY (trading_mode empty)"
        try:
            self.query_one("#broker-status", Static).update(
                f"  mode: {mode}  ·  order keys: [B]uy [S]ell [C]ancel")
        except Exception:
            pass

        lines: list[str] = []
        account = result.get("alpaca_account")
        if account:
            lines.append(
                f"  ALPACA PAPER  {account.get('status', '?')}  "
                f"equity {float(account.get('equity', 0)):,.2f}  "
                f"cash {float(account.get('cash', 0)):,.2f}  "
                f"buying power {float(account.get('buying_power', 0)):,.2f}")
        else:
            lines.append(f"  ALPACA PAPER  unavailable — "
                         f"{result.get('alpaca_error', 'no data')[:90]}")
        binance = result.get("binance_account")
        if binance:
            label = result.get("binance_label", "BINANCE TESTNET")
            if label == "BINANCE FUTURES TESTNET":
                lines.append(
                    f"  {label}  canTrade={binance.get('canTrade')}  "
                    f"wallet {float(binance.get('totalWalletBalance') or 0):,.2f}  "
                    f"available {float(binance.get('availableBalance') or 0):,.2f}")
            else:
                top = sorted(binance.get("balances", []),
                             key=lambda b: float(b.get("free", 0)), reverse=True)[:4]
                holdings = ", ".join(
                    f"{b['asset']} {float(b['free']):,.4f}" for b in top) or "no balances"
                lines.append(f"  {label}  canTrade={binance.get('canTrade')}  "
                             f"{holdings}")
        else:
            lines.append(f"  BINANCE TESTNET  unavailable — "
                         f"{result.get('binance_error', 'no data')[:90]}")
        try:
            self.query_one("#broker-acct", Static).update("\n".join(lines))
        except Exception:
            pass

        orders = self.query_one("#broker-orders", DataTable)
        orders.clear()
        self._order_index: dict[str, dict[str, str]] = {}
        for source, key_prefix in (("alpaca", "alpaca"), ("binance", "binance")):
            for order in result.get(f"{key_prefix}_orders") or []:
                symbol = str(order.get("symbol") or order.get("symbol"))
                row_key = (f"{key_prefix}:{symbol}:"
                           f"{order.get('id') or order.get('orderId')}")
                orders.add_row(
                    source.upper(),
                    symbol,
                    str(order.get("side") or "?"),
                    str(order.get("qty") or order.get("origQty") or "?"),
                    str(order.get("limit_price") or order.get("price") or "—"),
                    str(order.get("status") or "?"),
                    str(order.get("id") or order.get("orderId") or "?"),
                    key=row_key,
                )
                self._order_index[row_key] = {
                    "broker": key_prefix,
                    "symbol": symbol,
                    "id": str(order.get("id") or order.get("orderId") or ""),
                }

        positions = self.query_one("#broker-positions", DataTable)
        positions.clear()
        for position in result.get("alpaca_positions") or []:
            positions.add_row(
                "ALPACA",
                str(position.get("symbol") or "?"),
                str(position.get("qty") or "?"),
                str(position.get("avg_entry_price") or "—"),
                str(position.get("unrealized_pl") or "—"),
            )
        for balance in (binance or {}).get("balances") or []:
            total = float(balance.get("free", 0)) + float(balance.get("locked", 0))
            positions.add_row(
                "BINANCE",
                str(balance.get("asset") or "?"),
                f"{total:,.6f}",
                "—",
                "—",
            )
        for position in result.get("binance_positions") or []:
            amount = float(position.get("positionAmt", 0) or 0)
            positions.add_row(
                "BINANCE F",
                str(position.get("symbol") or "?"),
                f"{amount:+,.4f}",
                str(position.get("entryPrice") or "—"),
                str(position.get("unRealizedProfit") or "—"),
            )

    # ── gated order flow ─────────────────────────────────────────────
    def action_order_buy(self) -> None:
        self._order_flow("buy")

    def action_order_sell(self) -> None:
        self._order_flow("sell")

    def _order_flow(self, side: str) -> None:
        if trading_mode(self.config) != "paper":
            self.notify("Trading gate closed — set trading_mode=paper first.",
                        severity="warning")
            return
        from .modals import SymbolInputScreen

        self.app.push_screen(SymbolInputScreen(
            f"{side.upper()} limit order — format: SYMBOL QTY PRICE "
            "(USDT pair routes to Binance testnet)",
            lambda value: self._confirm_order(side, value)))

    def _confirm_order(self, side: str, value: str) -> None:
        from .modals import ConfirmScreen

        parts = str(value).split()
        if len(parts) != 3:
            self.notify("Format: SYMBOL QTY PRICE", severity="error")
            return
        symbol, quantity, price = parts
        broker_name = route_symbol(symbol)
        summary = (f"  {side.upper()} {quantity} {symbol} @ limit {price}\n\n"
                   f"  via {broker_name.upper()} "
                   f"({'paper' if broker_name == 'alpaca' else 'spot testnet'})\n"
                   f"  Mainnet is refused in code.")
        self.app.push_screen(ConfirmScreen(
            summary, lambda: self._place_order(broker_name, side, symbol,
                                               quantity, price)))

    def _place_order(self, broker_name: str, side: str, symbol: str,
                     quantity: str, price: str) -> None:
        def _work() -> dict[str, Any]:
            if broker_name == "binance":
                broker = binance_broker(self.config)
                return {"result": broker.place_limit_order(
                    symbol, side, quantity, price), "broker": broker_name}
            broker = AlpacaBroker(self.config)
            return {"result": broker.place_limit_order(
                symbol, side, quantity, price), "broker": broker_name}

        def _deliver(result: dict[str, Any]) -> None:
            placed = result.get("result") or {}
            order_id = placed.get("id") or placed.get("orderId") or "?"
            self.notify(
                f"{result.get('broker', '?').upper()} {side.upper()} "
                f"{symbol} {quantity} @ {price} -> status "
                f"{placed.get('status', '?')} (id {order_id})",
                severity="information")
            self.action_refresh()

        def _fail_local(error: str) -> None:
            self.notify(f"Order refused: {error}", severity="error")

        def _worker_wrapper():
            # _run_async wraps exceptions as {"ok": False, "error": ...}
            # only for exceptions; BrokerRefused must surface as a
            # readable refusal either way.
            return _work()

        def _on_result(result: Any) -> None:
            if isinstance(result, dict) and not result.get("ok", True):
                _fail_local(str(result.get("error") or "refused"))
                return
            _deliver(result)

        _run_async(self, _worker_wrapper, _on_result, dedup_key="broker-order")

    def action_cancel_order(self) -> None:
        table = self.query_one("#broker-orders", DataTable)
        if table.row_count == 0:
            self.notify("No open orders.", severity="warning")
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        info = (self._order_index or {}).get(str(row_key.value or ""))
        if not info:
            self.notify("Select an open order row first.", severity="warning")
            return
        from .modals import ConfirmScreen

        summary = (f"  Cancel {info['broker'].upper()} order\n\n"
                   f"  {info['symbol']}  id {info['id']}")
        self.app.push_screen(ConfirmScreen(
            summary, lambda: self._cancel(info)))

    def _cancel(self, info: dict[str, str]) -> None:
        if trading_mode(self.config) != "paper":
            self.notify("Trading gate closed.", severity="warning")
            return

        def _work() -> dict[str, Any]:
            if info["broker"] == "binance":
                binance_broker(self.config).cancel_order(info["symbol"],
                                                         info["id"])
            else:
                AlpacaBroker(self.config).cancel_order(info["id"])
            return {"ok": True}

        def _on_result(result: Any) -> None:
            if isinstance(result, dict) and not result.get("ok", True):
                self.notify(f"Cancel failed: {result.get('error')}",
                            severity="error")
                return
            self.notify("Order cancelled.", severity="information")
            self.action_refresh()

        _run_async(self, _work, _on_result, dedup_key="broker-cancel")

    def action_show_help(self) -> None:
        from .modals import KeyHelpScreen

        self.app.push_screen(KeyHelpScreen("BROKERS — KEYS", [
            ("r", "refresh accounts, orders, positions"),
            ("b / s", "buy / sell limit order (SYMBOL QTY PRICE)"),
            ("c", "cancel the highlighted open order"),
            ("gates", "trading_mode=paper required; sandbox endpoints pinned"),
        ]))

    def action_back(self) -> None:
        self.app.pop_screen()
