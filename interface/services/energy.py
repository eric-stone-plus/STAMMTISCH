"""EnergyService — the EIA v2 watchlist fetch, out of the screen.

Copy + adapt (no ``tui.`` import) of the old ENERGY module's data side
(``tui/energy.py``, read-only reference): a curated desk watchlist of
EIA Open Data API v2 series (crude / gas / coal / world / outlook),
fetched with stdlib urllib through an EXPLICIT pinned HTTP proxy and
collapsed into tape rows.

The fail-closed egress contract lives in the shared
:mod:`interface.services.egress` module (extracted from this module and
its polymarket twin by the D-M6 review — the twins carried verbatim
copies, self-admitted "kept in sync"): ambient proxy variables are
ignored, a missing or invalid proxy NEVER falls back to direct access,
and the API key comes from the explicit argument or ``EIA_API_KEY``.

Adaptations from the old module:

- output rows are frozen :class:`SeriesRow` dataclasses (the old dict
  keys kept verbatim) and the board result is a frozen
  :class:`EnergyFrame` (``{ok, rows, error, via}`` shape);
- the I/O seam is an injectable ``transport`` callable (default
  :func:`_open`, delegating to the shared pinned-proxy opener) instead
  of a monkeypatched module attribute — same single-seam rule, injection
  over patching;
- ``build_url``/``parse_rows`` take ``today`` so forecast-window logic
  is deterministic in tests (the old functions read ``date.today()``
  inline);
- the pure string formatters (``format_value`` / ``format_change`` /
  ``format_detail`` with the EIA attribution line) are copied alongside
  the data contract: they shape DATA for display, stdlib only.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
    "DEFAULT_LENGTH",
    "EIA_BASE",
    "REGISTER_URL",
    "SERIES",
    "Egress",
    "EnergyFrame",
    "SeriesRow",
    "SeriesSpec",
    "as_http_proxy",
    "build_url",
    "fetch_series",
    "fetch_watchlist",
    "format_change",
    "format_detail",
    "format_value",
    "parse_rows",
    "resolve_egress",
]

EIA_BASE = "https://api.eia.gov/v2"
REGISTER_URL = "https://www.eia.gov/opendata/"
_USER_AGENT = "stammtisch-interface/0.1 (+read-only; energy)"
#: Periods fetched per series (latest N observations, newest first).
DEFAULT_LENGTH = 10
#: EIA throttles at ~5 requests/second burst per key; stay under it.
_FETCH_WORKERS = 4

#: Single I/O seam — ``transport(url, proxy, timeout) -> bytes``. Tests
#: inject a stub; nothing else talks to the net.
Transport = Callable[[str, str, float], bytes]


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


#: Curated for a coal trader who also watches crude and gas. Routes, facet
#: values, and frequencies verified against api.eia.gov/v2 on 2026-08-17
#: (copied from tui/energy.py:63-235). Notes: EIA petroleum/NG futures
#: routes stop at 2024-04-05 (never use them); no weekly/monthly coal route
#: exists in v2; international coal is annual only; international unit
#: codes: MT = 1000 metric tons.
SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec(
        key="wti_spot", group="CRUDE", label="WTI Cushing spot",
        route="petroleum/pri/spt", facets=(("series", "RWTC"),),
        frequency="daily", unit="$/bbl",
    ),
    SeriesSpec(
        key="brent_spot", group="CRUDE", label="Brent spot",
        route="petroleum/pri/spt", facets=(("series", "RBRTE"),),
        frequency="daily", unit="$/bbl",
    ),
    SeriesSpec(
        key="us_crude_stocks", group="CRUDE", label="US crude stocks ex-SPR",
        route="petroleum/stoc/wstk", facets=(("series", "WCESTUS1"),),
        frequency="weekly", unit="Mbbl", decimals=0,
    ),
    SeriesSpec(
        key="henry_hub_spot", group="GAS", label="Henry Hub spot",
        route="natural-gas/pri/fut", facets=(("series", "RNGWHHD"),),
        frequency="daily", unit="$/MMBtu",
    ),
    SeriesSpec(
        key="us_gas_storage", group="GAS", label="Lower-48 working gas",
        route="natural-gas/stor/wkly",
        facets=(("series", "NW2_EPG0_SWO_R48_BCF"),),
        frequency="weekly", unit="Bcf", decimals=0,
    ),
    SeriesSpec(
        key="china_coal_imports", group="COAL", label="China coal imports",
        route="international",
        facets=(("productId", "7"), ("activityId", "3"),
                ("countryRegionId", "CHN"), ("unit", "MT")),
        frequency="annual", unit="kt", decimals=0,
    ),
    SeriesSpec(
        key="china_coal_production", group="COAL",
        label="China coal production",
        route="international",
        facets=(("productId", "7"), ("activityId", "1"),
                ("countryRegionId", "CHN"), ("unit", "MT")),
        frequency="annual", unit="kt", decimals=0,
    ),
    SeriesSpec(
        key="india_coal_production", group="COAL",
        label="India coal production",
        route="international",
        facets=(("productId", "7"), ("activityId", "1"),
                ("countryRegionId", "IND"), ("unit", "MT")),
        frequency="annual", unit="kt", decimals=0,
    ),
    SeriesSpec(
        key="saudi_crude_production", group="WORLD",
        label="Saudi crude production",
        route="international",
        facets=(("productId", "57"), ("activityId", "1"),
                ("countryRegionId", "SAU"), ("unit", "TBPD")),
        frequency="monthly", unit="kb/d", decimals=0,
    ),
    SeriesSpec(
        key="russia_crude_production", group="WORLD",
        label="Russia crude production",
        route="international",
        facets=(("productId", "57"), ("activityId", "1"),
                ("countryRegionId", "RUS"), ("unit", "TBPD")),
        frequency="monthly", unit="kb/d", decimals=0,
    ),
    SeriesSpec(
        key="japan_gas_imports", group="WORLD", label="Japan natgas imports",
        route="international",
        facets=(("productId", "26"), ("activityId", "3"),
                ("countryRegionId", "JPN"), ("unit", "BCF")),
        frequency="annual", unit="Bcf", decimals=0,
    ),
    SeriesSpec(
        key="steo_wti", group="OUTLOOK", label="WTI · EIA STEO fcst",
        route="steo", facets=(("seriesId", "WTIPUUS"),),
        frequency="monthly", unit="$/bbl", forecast=True,
    ),
    SeriesSpec(
        key="steo_brent", group="OUTLOOK", label="Brent · EIA STEO fcst",
        route="steo", facets=(("seriesId", "BREPUUS"),),
        frequency="monthly", unit="$/bbl", forecast=True,
    ),
    SeriesSpec(
        key="steo_henry_hub", group="OUTLOOK",
        label="Henry Hub · EIA STEO fcst",
        route="steo", facets=(("seriesId", "NGHHUUS"),),
        frequency="monthly", unit="$/MMBtu", forecast=True,
    ),
)


def resolve_egress(proxy_url: str | None = None) -> Egress | None:
    """Resolve only the explicit argument or ``STAMMTISCH_ENERGY_PROXY``."""
    return egress.resolve_egress(proxy_url=proxy_url,
                                 env_var="STAMMTISCH_ENERGY_PROXY")


def _open(url: str, proxy: str, timeout: float = 20.0) -> bytes:
    """Single I/O seam (default transport). Nothing else talks to the net."""
    return open_via_pinned_proxy(url, proxy, timeout,
                                 user_agent=_USER_AGENT)


def _today() -> date:
    """UTC today (the copy's ``date.today()`` default, tz-honest)."""
    return datetime.now(timezone.utc).date()


def build_url(spec: SeriesSpec, api_key: str, length: int = DEFAULT_LENGTH,
              today: date | None = None) -> str:
    """EIA v2 data URL for one series: newest ``length`` periods first.

    Copy of tui/energy.py:326-348; ``today`` is injected (the old read
    the clock inline) so the forecast window is testable.
    """
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
        current = today or _today()
        year, month = ((current.year, current.month - 1)
                       if current.month > 1 else (current.year - 1, 12))
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
    if math.isnan(number):  # the old copy's `number != number` NaN guard
        return None
    return number


@dataclass(frozen=True)
class SeriesRow:
    """One watchlist tape row (the old dict contract as a frozen row).

    ``error`` is None on a landed series; otherwise every data field is
    empty and ``error`` says why (old ``_error_row``).
    """

    key: str
    group: str
    label: str
    unit: str
    decimals: int
    frequency: str
    period: str | None = None
    value: float | None = None
    prev_period: str | None = None
    prev_value: float | None = None
    change: float | None = None
    change_pct: float | None = None
    history: tuple[tuple[str, float], ...] = ()
    description: str = ""
    route: str = ""
    error: str | None = None


def _error_row(spec: SeriesSpec, error: str) -> SeriesRow:
    return SeriesRow(
        key=spec.key, group=spec.group, label=spec.label, unit=spec.unit,
        decimals=spec.decimals, frequency=spec.frequency or "default",
        route=spec.route, error=error,
    )


def parse_rows(spec: SeriesSpec, payload: dict[str, Any],
               today: date | None = None) -> SeriesRow:
    """Collapse one EIA v2 data response into the tape row we display.

    Copy of tui/energy.py:382-439: usable (period, value) points only,
    newest first, change vs the previous point; forecast monthly series
    headline the earliest projection month diffed against the latest
    month at or before today.
    """
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
                item.get("series-description")
                or item.get("seriesDescription") or "")
        points.append((period, value))
    if not points:
        return _error_row(spec, "EIA returned no usable observations")
    points.sort(key=lambda point: point[0], reverse=True)
    latest_period, latest = points[0]
    prev_period, prev = points[1] if len(points) > 1 else (None, None)
    if spec.forecast and spec.frequency == "monthly":
        # Headline the earliest projection month; diff against the latest
        # month at or before today (the current-month estimate).
        current = (today or _today()).strftime("%Y-%m")
        future = [point for point in points if point[0] > current]
        past = [point for point in points if point[0] <= current]
        if future:
            latest_period, latest = future[-1]
            prev_period, prev = past[0] if past else (None, None)
    change = latest - prev if prev is not None else None
    change_pct = (change / prev * 100.0) if change is not None and prev else None
    return SeriesRow(
        key=spec.key, group=spec.group, label=spec.label, unit=spec.unit,
        decimals=spec.decimals, frequency=spec.frequency or "default",
        period=latest_period, value=latest, prev_period=prev_period,
        prev_value=prev, change=change, change_pct=change_pct,
        history=tuple(points), description=description, route=spec.route,
    )


