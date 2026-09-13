"""Binance broker — testnet only, spot and USDⓈ-M futures variants.

Both base URLs are pinned to sandbox hosts (``testnet.binance.vision``,
``testnet.binancefuture.com``); mainnet is a documentation constant no
code path assigns. Signed requests use stdlib HMAC-SHA256 anchored to
the server clock, and order placement validates the symbol's
PRICE_FILTER / LOT_SIZE / MIN_NOTIONAL filters first — an off-filter
order is refused locally with a precise message instead of a rejected
round trip. Which testnet the credentials belong to is an operator
choice (config ``binance_testnet_kind``: "spot" | "futures").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from typing import Any
from urllib.parse import urlencode

from ..datafeeds import http as dfhttp
from .errors import BrokerError, BrokerRefused
from .gate import ensure_trading_allowed

TESTNET_BASE = "https://testnet.binance.vision"
FUTURES_TESTNET_BASE = "https://testnet.binancefuture.com"
# Documentation only — no code path assigns these to a broker instance.
MAINNET_BASE = "https://api.binance.com"
FUTURES_MAINNET_BASE = "https://fapi.binance.com"

_RECV_WINDOW_MS = 10_000
_TIME_CACHE_TTL = 60.0


class _SignedClient:
    """Shared signing/transport for both Binance testnet variants."""

    TIME_PATH = "/api/v3/time"

    def __init__(self, config: Any, base: str, credentials: Any,
                 provider: str):
        self._config = config
        self.base = base
        self._credentials = credentials
        self._provider = provider
        self._filters: dict[str, dict[str, float]] = {}
        # Per-instance server-clock offset: spot and futures testnet are
        # different hosts and must not share one skew estimate.
        self._time_offset: float | None = None
        self._time_checked_at = 0.0

    # ── wire helpers ─────────────────────────────────────────────────
    def _sign(self, query: str) -> str:
        return hmac.new(self._credentials.secret.encode("utf-8"),
                        query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _server_time_ms(self) -> float:
        now = time.monotonic()
        if self._time_offset is None or now - self._time_checked_at > _TIME_CACHE_TTL:
            payload = self._get(self.TIME_PATH)
            server_ms = float(payload["serverTime"])
            self._time_offset = server_ms - time.time() * 1000.0
            self._time_checked_at = now
        return time.time() * 1000.0 + (self._time_offset or 0.0)

    def _get(self, path: str, params: dict[str, Any] | None = None,
             signed: bool = False) -> Any:
        query = dict(params or {})
        headers = {"X-MBX-APIKEY": self._credentials.key} if signed else None
        if signed:
            query["timestamp"] = int(self._server_time_ms())
            query["recvWindow"] = _RECV_WINDOW_MS
            query["signature"] = self._sign(urlencode(query))
        url = self.base + path
        if query:
            url += "?" + urlencode(query)
        try:
            payload = dfhttp.get_json(url, timeout=10.0, headers=headers,
                                      provider=self._provider)
        except Exception as exc:
            # Binance returns JSON error bodies (code/msg) on 4xx; surface
            # them instead of the bare HTTP status.
            code = getattr(exc, "code", 0)
            detail = ""
            if code:
                try:
                    detail = exc.read().decode(errors="replace")[:200]
                except Exception:
                    detail = ""
            raise BrokerError(f"binance GET {path}: HTTP {code or 'error'} "
                              f"{detail or exc}") from exc
        if isinstance(payload, dict) and payload.get("code") and payload.get("msg"):
            raise BrokerError(f"binance GET {path}: "
                              f"{payload['code']} {payload['msg']}")
        return payload

    def _send(self, path: str, params: dict[str, Any],
              method: str = "POST") -> Any:
        """Signed mutation (POST/DELETE); gated + explicit HTTP method."""
        ensure_trading_allowed(self._config, "binance order action")
        from urllib.request import Request

        query = dict(params)
        query["timestamp"] = int(self._server_time_ms())
        query["recvWindow"] = _RECV_WINDOW_MS
        query["signature"] = self._sign(urlencode(query))
        url = self.base + path + "?" + urlencode(query)
        request = Request(url, headers={
            "X-MBX-APIKEY": self._credentials.key,
            "User-Agent": dfhttp.USER_AGENT}, method=method)
        try:
            with dfhttp._open(request, 10.0, self._provider) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            code = getattr(exc, "code", 0)
            detail = ""
            if code:
                try:
                    detail = exc.read().decode(errors="replace")[:300]
                except Exception:
                    detail = ""
            raise BrokerError(
                f"binance {method} {path}: HTTP {code or 'error'} {detail}") from exc
        payload = json.loads(text) if text.strip() else None
        if isinstance(payload, dict) and payload.get("code") and payload.get("msg"):
            raise BrokerError(f"binance {method} {path}: "
                              f"{payload['code']} {payload['msg']}")
        return payload

    # ── filter math (fail-closed before the wire) ────────────────────
    @staticmethod
    def _round_step(value: float, step: float) -> str:
        """Truncate DOWN to the filter step (never round up into a fill)."""
        if step <= 0:
            return f"{value:.8f}".rstrip("0").rstrip(".")
        quantized = int(value / step) * step
        decimals = max(0, int(-math.floor(math.log10(step))))
        return f"{quantized:.{decimals}f}"

    def conforming(self, symbol: str, quantity: float,
                   price: float) -> tuple[str, str]:
        """Return (qty, price) strings adjusted to the symbol's filters."""
        limits = self._symbol_filters(symbol)
        quantity = float(self._round_step(quantity, limits.get("step", 0.00001)))
        price = float(self._round_step(price, limits.get("tick", 0.01)))
        if quantity <= 0 or (limits.get("min_qty") and quantity < limits["min_qty"]):
            raise BrokerRefused(
                f"binance {symbol}: quantity {quantity} below LOT_SIZE "
                f"min {limits.get('min_qty')}")
        if limits.get("min_notional") and quantity * price < limits["min_notional"]:
            raise BrokerRefused(
                f"binance {symbol}: notional {quantity * price:.2f} below "
                f"min {limits['min_notional']}")
        return (self._round_step(quantity, limits.get("step", 0.00001)),
                self._round_step(price, limits.get("tick", 0.01)))

    def _symbol_filters(self, symbol: str) -> dict[str, float]:
        raise NotImplementedError


