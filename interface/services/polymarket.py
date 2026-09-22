"""PolymarketService — the Gamma tape fetch, out of the screen.

Copy + adapt (no ``tui.`` import) of the old polymarket module's data
side (``tui/polymarket.py``, read-only reference): Gamma's public,
keyless ``/markets`` endpoint, fetched with stdlib urllib through an
EXPLICIT pinned HTTP proxy, collapsed into tape rows.

The fail-closed egress contract lives in the shared
:mod:`interface.services.egress` module (extracted from this module and
its energy twin by the D-M6 review — the twins carried verbatim copies,
self-admitted "kept in sync"; env ``STAMMTISCH_POLYMARKET_PROXY``):
ambient proxy variables are ignored, a missing or invalid proxy NEVER
falls back to direct access.

Adaptations from the old module:

- output rows are frozen :class:`MarketRow` dataclasses (the old dict
  keys kept verbatim) and the fetch result is a frozen
  :class:`MarketTape` (``{ok, markets, error, url, via}`` shape);
- the I/O seam is an injectable ``transport`` callable (default
  :func:`_open`, delegating to the shared pinned-proxy opener) instead
  of a monkeypatched module attribute;
- the ``/``-style LOCAL filter stays screen-side (it is view state, not
  data) — only the pure formatters live here (the old ``market_url``
  browser-launch helper died with the browser launch: no screen reads
  it).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from interface.services import egress
from interface.services.egress import (
    Egress,
    as_http_proxy,
    open_via_pinned_proxy,
    proxy_request_error,
)

__all__ = [
    "DEFAULT_LIMIT",
    "GAMMA_BASE",
    "Egress",
    "MarketRow",
    "MarketTape",
    "as_http_proxy",
    "fetch_markets",
    "format_detail",
    "format_vol",
    "format_yes",
    "resolve_egress",
    "summarize_market",
]

GAMMA_BASE = "https://gamma-api.polymarket.com"
_USER_AGENT = "stammtisch-interface/0.1 (+read-only; polymarket)"
DEFAULT_LIMIT = 50

#: Single I/O seam — ``transport(url, proxy, timeout) -> bytes``.
Transport = Callable[[str, str, float], bytes]


def resolve_egress(proxy_url: str | None = None) -> Egress | None:
    """Resolve only the explicit argument or ``STAMMTISCH_POLYMARKET_PROXY``."""
    return egress.resolve_egress(proxy_url=proxy_url,
                                 env_var="STAMMTISCH_POLYMARKET_PROXY")


def _open(url: str, proxy: str, timeout: float = 20.0) -> bytes:
    """Single I/O seam (default transport). Nothing else talks to the net."""
    return open_via_pinned_proxy(url, proxy, timeout,
                                 user_agent=_USER_AGENT)


def _loads_if_str(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):  # the old copy's `number != number` NaN guard
        return None
    return number


@dataclass(frozen=True)
class MarketRow:
    """One Gamma market collapsed for the tape (old dict keys verbatim)."""

    id: str
    question: str
    slug: str
    yes: float | None
    volume24hr: float | None
    volume: float | None
    end: str
    fee_rate: float | None
    category: str


def summarize_market(raw: dict[str, Any]) -> MarketRow:
    """Collapse a Gamma market object into the tape row we display.

    Copy of tui/polymarket.py:109-154: JSON-string fields parsed, the
    YES outcome price picked (first price as fallback), category from
    category/groupItemTitle/events-tags in that order.
    """
    try:
        price_values = _loads_if_str(raw.get("outcomePrices")) or []
    except (json.JSONDecodeError, TypeError, ValueError):
        price_values = []
    try:
        outcome_values = _loads_if_str(raw.get("outcomes")) or []
    except (json.JSONDecodeError, TypeError, ValueError):
        outcome_values = []
    prices = []
    for value in price_values if isinstance(price_values, list) else []:
        number = _finite(value)
        if number is not None:
            prices.append(number)
    outcomes = ([str(x) for x in outcome_values]
                if isinstance(outcome_values, list) else [])
    yes = None
    for name, price in zip(outcomes, prices):
        if name.lower() == "yes":
            yes = price
            break
    if yes is None and prices:
        yes = prices[0]
    try:
        fee = _loads_if_str(raw.get("feeSchedule")) or {}
    except (json.JSONDecodeError, TypeError, ValueError):
        fee = {}
    fee_rate = _finite(fee.get("rate")) if isinstance(fee, dict) else None
    category = str(raw.get("category") or raw.get("groupItemTitle") or "")
    events = raw.get("events") or []
    if not category and events and isinstance(events[0], dict):
        tags = events[0].get("tags") or []
        if tags and isinstance(tags[0], dict):
            category = str(tags[0].get("label") or tags[0].get("slug") or "")
        category = category or str(events[0].get("category") or "")
    return MarketRow(
        id=str(raw.get("id") or ""),
        question=str(raw.get("question") or ""),
        slug=str(raw.get("slug") or ""),
        yes=yes,
        volume24hr=_finite(raw.get("volume24hr")),
        volume=_finite(raw.get("volumeNum") or raw.get("volume")),
        end=str(raw.get("endDateIso") or "")[:10],
        fee_rate=fee_rate,
        category=category,
    )


@dataclass(frozen=True)
class MarketTape:
    """One fetch result (the old ``{ok, markets, error, url, via}``)."""

    ok: bool
    markets: tuple[MarketRow, ...] = ()
    error: str | None = None
    url: str = ""
    via: str | None = None


def fetch_markets(
    limit: int = DEFAULT_LIMIT,
    proxy_url: str | None = None,
    timeout: float = 20.0,
    transport: Transport = _open,
) -> MarketTape:
    """Active markets by 24h volume; fail-closed without a valid proxy.

    Copy of tui/polymarket.py:257-326 with the injectable transport.
    """
    params = urlencode({
        "limit": int(limit),
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    })
    url = f"{GAMMA_BASE}/markets?{params}"
    egress = resolve_egress(proxy_url=proxy_url)
    if not egress:
        configured = (proxy_url or "").strip() or (
            os.environ.get("STAMMTISCH_POLYMARKET_PROXY") or "").strip()
        if configured:
            error = ("invalid Polymarket proxy configuration; "
                     "direct network access is disabled")
        else:
            error = ("no Polymarket proxy configured; "
                     "direct network access is disabled")
        return MarketTape(ok=False, error=error, url=url)
    proxy = egress.url
    via = egress.label
    try:
        raw = json.loads(transport(url, proxy, timeout).decode("utf-8"))
    except HTTPError as exc:
        return MarketTape(ok=False, error=f"HTTP {exc.code}", url=url, via=via)
    except TimeoutError:
        return MarketTape(ok=False, error=proxy_request_error(timed_out=True),
                          url=url, via=via)
    except (URLError, OSError) as exc:
        return MarketTape(ok=False, error=proxy_request_error(exc), url=url,
                          via=via)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return MarketTape(ok=False, error="Gamma /markets returned invalid "
                                          "JSON", url=url, via=via)
    if not isinstance(raw, list):
        return MarketTape(ok=False, error="Gamma /markets is not a list",
                          url=url, via=via)
    markets = tuple(summarize_market(m) for m in raw if isinstance(m, dict))
    return MarketTape(ok=True, markets=markets, url=url, via=via)


# ── pure display shaping (old polymarket.py:157-189) ────────────────────


def format_yes(prob: float | None) -> str:
    if prob is None:
        return "—"
    return f"{prob * 100:.1f}%"


def format_vol(value: float | None) -> str:
    if value is None:
        return "—"
    abs_v = abs(value)
    if abs_v >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def format_detail(row: MarketRow) -> str:
    yes = row.yes
    no = (1.0 - yes) if isinstance(yes, float) else None
    fee = row.fee_rate
    fee_s = f"{fee * 100:.1f}%" if isinstance(fee, float) else "—"
    return (
        f"  {row.question or '?'}\n"
        f"  YES {format_yes(yes)}   NO {format_yes(no)}   "
        f"fee {fee_s}   24h ${format_vol(row.volume24hr)}   "
        f"end {row.end or '—'}   {row.category or '—'}\n"
        f"  slug  {row.slug or '—'}\n"
        f"  Read-only market data · no order path"
    )
