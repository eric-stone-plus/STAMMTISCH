"""ENERGY section — EIA Open Data API v2 watchlist, read-only, in the terminal.

Not MCP and not a browser viewport. The EIA Open Data API v2
(https://www.eia.gov/opendata/) is free with a registered API key, sent as
the ``api_key`` query parameter. This module GETs a curated desk watchlist
(crude, gas, coal, world, outlook) with stdlib urllib and renders a tape.
No auth beyond the key, no write path. Network access is fail-closed,
mirroring tui/polymarket.py: callers must provide an explicit HTTP proxy
URL through STAMMTISCH config (``energy_proxy_url``) or
``STAMMTISCH_ENERGY_PROXY``. Ambient proxy variables are ignored and a
missing proxy never falls back to direct access.
"""

from __future__ import annotations

import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from .analysis import _run_async


EIA_BASE = "https://api.eia.gov/v2"
REGISTER_URL = "https://www.eia.gov/opendata/"
_USER_AGENT = "stammtisch-tui/0.1 (+read-only; energy)"
# Periods fetched per series (latest N observations, newest first).
DEFAULT_LENGTH = 10
# EIA throttles at ~5 requests/second burst per key; stay under it.
_FETCH_WORKERS = 4


@dataclass(frozen=True)
class SeriesSpec:
    """One curated watchlist entry: an EIA v2 route plus display metadata."""

    key: str
    group: str  # CRUDE / GAS / COAL / WORLD / OUTLOOK
    label: str
    route: str  # path below /v2/, without the trailing /data node
    facets: tuple[tuple[str, str], ...] = ()
    frequency: str = ""  # empty = route default periodicity
    unit: str = ""
    decimals: int = 2
    # STEO-style series mix history with projections: headline the earliest
    # month after the current one and diff it against the latest actual.
    forecast: bool = False


# Curated for a coal trader who also watches crude and gas. Routes, facet
# values, and frequencies verified against api.eia.gov/v2 on 2026-08-17.
# Notes: EIA petroleum/NG futures routes stop at 2024-04-05 (never use
# them); no weekly/monthly coal route exists in v2; international coal is
# annual only; international unit codes: MT = 1000 metric tons.
SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec(
        key="wti_spot",
        group="CRUDE",
        label="WTI Cushing spot",
        route="petroleum/pri/spt",
        facets=(("series", "RWTC"),),
        frequency="daily",
        unit="$/bbl",
    ),
    SeriesSpec(
        key="brent_spot",
        group="CRUDE",
        label="Brent spot",
        route="petroleum/pri/spt",
        facets=(("series", "RBRTE"),),
        frequency="daily",
        unit="$/bbl",
    ),
    SeriesSpec(
        key="us_crude_stocks",
        group="CRUDE",
        label="US crude stocks ex-SPR",
        route="petroleum/stoc/wstk",
        facets=(("series", "WCESTUS1"),),
        frequency="weekly",
        unit="Mbbl",
        decimals=0,
    ),
    SeriesSpec(
        key="henry_hub_spot",
        group="GAS",
        label="Henry Hub spot",
        route="natural-gas/pri/fut",
        facets=(("series", "RNGWHHD"),),
        frequency="daily",
        unit="$/MMBtu",
    ),
    SeriesSpec(
        key="us_gas_storage",
        group="GAS",
        label="Lower-48 working gas",
        route="natural-gas/stor/wkly",
        facets=(("series", "NW2_EPG0_SWO_R48_BCF"),),
        frequency="weekly",
        unit="Bcf",
        decimals=0,
    ),
    SeriesSpec(
        key="china_coal_imports",
        group="COAL",
        label="China coal imports",
        route="international",
        facets=(
            ("productId", "7"),
            ("activityId", "3"),
            ("countryRegionId", "CHN"),
            ("unit", "MT"),
        ),
        frequency="annual",
        unit="kt",
        decimals=0,
    ),
    SeriesSpec(
        key="china_coal_production",
        group="COAL",
        label="China coal production",
        route="international",
        facets=(
            ("productId", "7"),
            ("activityId", "1"),
            ("countryRegionId", "CHN"),
            ("unit", "MT"),
        ),
        frequency="annual",
        unit="kt",
        decimals=0,
    ),
    SeriesSpec(
        key="india_coal_production",
        group="COAL",
        label="India coal production",
        route="international",
        facets=(
            ("productId", "7"),
            ("activityId", "1"),
            ("countryRegionId", "IND"),
            ("unit", "MT"),
        ),
        frequency="annual",
        unit="kt",
        decimals=0,
    ),
    SeriesSpec(
        key="saudi_crude_production",
        group="WORLD",
        label="Saudi crude production",
        route="international",
        facets=(
            ("productId", "57"),
            ("activityId", "1"),
            ("countryRegionId", "SAU"),
            ("unit", "TBPD"),
        ),
        frequency="monthly",
        unit="kb/d",
        decimals=0,
    ),
    SeriesSpec(
        key="russia_crude_production",
        group="WORLD",
        label="Russia crude production",
        route="international",
        facets=(
            ("productId", "57"),
            ("activityId", "1"),
            ("countryRegionId", "RUS"),
            ("unit", "TBPD"),
        ),
        frequency="monthly",
        unit="kb/d",
        decimals=0,
    ),
    SeriesSpec(
        key="japan_gas_imports",
        group="WORLD",
        label="Japan natgas imports",
        route="international",
        facets=(
            ("productId", "26"),
            ("activityId", "3"),
            ("countryRegionId", "JPN"),
            ("unit", "BCF"),
        ),
        frequency="annual",
        unit="Bcf",
        decimals=0,
    ),
    SeriesSpec(
        key="steo_wti",
        group="OUTLOOK",
        label="WTI · EIA STEO fcst",
        route="steo",
        facets=(("seriesId", "WTIPUUS"),),
        frequency="monthly",
        unit="$/bbl",
        forecast=True,
    ),
    SeriesSpec(
        key="steo_brent",
        group="OUTLOOK",
        label="Brent · EIA STEO fcst",
        route="steo",
        facets=(("seriesId", "BREPUUS"),),
        frequency="monthly",
        unit="$/bbl",
        forecast=True,
    ),
    SeriesSpec(
        key="steo_henry_hub",
        group="OUTLOOK",
        label="Henry Hub · EIA STEO fcst",
        route="steo",
        facets=(("seriesId", "NGHHUUS"),),
        frequency="monthly",
        unit="$/MMBtu",
        forecast=True,
    ),
)