class BinanceBroker(_SignedClient):
    """Spot testnet client (config ``binance_testnet_kind: "spot"``)."""

    LABEL = "BINANCE SPOT TESTNET"

    def __init__(self, config: Any, base: str = TESTNET_BASE):
        if base != TESTNET_BASE:
            raise BrokerRefused(
                "Binance base URL is not the spot testnet — refused")
        from .keys import binance_credentials

        super().__init__(config, base, binance_credentials(config), "binance")

    # ── read-only ────────────────────────────────────────────────────
    def account(self) -> dict[str, Any]:
        payload = self._get("/api/v3/account", signed=True)
        balances = [b for b in payload.get("balances", [])
                    if float(b.get("free", 0)) + float(b.get("locked", 0)) > 0]
        return {**payload, "balances": balances}

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        payload = self._get("/api/v3/openOrders", params, signed=True)
        return payload if isinstance(payload, list) else []

    def positions(self) -> list[dict[str, Any]]:
        return []  # spot has no positions; balances cover the holdings

    # ── filters ──────────────────────────────────────────────────────
    def _symbol_filters(self, symbol: str) -> dict[str, float]:
        if symbol in self._filters:
            return self._filters[symbol]
        payload = self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not symbols:
            raise BrokerError(f"binance exchangeInfo for {symbol}: empty")
        extracted = _extract_filters(symbols[0].get("filters", []))
        self._filters[symbol] = extracted
        return extracted

    # ── gated mutations ──────────────────────────────────────────────
    def place_limit_order(self, symbol: str, side: str, quantity: str,
                          limit_price: str) -> dict[str, Any]:
        ensure_trading_allowed(self._config, "binance order placement")
        symbol, side, qty, price = _validated_order(symbol, side, quantity,
                                                    limit_price)
        qty, price = self.conforming(symbol, float(qty), float(price))
        return self._send("/api/v3/order", {
            "symbol": symbol, "side": side, "type": "LIMIT",
            "timeInForce": "GTC", "quantity": qty, "price": price,
        }, method="POST") or {}

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        ensure_trading_allowed(self._config, "binance order cancellation")
        return self._send("/api/v3/order",
                          {"symbol": symbol.strip().upper(),
                           "orderId": int(order_id)}, method="DELETE") or {}