def fetch_series(
    spec: SeriesSpec,
    api_key: str,
    proxy: str,
    timeout: float = 20.0,
    length: int = DEFAULT_LENGTH,
    transport: Transport = _open,
    today: date | None = None,
) -> SeriesRow:
    """Fetch one watchlist series; errors degrade to an error row."""
    url = build_url(spec, api_key, length=length, today=today)
    try:
        raw = json.loads(
            transport(url, proxy, timeout).decode("utf-8"))
    except HTTPError as exc:
        return _error_row(spec, f"HTTP {exc.code}")
    except TimeoutError:
        return _error_row(spec, proxy_request_error(timed_out=True))
    except (URLError, OSError) as exc:
        return _error_row(spec, proxy_request_error(exc))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return _error_row(spec, "EIA returned invalid JSON")
    if isinstance(raw, dict) and raw.get("error"):
        message = raw["error"]
        if isinstance(message, dict):
            message = message.get("message") or str(message)
        return _error_row(spec, f"EIA error: {message}")
    return parse_rows(spec, raw, today=today)


@dataclass(frozen=True)
class EnergyFrame:
    """Whole-board result (the old ``{ok, rows, error, via}`` dict)."""

    ok: bool
    rows: tuple[SeriesRow, ...] = ()
    error: str | None = None
    via: str | None = None