def as_http_proxy(value: str) -> str:
    """Validate an explicit proxy URL supported by stdlib urllib.

    Mirror of tui/polymarket.as_http_proxy; keep the two copies in sync —
    each network module owns its fail-closed egress contract.
    """
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


@dataclass(frozen=True)
class Egress:
    """Explicit proxy binding without UI-visible endpoint details."""

    url: str
    label: str = "configured proxy"


def resolve_egress(proxy_url: str | None = None) -> Egress | None:
    """Resolve only the explicit argument or product-specific environment."""
    configured = (proxy_url or "").strip()
    if not configured:
        configured = (os.environ.get("STAMMTISCH_ENERGY_PROXY") or "").strip()
    if not configured:
        return None
    try:
        return Egress(url=as_http_proxy(configured))
    except ValueError:
        return None


class _PinnedProxyHandler(ProxyHandler):
    """Explicit-proxy handler that never honors no_proxy/NO_PROXY.

    Mirror of tui/polymarket._PinnedProxyHandler; see its docstring for why
    the bypass decision must live inside the opener.
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


def build_url(spec: SeriesSpec, api_key: str, length: int = DEFAULT_LENGTH) -> str:
    """EIA v2 data URL for one series: newest ``length`` periods first."""
    params: list[tuple[str, Any]] = [("api_key", api_key)]
    if spec.frequency:
        params.append(("frequency", spec.frequency))
    params.append(("data[]", "value"))
    for facet_id, facet_value in spec.facets:
        params.append((f"facets[{facet_id}][]", facet_value))
    if spec.forecast:
        # Forecast routes serve history AND projections sorted by period; a
        # plain newest-first cut would return only far-horizon months. Start
        # at last month and widen the window past the projection horizon.
        today = date.today()
        year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        params.append(("start", f"{year:04d}-{month:02d}"))
        length = max(int(length), 36)
    params.append(("sort[0][column]", "period"))
    params.append(("sort[0][direction]", "desc"))
    params.append(("offset", 0))
    params.append(("length", int(length)))
    route = spec.route.rstrip("/")
    suffix = "" if route.startswith("seriesid/") else "/data"
    return f"{EIA_BASE}/{route}{suffix}?{urlencode(params)}"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _error_row(spec: SeriesSpec, error: str) -> dict[str, Any]:
    return {
        "key": spec.key,
        "group": spec.group,
        "label": spec.label,
        "unit": spec.unit,
        "decimals": spec.decimals,
        "frequency": spec.frequency or "default",
        "period": None,
        "value": None,
        "prev_period": None,
        "prev_value": None,
        "change": None,
        "change_pct": None,
        "history": [],
        "description": "",
        "route": spec.route,
        "error": error,
    }


def parse_rows(
    spec: SeriesSpec,
    payload: dict[str, Any],
    today: date | None = None,
) -> dict[str, Any]:
    """Collapse one EIA v2 data response into the tape row we display."""
    response = payload.get("response") if isinstance(payload, dict) else None
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, list):
        return _error_row(spec, "EIA response has no data rows")
    points: list[tuple[str, float]] = []
    description = ""
    for item in data:
        if not isinstance(item, dict):
            continue
        period = str(item.get("period") or "").strip()
        value = _finite(item.get("value"))
        if not period or value is None:
            continue
        if not description:
            description = str(
                item.get("series-description") or item.get("seriesDescription") or ""
            )
        points.append((period, value))
    if not points:
        return _error_row(spec, "EIA returned no usable observations")
    points.sort(key=lambda point: point[0], reverse=True)
    latest_period, latest = points[0]
    prev_period, prev = points[1] if len(points) > 1 else (None, None)
    if spec.forecast and spec.frequency == "monthly":
        # Headline the earliest projection month; diff against the latest
        # month at or before today (the current-month estimate).
        current = (today or date.today()).strftime("%Y-%m")
        future = [point for point in points if point[0] > current]
        past = [point for point in points if point[0] <= current]
        if future:
            latest_period, latest = future[-1]
            prev_period, prev = past[0] if past else (None, None)
    change = latest - prev if prev is not None else None
    change_pct = (change / prev * 100.0) if change is not None and prev else None
    return {
        "key": spec.key,
        "group": spec.group,
        "label": spec.label,
        "unit": spec.unit,
        "decimals": spec.decimals,
        "frequency": spec.frequency or "default",
        "period": latest_period,
        "value": latest,
        "prev_period": prev_period,
        "prev_value": prev,
        "change": change,
        "change_pct": change_pct,
        "history": points,
        "description": description,
        "route": spec.route,
        "error": None,
    }


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


def fetch_series(
    spec: SeriesSpec,
    api_key: str,
    proxy: str,
    timeout: float = 20.0,
    length: int = DEFAULT_LENGTH,
) -> dict[str, Any]:
    """Fetch one watchlist series; errors degrade to an error row."""
    url = build_url(spec, api_key, length=length)
    try:
        raw = json.loads(_open(url, proxy=proxy, timeout=timeout).decode("utf-8"))
    except HTTPError as exc:
        return _error_row(spec, f"HTTP {exc.code}")
    except TimeoutError:
        return _error_row(spec, _proxy_request_error(timed_out=True))
    except (URLError, OSError) as exc:
        return _error_row(spec, _proxy_request_error(exc))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return _error_row(spec, "EIA returned invalid JSON")
    if isinstance(raw, dict) and raw.get("error"):
        message = raw["error"]
        if isinstance(message, dict):
            message = message.get("message") or str(message)
        return _error_row(spec, f"EIA error: {message}")
    return parse_rows(spec, raw)


def fetch_watchlist(
    api_key: str | None = None,
    proxy_url: str | None = None,
    specs: tuple[SeriesSpec, ...] = SERIES,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Whole board in parallel. ``{ok, rows, error, via}``.

    Fails closed on missing key or proxy. Per-series failures degrade to
    error rows; the board itself is ``ok`` as long as one series landed.
    """
    key = (api_key or "").strip() or (os.environ.get("EIA_API_KEY") or "").strip()
    if not key:
        return {
            "ok": False,
            "rows": [],
            "error": "no EIA API key configured; register free at eia.gov/opendata",
            "via": None,
        }
    egress = resolve_egress(proxy_url=proxy_url)
    if not egress:
        configured = (proxy_url or "").strip() or (
            os.environ.get("STAMMTISCH_ENERGY_PROXY") or ""
        ).strip()
        if configured:
            error = (
                "invalid energy proxy configuration; "
                "direct network access is disabled"
            )
        else:
            error = (
                "no energy proxy configured; "
                "direct network access is disabled"
            )
        return {"ok": False, "rows": [], "error": error, "via": None}

    def _one(spec: SeriesSpec) -> dict[str, Any]:
        return fetch_series(spec, key, egress.url, timeout=timeout)

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(_FETCH_WORKERS, max(1, len(specs)))) as pool:
        for row in pool.map(_one, specs):
            rows.append(row)
    landed = [row for row in rows if row.get("error") is None]
    if not landed:
        first_error = rows[0]["error"] if rows else "no series configured"
        return {"ok": False, "rows": rows, "error": first_error, "via": egress.label}
    return {"ok": True, "rows": rows, "error": None, "via": egress.label}


