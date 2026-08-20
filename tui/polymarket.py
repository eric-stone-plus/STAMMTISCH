"""Polymarket prediction-market tape — read-only, in the terminal.

Not MCP and not a browser viewport. Gamma's public markets endpoint is
keyless; this module GETs it with stdlib urllib and renders a desk tape.
No auth, no order path. Network access is fail-closed: callers must provide
an explicit HTTP proxy URL through STAMMTISCH config or
``STAMMTISCH_POLYMARKET_PROXY``. Ambient proxy variables are ignored and a
missing proxy never falls back to direct access.
"""

from __future__ import annotations

import errno
import json
import os
import webbrowser
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from .analysis import _run_async


GAMMA_BASE = "https://gamma-api.polymarket.com"
MARKET_BASE = "https://polymarket.com"
_USER_AGENT = "stammtisch-tui/0.1 (+read-only; polymarket)"
DEFAULT_LIMIT = 50


def market_url(row: dict[str, Any]) -> str | None:
    """Public anonymous market page for one tape row, or None."""

    slug = str(row.get("slug") or "").strip()
    if not slug:
        return None
    return f"{MARKET_BASE}/market/{slug}"


@dataclass(frozen=True)
class Egress:
    """Explicit proxy binding without UI-visible endpoint details."""

    url: str
    label: str = "configured proxy"