class BinanceFuturesBroker(_SignedClient):
    """USDⓈ-M futures testnet client (``binance_testnet_kind: "futures"``).

    The GALAHAD product tree ships futures-testnet credentials, so this
    is the variant those keys authenticate against.
    """

    LABEL = "BINANCE FUTURES TESTNET"
    TIME_PATH = "/fapi/v1/time"

    def __init__(self, config: Any, base: str = FUTURES_TESTNET_BASE):
        if base != FUTURES_TESTNET_BASE:
            raise BrokerRefused(
                "Binance base URL is not the futures testnet — refused")
        from .keys import binance_credentials

        super().__init__(config, base, binance_credentials(config),
                         "binance-futures")

    # ── read-only ────────────────────────────────────────────────────
    def account(self) -> dict[str, Any]:
        payload = self._get("/fapi/v2/account", signed=True)
        return {
            "canTrade": payload.get("canTrade"),
            "totalWalletBalance": payload.get("totalWalletBalance"),
            "availableBalance": payload.get("availableBalance"),
            "balances": [],  # futures: wallet balances shown in the header
        }

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        payload = self._get("/fapi/v1/openOrders", params, signed=True)
        return payload if isinstance(payload, list) else []

    def positions(self) -> list[dict[str, Any]]:
        payload = self._get("/fapi/v2/positionRisk", signed=True)
        rows = payload if isinstance(payload, list) else []
        return [row for row in rows if float(row.get("positionAmt", 0) or 0) != 0]

    # ── filters ──────────────────────────────────────────────────────
    def _symbol_filters(self, symbol: str) -> dict[str, float]:
        if symbol in self._filters:
            return self._filters[symbol]
        payload = self._get("/fapi/v1/exchangeInfo")
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        match = next((s for s in (symbols or []) if s.get("symbol") == symbol),
                     None)
        if match is None:
            raise BrokerError(f"binance futures exchangeInfo: {symbol} unknown")
        extracted = _extract_filters(match.get("filters", []))
        self._filters[symbol] = extracted
        return extracted

    # ── gated mutations ──────────────────────────────────────────────
    def place_limit_order(self, symbol: str, side: str, quantity: str,
                          limit_price: str) -> dict[str, Any]:
        ensure_trading_allowed(self._config, "binance order placement")
        symbol, side, qty, price = _validated_order(symbol, side, quantity,
                                                    limit_price)
        qty, price = self.conforming(symbol, float(qty), float(price))
        return self._send("/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "LIMIT",
            "timeInForce": "GTC", "quantity": qty, "price": price,
        }, method="POST") or {}

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        ensure_trading_allowed(self._config, "binance order cancellation")
        return self._send("/fapi/v1/order",
                          {"symbol": symbol.strip().upper(),
                           "orderId": int(order_id)}, method="DELETE") or {}


def _extract_filters(filters: list[dict[str, Any]]) -> dict[str, float]:
    by_type = {f.get("filterType"): f for f in filters}
    extracted: dict[str, float] = {}
    price = by_type.get("PRICE_FILTER", {})
    lot = by_type.get("LOT_SIZE", {})
    notional = (by_type.get("NOTIONAL")
                or by_type.get("MIN_NOTIONAL") or {})
    if price.get("tickSize"):
        extracted["tick"] = float(price["tickSize"])
    if lot.get("stepSize"):
        extracted["step"] = float(lot["stepSize"])
    if lot.get("minQty"):
        extracted["min_qty"] = float(lot["minQty"])
    # Spot names the field minNotional; futures names it notional.
    min_notional = notional.get("minNotional") or notional.get("notional")
    if min_notional:
        extracted["min_notional"] = float(min_notional)
    return extracted


def _validated_order(symbol: str, side: str, quantity: str,
                     limit_price: str) -> tuple[str, str, str, str]:
    side = side.strip().lower()
    if side not in ("buy", "sell"):
        raise BrokerRefused(f"invalid side {side!r}")
    return symbol.strip().upper(), side.upper(), str(quantity), str(limit_price)


def binance_broker(config: Any) -> _SignedClient:
    """Build the testnet variant the operator's keys belong to."""
    kind = str(config.get("binance_testnet_kind", "spot") or "spot").lower()
    if kind == "futures":
        return BinanceFuturesBroker(config)
    return BinanceBroker(config)