def fetch_watchlist(
    api_key: str | None = None,
    proxy_url: str | None = None,
    specs: tuple[SeriesSpec, ...] = SERIES,
    timeout: float = 20.0,
    transport: Transport = _open,
    today: date | None = None,
) -> EnergyFrame:
    """Whole board in parallel; fail-closed on missing key or proxy.

    Copy of tui/energy.py:489-536: per-series failures degrade to error
    rows; the board itself is ``ok`` as long as one series landed.
    """
    key = ((api_key or "").strip()
           or (os.environ.get("EIA_API_KEY") or "").strip())
    if not key:
        return EnergyFrame(
            ok=False,
            error="no EIA API key configured; "
                  "register free at eia.gov/opendata")
    egress = resolve_egress(proxy_url=proxy_url)
    if not egress:
        configured = (proxy_url or "").strip() or (
            os.environ.get("STAMMTISCH_ENERGY_PROXY") or "").strip()
        if configured:
            error = ("invalid energy proxy configuration; "
                     "direct network access is disabled")
        else:
            error = ("no energy proxy configured; "
                     "direct network access is disabled")
        return EnergyFrame(ok=False, error=error)

    def _one(spec: SeriesSpec) -> SeriesRow:
        return fetch_series(spec, key, egress.url, timeout=timeout,
                            transport=transport, today=today)

    with ThreadPoolExecutor(
            max_workers=min(_FETCH_WORKERS, max(1, len(specs)))) as pool:
        rows = list(pool.map(_one, specs))
    landed = [row for row in rows if row.error is None]
    if not landed:
        first_error = rows[0].error if rows else "no series configured"
        return EnergyFrame(ok=False, rows=tuple(rows), error=first_error,
                           via=egress.label)
    return EnergyFrame(ok=True, rows=tuple(rows), via=egress.label)


# ── pure display shaping (old energy.py:539-576) ────────────────────────


def format_value(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}"


def format_change(row: SeriesRow) -> str:
    change = row.change
    pct = row.change_pct
    if change is None:
        return "—"
    decimals = row.decimals
    sign = "+" if change >= 0 else ""
    if pct is None:
        return f"{sign}{change:,.{decimals}f}"
    return f"{sign}{change:,.{decimals}f} ({sign}{pct:.1f}%)"


def format_detail(row: SeriesRow) -> str:
    if row.error:
        return (
            f"  {row.label} · {row.group}\n"
            f"  [ERROR] {row.error}\n"
            f"  route {row.route or '—'} · reload to retry"
        )
    history = row.history or ()
    recent = "   ".join(
        f"{period} {format_value(value, row.decimals)}"
        for period, value in history[:5]
    )
    description = row.description or row.route or "—"
    return (
        f"  {row.label} · {row.frequency} · {row.unit}\n"
        f"  {description}\n"
        f"  {recent}\n"
        f"  EIA Open Data API v2 · read-only · register free at "
        f"eia.gov/opendata"
    )
