"""Alpaca broker — paper trading only.

The base URL is pinned to the paper endpoint at construction; the live
endpoint is a documentation constant and is never wired anywhere. Every
mutating call passes the trading-mode gate first, so a closed gate
means zero wire traffic.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import BrokerError, BrokerRefused
from .gate import ensure_trading_allowed
from .keys import alpaca_credentials

PAPER_BASE = "https://paper-api.alpaca.markets"
# Documentation only — no code path assigns this to a broker instance.
LIVE_BASE = "https://api.alpaca.markets"

SOURCE = "Alpaca paper"


class AlpacaBroker:
    """REST v2 client scoped to the paper endpoint."""

    def __init__(self, config: Any, base: str = PAPER_BASE):
        if base != PAPER_BASE:
            raise BrokerRefused(
                "Alpaca base URL is not the paper endpoint — refused")
        self.base = base
        self._config = config
        self._credentials = alpaca_credentials(config)

    def _raw(self, method: str, path: str,
             body: dict | None = None) -> tuple[int, str]:
        """One authenticated call; 4xx/5xx become BrokerError with detail."""
        from urllib.request import Request

        from ..datafeeds.http import USER_AGENT, _open

        request = Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={**self._headers(), "Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
            method=method)
        try:
            with _open(request, 10.0, "alpaca") as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            code = getattr(exc, "code", 0)
            detail = ""
            if code:
                try:
                    detail = exc.read().decode(errors="replace")[:300]
                except Exception:
                    detail = ""
            raise BrokerError(
                f"alpaca {method} {path}: HTTP {code or 'error'} {detail}") from exc

    def _json(self, method: str, path: str, body: dict | None = None) -> Any:
        _status, text = self._raw(method, path, body)
        if not text.strip():
            return None
        return json.loads(text)

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._credentials.key,
            "APCA-API-SECRET-KEY": self._credentials.secret,
        }

    # ── read-only ────────────────────────────────────────────────────
    def clock(self) -> dict[str, Any]:
        return self._json("GET", "/v2/clock") or {}

    def account(self) -> dict[str, Any]:
        return self._json("GET", "/v2/account") or {}

    def positions(self) -> list[dict[str, Any]]:
        payload = self._json("GET", "/v2/positions")
        return payload if isinstance(payload, list) else []

    def open_orders(self) -> list[dict[str, Any]]:
        payload = self._json("GET", "/v2/orders?status=open&limit=50")
        return payload if isinstance(payload, list) else []

    # ── gated mutations ──────────────────────────────────────────────
    def place_limit_order(self, symbol: str, side: str, quantity: str,
                          limit_price: str,
                          time_in_force: str = "day") -> dict[str, Any]:
        ensure_trading_allowed(self._config, "alpaca order placement")
        side = side.strip().lower()
        if side not in ("buy", "sell"):
            raise BrokerRefused(f"invalid side {side!r}")
        return self._json("POST", "/v2/orders", {
            "symbol": symbol.strip().upper(),
            "qty": str(quantity),
            "side": side,
            "type": "limit",
            "time_in_force": time_in_force,
            "limit_price": str(limit_price),
        }) or {}

    def cancel_order(self, order_id: str) -> None:
        ensure_trading_allowed(self._config, "alpaca order cancellation")
        self._json("DELETE", f"/v2/orders/{order_id}")

    def daily_bars(self, symbol: str, start: str = "2019-01-01") -> "Any":
        """Free IEX daily bars via the data API (read-only, paginated)."""
        from urllib.parse import urlencode

        from ..datafeeds.http import get_json

        bars: list[dict] = []
        page = None
        while True:
            params = {"timeframe": "1Day", "start": start, "limit": 10000}
            if page:
                params["page_token"] = page
            # The data API lives on its own host (same keys, read-only).
            payload = get_json(
                "https://data.alpaca.markets/v2/stocks/"
                f"{symbol.strip().upper()}/bars?{urlencode(params)}",
                headers=self._headers(), provider="alpaca")
            bars += payload.get("bars") or []
            page = payload.get("next_page_token")
            if not page:
                break
        return bars
