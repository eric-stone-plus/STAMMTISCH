"""EnergyService tests: the EIA v2 watchlist contract copied from
tui/energy.py — URL assembly (facets, forecast window), the response
collapse (points, change, forecast headline), fail-closed egress (no
key / no proxy / invalid proxy never reach the net), and the pure
formatters. The transport is a stub: nothing here talks to the net."""

from __future__ import annotations

import errno
import json
from datetime import date
from urllib.error import HTTPError, URLError

import pytest

from interface.services.energy import (
    SERIES,
    SeriesRow,
    SeriesSpec,
    as_http_proxy,
    build_url,
    fetch_series,
    fetch_watchlist,
    format_change,
    format_detail,
    format_value,
    parse_rows,
    resolve_egress,
)

TODAY = date(2026, 9, 22)
PROXY = "http://127.0.0.1:8080"


def _spec(**kw) -> SeriesSpec:
    base = {"key": "wti_spot", "group": "CRUDE", "label": "WTI Cushing spot",
            "route": "petroleum/pri/spt", "facets": (("series", "RWTC"),),
            "frequency": "daily", "unit": "$/bbl"}
    base.update(kw)
    return SeriesSpec(**base)


def _payload(periods):
    return {"response": {"data": [
        {"period": p, "value": v,
         "series-description": "WTI Cushing spot FOB"}
        for p, v in periods
    ]}}


# ── egress contract (fail-closed) ──────────────────────────────────────


def test_as_http_proxy_accepts_only_one_http_origin() -> None:
    assert as_http_proxy(PROXY) == PROXY
    assert as_http_proxy(f" {PROXY} ") == PROXY
    for bad in ("", "  ", "https://127.0.0.1:8080", "socks5://x:1080",
                "http://user:pass@127.0.0.1:8080", "http://127.0.0.1:8080/path",
                "http://127.0.0.1:99999", "http://127.0.0.1:notaport"):
        with pytest.raises(ValueError):
            as_http_proxy(bad)


def test_resolve_egress_reads_only_the_product_env(monkeypatch) -> None:
    monkeypatch.delenv("STAMMTISCH_ENERGY_PROXY", raising=False)
    monkeypatch.setenv("http_proxy", "http://ambient:1")
    assert resolve_egress(None) is None, "ambient proxies are ignored"
    monkeypatch.setenv("STAMMTISCH_ENERGY_PROXY", PROXY)
    assert resolve_egress(None) == resolve_egress(PROXY)
    monkeypatch.setenv("STAMMTISCH_ENERGY_PROXY", "https://bad:1")
    assert resolve_egress(None) is None, "invalid config fails closed"


# ── URL assembly ────────────────────────────────────────────────────────


def test_build_url_carries_key_facets_and_sort() -> None:
    url = build_url(_spec(), "KEY123")
    assert url.startswith(
        "https://api.eia.gov/v2/petroleum/pri/spt/data?")
    assert "api_key=KEY123" in url
    assert "frequency=daily" in url
    assert "facets%5Bseries%5D%5B%5D=RWTC" in url
    assert "sort%5B0%5D%5Bcolumn%5D=period" in url
    assert "length=10" in url


def test_build_url_forecast_window_starts_last_month_and_widens() -> None:
    spec = _spec(key="steo_wti", group="OUTLOOK", route="steo",
                 facets=(("seriesId", "WTIPUUS"),), frequency="monthly",
                 forecast=True)
    url = build_url(spec, "K", today=TODAY)
    assert "start=2026-08" in url, "Sep 2026 -> window starts Aug 2026"
    assert "length=36" in url, "widened past the projection horizon"
    url_jan = build_url(spec, "K", today=date(2026, 1, 15))
    assert "start=2025-12" in url_jan, "January wraps to December"


# ── response collapse ───────────────────────────────────────────────────


def test_parse_rows_collapses_points_newest_first_with_change() -> None:
    row = parse_rows(_spec(), _payload([("2026-09-20", 71.5),
                                        ("2026-09-19", 70.0)]))
    assert row.error is None
    assert row.period == "2026-09-20" and row.value == pytest.approx(71.5)
    assert row.prev_period == "2026-09-19"
    assert row.change == pytest.approx(1.5)
    assert row.change_pct == pytest.approx(1.5 / 70.0 * 100.0)
    assert row.history[0] == ("2026-09-20", 71.5)
    assert row.description == "WTI Cushing spot FOB"


def test_parse_rows_rejects_empty_and_shapeless_payloads() -> None:
    assert parse_rows(_spec(), {}).error == "EIA response has no data rows"
    assert parse_rows(_spec(), {"response": {"data": []}}).error == (
        "EIA returned no usable observations")
    nan = parse_rows(_spec(), {"response": {"data": [
        {"period": "p", "value": None}]}})
    assert nan.error == "EIA returned no usable observations"


def test_parse_rows_forecast_headlines_the_earliest_projection_month() -> None:
    spec = _spec(key="steo_wti", group="OUTLOOK", route="steo",
                 facets=(("seriesId", "WTIPUUS"),), frequency="monthly",
                 forecast=True)
    row = parse_rows(spec, _payload([
        ("2027-12", 90.0), ("2026-10", 72.0), ("2026-09", 71.0)]),
        today=TODAY)
    assert row.period == "2026-10", "earliest month after today headlined"
    assert row.prev_period == "2026-09", "diffed against the latest actual"
    assert row.change == pytest.approx(1.0)


# ── fetch seams ─────────────────────────────────────────────────────────