def format_value(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}"


def format_change(row: dict[str, Any]) -> str:
    change = row.get("change")
    pct = row.get("change_pct")
    if change is None:
        return "—"
    decimals = row.get("decimals", 2)
    sign = "+" if change >= 0 else ""
    if pct is None:
        return f"{sign}{change:,.{decimals}f}"
    return f"{sign}{change:,.{decimals}f} ({sign}{pct:.1f}%)"


def format_detail(row: dict[str, Any]) -> str:
    if row.get("error"):
        return (
            f"  {row.get('label') or '?'} · {row.get('group') or ''}\n"
            f"  [ERROR] {row['error']}\n"
            f"  route {row.get('route') or '—'} · reload to retry"
        )
    decimals = row.get("decimals", 2)
    history = row.get("history") or []
    recent = "   ".join(
        f"{period} {format_value(value, decimals)}" for period, value in history[:5]
    )
    description = row.get("description") or row.get("route") or "—"
    return (
        f"  {row.get('label') or '?'} · {row.get('frequency') or '—'} · "
        f"{row.get('unit') or '—'}\n"
        f"  {description}\n"
        f"  {recent}\n"
        f"  EIA Open Data API v2 · read-only · register free at eia.gov/opendata"
    )


class EnergyScreen(Screen):
    """Terminal watchlist of EIA energy series. Read-only."""

    TITLE = "  ENERGY  |  EIA OPEN DATA (READ ONLY)  |  R RELOAD  |  ESC BACK"
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "reload", "Reload"),
    ]
    CSS = """
    EnergyScreen { layout: vertical; }
    #eg-status { height: auto; color: #a0a0a0; padding: 0 1; }
    #eg-table-wrap { height: 1fr; border: solid #505050; }
    #eg-detail { height: 6; border: solid #505050; padding: 0 1; color: #c0c0c0; }
    """

    def __init__(
        self,
        api_key: str | None = None,
        proxy_url: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.api_key = api_key
        self.proxy_url = proxy_url
        self._rows: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static(self.TITLE, classes="header-bar")
        yield Static("  Loading energy watchlist...", id="eg-status")
        with Vertical(id="eg-table-wrap"):
            yield DataTable(id="eg-table", cursor_type="row")
        yield Static("  Select a row to inspect recent observations.", id="eg-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#eg-table", DataTable)
        table.add_columns("GROUP", "SERIES", "PERIOD", "LAST", "CHANGE", "UNIT")
        self.action_reload()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        status = self.query_one("#eg-status", Static)
        status.update("  Loading energy watchlist...")

        def _fetch():
            return fetch_watchlist(api_key=self.api_key, proxy_url=self.proxy_url)

        def _on_result(result):
            via = result.get("via") or "proxy unavailable"
            if not result.get("ok"):
                err = result.get("error") or "fetch failed"
                status.update(
                    f"  [ERROR] {err} · check EIA key/proxy in Settings, then reload"
                )
                self._rows = list(result.get("rows") or [])
                self._paint()
                return
            self._rows = list(result.get("rows") or [])
            landed = sum(1 for row in self._rows if row.get("error") is None)
            status.update(
                f"  {landed}/{len(self._rows)} series · EIA Open Data v2 · {via}"
            )
            self._paint()
            if self._rows:
                self.query_one("#eg-table", DataTable).focus()

        _run_async(self, _fetch, _on_result)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_detail(event.row_key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_detail(event.row_key)

    def _row_for(self, row_key) -> dict[str, Any] | None:
        if row_key is None or row_key.value is None:
            return None
        key = str(row_key.value)
        for row in self._rows:
            if str(row.get("key")) == key:
                return row
        return None

    def _paint(self) -> None:
        table = self.query_one("#eg-table", DataTable)
        table.clear()
        for row in self._rows:
            if row.get("error"):
                table.add_row(
                    row.get("group") or "?",
                    row.get("label") or "?",
                    "—",
                    "ERR",
                    "—",
                    row.get("unit") or "—",
                    key=str(row.get("key")),
                )
                continue
            decimals = row.get("decimals", 2)
            table.add_row(
                row.get("group") or "?",
                row.get("label") or "?",
                row.get("period") or "—",
                format_value(row.get("value"), decimals),
                format_change(row),
                row.get("unit") or "—",
                key=str(row.get("key")),
            )
        if not self._rows:
            self.query_one("#eg-detail", Static).update("  No series loaded.")

    def _show_detail(self, row_key) -> None:
        row = self._row_for(row_key)
        if row is not None:
            self.query_one("#eg-detail", Static).update(format_detail(row))