def as_http_proxy(value: str) -> str:
    """Validate an explicit proxy URL supported by stdlib urllib."""
    text = (value or "").strip()
    if not text:
        raise ValueError("proxy URL is empty")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy URL is invalid") from exc
    # CPython urllib reliably implements CONNECT for HTTPS targets through an
    # HTTP proxy. HTTPS-to-proxy (TLS-in-TLS) support varies by interpreter
    # and transport stack, so accepting it would make this fail-closed path
    # configuration-dependent.
    if parsed.scheme.lower() != "http":
        raise ValueError("proxy URL must use http://")
    if not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("proxy URL must identify one proxy origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("proxy credentials are not supported")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("proxy port is invalid")
    return text


def resolve_egress(
    proxy_url: str | None = None,
) -> Egress | None:
    """Resolve only the explicit argument or product-specific environment."""
    configured = (proxy_url or "").strip()
    if not configured:
        configured = (os.environ.get("STAMMTISCH_POLYMARKET_PROXY") or "").strip()
    if not configured:
        return None
    try:
        return Egress(url=as_http_proxy(configured))
    except ValueError:
        return None


def _loads_if_str(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def summarize_market(raw: dict[str, Any]) -> dict[str, Any]:
    """Collapse a Gamma market object into the tape row we display."""
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
    outcomes = [str(x) for x in outcome_values] if isinstance(outcome_values, list) else []
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
    return {
        "id": str(raw.get("id") or ""),
        "question": str(raw.get("question") or ""),
        "slug": str(raw.get("slug") or ""),
        "yes": yes,
        "volume24hr": _finite(raw.get("volume24hr")),
        "volume": _finite(raw.get("volumeNum") or raw.get("volume")),
        "end": str(raw.get("endDateIso") or "")[:10],
        "fee_rate": fee_rate,
        "category": category,
    }


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


def format_detail(row: dict[str, Any]) -> str:
    yes = row.get("yes")
    no = (1.0 - yes) if isinstance(yes, float) else None
    fee = row.get("fee_rate")
    fee_s = f"{fee * 100:.1f}%" if isinstance(fee, float) else "—"
    end = row.get("end") or "—"
    slug = row.get("slug") or "—"
    cat = row.get("category") or "—"
    return (
        f"  {row.get('question') or '?'}\n"
        f"  YES {format_yes(yes)}   NO {format_yes(no)}   "
        f"fee {fee_s}   24h ${format_vol(row.get('volume24hr'))}   "
        f"end {end}   {cat}\n"
        f"  slug  {slug}\n"
        f"  Read-only market data · no order path"
    )


class _PinnedProxyHandler(ProxyHandler):
    """Explicit-proxy handler that never honors no_proxy/NO_PROXY.

    urllib's ProxyHandler.proxy_open consults the ambient no_proxy list via
    the module-level ``proxy_bypass`` function and silently connects DIRECTLY
    for matching hosts — which would void this module's fail-closed egress
    contract.  Overriding a ``proxy_bypass`` method is ineffective because
    ProxyHandler does not dispatch through the instance.  Keep the bypass
    decision inside this opener instead; no environment or urllib global is
    changed, so unrelated concurrent requests retain their own policy.
    """

    def proxy_open(self, req: Request, proxy: str, request_type: str):
        original_type = req.type
        parsed = urlsplit(proxy)
        proxy_type = (parsed.scheme or request_type).lower()
        if (
            proxy_type != "http"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise URLError("invalid explicit proxy")

        req.set_proxy(parsed.netloc, proxy_type)
        if original_type == proxy_type or original_type == "https":
            return None
        # Match ProxyHandler's restart behavior for an HTTP target while
        # deliberately omitting its ambient bypass.
        return self.parent.open(req, timeout=req.timeout)


def _open(url: str, proxy: str, timeout: float = 20.0) -> bytes:
    """Single I/O seam. Tests monkeypatch this; nothing else talks to the net."""
    if not proxy:
        raise ValueError("an explicit proxy URL is required")
    pinned_proxy = as_http_proxy(proxy)
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    opener = build_opener(
        _PinnedProxyHandler({"http": pinned_proxy, "https": pinned_proxy})
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _proxy_request_error(
    error: BaseException | None = None,
    *,
    timed_out: bool = False,
) -> str:
    """Return stable English UI copy without exposing endpoint/OS details."""
    reason = getattr(error, "reason", error)
    if timed_out or isinstance(reason, TimeoutError):
        return "request timed out through the configured proxy"
    unavailable_errnos = {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
    }
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in unavailable_errnos:
        return "configured proxy is unavailable"
    return "request failed through the configured proxy"


def fetch_markets(
    limit: int = DEFAULT_LIMIT,
    proxy_url: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Active markets by 24h volume. ``{ok, markets, error, url, via}``."""
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
            os.environ.get("STAMMTISCH_POLYMARKET_PROXY") or ""
        ).strip()
        if configured:
            error = (
                "invalid Polymarket proxy configuration; "
                "direct network access is disabled"
            )
        else:
            error = (
                "no Polymarket proxy configured; "
                "direct network access is disabled"
            )
        return {
            "ok": False,
            "markets": [],
            "error": error,
            "url": url,
            "via": None,
        }
    proxy = egress.url
    via = egress.label
    try:
        raw = json.loads(_open(url, proxy=proxy, timeout=timeout).decode("utf-8"))
    except HTTPError as exc:
        return {"ok": False, "markets": [], "error": f"HTTP {exc.code}", "url": url, "via": via}
    except TimeoutError:
        return {
            "ok": False,
            "markets": [],
            "error": _proxy_request_error(timed_out=True),
            "url": url,
            "via": via,
        }
    except (URLError, OSError) as exc:
        return {
            "ok": False,
            "markets": [],
            "error": _proxy_request_error(exc),
            "url": url,
            "via": via,
        }
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return {
            "ok": False,
            "markets": [],
            "error": "Gamma /markets returned invalid JSON",
            "url": url,
            "via": via,
        }
    if not isinstance(raw, list):
        return {"ok": False, "markets": [], "error": "Gamma /markets is not a list", "url": url, "via": via}
    markets = [summarize_market(m) for m in raw if isinstance(m, dict)]
    return {"ok": True, "markets": markets, "error": None, "url": url, "via": via}


def _matches(row: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    blob = " ".join(
        str(row.get(k) or "") for k in ("question", "slug", "category")
    ).casefold()
    return query.casefold() in blob


class PolymarketScreen(Screen):
    """Terminal tape of active Polymarket contracts. Read-only."""

    TITLE = "  POLYMARKET PREDICTION MARKET  |  READ ONLY  |  / FILTER  R RELOAD  |  ESC BACK"
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "reload", "Reload"),
        Binding("slash", "focus_filter", "Filter"),
    ]
    CSS = """
    PolymarketScreen { layout: vertical; }
    #pm-status { height: auto; color: #a0a0a0; padding: 0 1; }
    #pm-table-wrap { height: 1fr; border: solid #505050; }
    #pm-detail { height: 6; border: solid #505050; padding: 0 1; color: #c0c0c0; }
    """

    def __init__(self, proxy_url: str | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.proxy_url = proxy_url
        self._rows: list[dict[str, Any]] = []
        self._query = ""

    def compose(self) -> ComposeResult:
        yield Static(self.TITLE, classes="header-bar")
        yield Input(placeholder="Filter by question, slug, or category...", id="pm-filter")
        yield Static("  Loading active markets...", id="pm-status")
        with Vertical(id="pm-table-wrap"):
            yield DataTable(id="pm-table", cursor_type="row")
        yield Static("  Select a row to inspect; Enter opens the market page.", id="pm-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#pm-table", DataTable)
        table.add_columns("YES", "24H VOL", "END", "QUESTION")
        self.query_one("#pm-filter", Input).focus()
        self.action_reload()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_focus_filter(self) -> None:
        self.query_one("#pm-filter", Input).focus()

    def action_reload(self) -> None:
        status = self.query_one("#pm-status", Static)
        status.update("  Loading active markets...")

        def _fetch():
            return fetch_markets(proxy_url=self.proxy_url)

        def _on_result(result):
            via = result.get("via") or "proxy unavailable"
            if not result.get("ok"):
                err = result.get("error") or "fetch failed"
                status.update(f"  [ERROR] {err} · check Market Proxy in Settings, then reload")
                self._rows = []
                self._paint()
                return
            self._rows = list(result.get("markets") or [])
            status.update(f"  {len(self._rows)} active markets · ranked by 24h volume · {via}")
            self._paint()
            if self._rows:
                self.query_one("#pm-table", DataTable).focus()

        _run_async(self, _fetch, _on_result)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "pm-filter":
            return
        self._query = event.value.strip()
        self._paint()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "pm-filter":
            self.query_one("#pm-table", DataTable).focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_detail(event.row_key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_detail(event.row_key)
        row = self._row_for(event.row_key)
        if row is not None:
            url = market_url(row)
            if url is not None:
                webbrowser.open(url)

    def _row_for(self, row_key) -> dict[str, Any] | None:
        if row_key is None or row_key.value is None:
            return None
        key = str(row_key.value)
        for row in self._rows:
            if str(row.get("id") or row.get("slug") or row.get("question")) == key:
                return row
        return None

    def _visible(self) -> list[dict[str, Any]]:
        return [row for row in self._rows if _matches(row, self._query)]

    def _paint(self) -> None:
        table = self.query_one("#pm-table", DataTable)
        table.clear()
        visible = self._visible()
        for row in visible:
            key = row.get("id") or row.get("slug") or row.get("question")
            table.add_row(
                format_yes(row.get("yes")),
                format_vol(row.get("volume24hr")),
                row.get("end") or "—",
                row.get("question") or "?",
                key=str(key),
            )
        if self._query:
            status = self.query_one("#pm-status", Static)
            status.update(f'  {len(visible)}/{len(self._rows)} markets match "{self._query}"')
        if not visible:
            self.query_one("#pm-detail", Static).update("  No matching markets.")

    def _show_detail(self, row_key) -> None:
        row = self._row_for(row_key)
        if row is not None:
            self.query_one("#pm-detail", Static).update(format_detail(row))


class CryptoScreen(PolymarketScreen):
    """Crypto module — the read-only Polymarket tape.

    Crypto headlines are still captured by the daily intake; the former
    daily-report desk was removed when reports moved to the browser
    (the sentiment board keeps the terminal surface).
    """

    TITLE = "  CRYPTO  |  POLYMARKET TAPE (READ ONLY)  |  R RELOAD  |  ESC BACK"

    def __init__(self, proxy_url: str | None = None, *, config: Any = None, **kwargs: Any):
        super().__init__(proxy_url=proxy_url, **kwargs)
        self.config = config