def _stub_transport(payload_by_period):
    body = json.dumps(_payload(payload_by_period)).encode("utf-8")
    calls: list[str] = []

    def transport(url, proxy, timeout):
        calls.append(url)
        assert proxy == PROXY, "the pinned proxy is the only egress"
        return body

    transport.calls = calls
    return transport


def test_fetch_series_lands_through_the_injected_transport() -> None:
    transport = _stub_transport([("2026-09-20", 71.5)])
    row = fetch_series(_spec(), "KEY", PROXY, transport=transport,
                       today=TODAY)
    assert row.error is None and row.value == pytest.approx(71.5)
    assert transport.calls and "api_key=KEY" in transport.calls[0]


def test_fetch_series_degrades_errors_to_rows() -> None:
    def http_error(url, proxy, timeout):
        raise HTTPError(url, 403, "Forbidden", None, None)

    row = fetch_series(_spec(), "KEY", PROXY, transport=http_error)
    assert row.error == "HTTP 403"

    def junk(url, proxy, timeout):
        return b"not-json{"

    assert "invalid JSON" in fetch_series(
        _spec(), "KEY", PROXY, transport=junk).error

    def refused(url, proxy, timeout):
        raise URLError(
            ConnectionRefusedError(errno.ECONNREFUSED, "refused"))

    assert fetch_series(_spec(), "KEY", PROXY,
                        transport=refused).error == (
        "configured proxy is unavailable")

    row = fetch_series(_spec(), "KEY", PROXY, transport=_stub_transport([]))
    assert row.error == "EIA returned no usable observations"


def test_fetch_watchlist_fails_closed_without_key_or_proxy(monkeypatch) -> None:
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.delenv("STAMMTISCH_ENERGY_PROXY", raising=False)
    frame = fetch_watchlist(api_key="  ")
    assert not frame.ok and "no EIA API key" in frame.error
    frame = fetch_watchlist(api_key="KEY", proxy_url=None)
    assert not frame.ok and "no energy proxy" in frame.error
    assert "direct network access is disabled" in frame.error
    frame = fetch_watchlist(api_key="KEY", proxy_url="https://bad:1")
    assert not frame.ok and "invalid energy proxy" in frame.error


def test_fetch_watchlist_composes_the_board(monkeypatch) -> None:
    monkeypatch.delenv("EIA_API_KEY", raising=False)

    def transport(url, proxy, timeout):
        if "/x/" in url:
            raise HTTPError(url, 500, "Boom", None, None)
        return json.dumps(
            _payload([("p1", 1.0)])).encode("utf-8")

    specs = (_spec(key="a"), _spec(key="b"), _spec(key="bad", route="x"))
    frame = fetch_watchlist(api_key="KEY", proxy_url=PROXY, specs=specs,
                            transport=transport)
    assert frame.ok and frame.via == "configured proxy"
    assert len(frame.rows) == 3, "an error row still keeps its seat"
    landed = [r for r in frame.rows if r.error is None]
    assert [r.key for r in landed] == ["a", "b"]
    bad = next(r for r in frame.rows if r.key == "bad")
    assert bad.error == "HTTP 500"


def test_fetch_watchlist_all_failed_is_not_ok(monkeypatch) -> None:
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    frame = fetch_watchlist(
        api_key="KEY", proxy_url=PROXY, specs=(_spec(key="a"),),
        transport=_stub_transport([]))
    assert not frame.ok
    assert frame.error == "EIA returned no usable observations"
    assert frame.rows and frame.rows[0].error


# ── formatters (old energy.py:539-576) ─────────────────────────────────


def test_format_value_and_change() -> None:
    assert format_value(None) == "—"
    assert format_value(1234.567, 2) == "1,234.57"
    assert format_value(1234.567, 0) == "1,235"
    assert format_change(SeriesRow(
        key="k", group="G", label="L", unit="u", decimals=2,
        frequency="daily")) == "—"
    row = SeriesRow(key="k", group="G", label="L", unit="u", decimals=2,
                    frequency="daily", change=1.25, change_pct=2.0)
    assert format_change(row) == "+1.25 (+2.0%)"
    down = SeriesRow(key="k", group="G", label="L", unit="u", decimals=0,
                     frequency="daily", change=-3.0)
    assert format_change(down) == "-3"


def test_format_detail_carries_the_eia_attribution() -> None:
    row = SeriesRow(key="k", group="G", label="WTI", unit="$/bbl",
                    decimals=2, frequency="daily", period="p", value=1.0,
                    history=(("p", 1.0), ("q", 2.0)),
                    description="desc", route="r")
    text = format_detail(row)
    assert "WTI · daily · $/bbl" in text
    assert "desc" in text and "p 1.00" in text
    assert "EIA Open Data API v2 · read-only" in text
    assert "eia.gov/opendata" in text
    err = SeriesRow(key="k", group="G", label="L", unit="u", decimals=2,
                    frequency="daily", route="r", error="HTTP 500")
    assert "[ERROR] HTTP 500" in format_detail(err)


def test_the_curated_watchlist_survived_the_copy() -> None:
    assert len(SERIES) == 14
    groups = {spec.group for spec in SERIES}
    assert groups == {"CRUDE", "GAS", "COAL", "WORLD", "OUTLOOK"}
    keys = [spec.key for spec in SERIES]
    assert len(keys) == len(set(keys)), "keys are unique row ids"
    forecast = [s for s in SERIES if s.forecast]
    assert all(s.frequency == "monthly" for s in forecast)
