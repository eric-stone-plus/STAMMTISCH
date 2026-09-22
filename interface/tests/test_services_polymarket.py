"""PolymarketService tests: the Gamma tape contract copied from
tui/polymarket.py — market collapse (JSON-string fields, YES pick,
category fallback), fail-closed egress, and the pure formatters. The
transport is a stub: nothing here talks to the net."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError

import pytest

from interface.services.polymarket import (
    GAMMA_BASE,
    MarketRow,
    as_http_proxy,
    fetch_markets,
    format_detail,
    format_vol,
    format_yes,
    resolve_egress,
    summarize_market,
)

PROXY = "http://127.0.0.1:8080"


def _raw(**kw):
    base = {
        "id": "7",
        "question": "Will X happen by 2026?",
        "slug": "will-x-happen",
        "outcomePrices": '["0.65", "0.35"]',
        "outcomes": '["Yes", "No"]',
        "volume24hr": 1234567.5,
        "volumeNum": 9000000,
        "endDateIso": "2026-12-31T00:00:00Z",
        "feeSchedule": '{"rate": 0.02}',
        "category": "Politics",
    }
    base.update(kw)
    return base


# ── market collapse (old polymarket.py:109-154) ────────────────────────


def test_summarize_market_picks_the_yes_price() -> None:
    row = summarize_market(_raw())
    assert isinstance(row, MarketRow)
    assert row.id == "7" and row.question.startswith("Will X")
    assert row.yes == pytest.approx(0.65)
    assert row.volume24hr == pytest.approx(1_234_567.5)
    assert row.volume == pytest.approx(9_000_000)
    assert row.end == "2026-12-31", "endDate truncated to the day"
    assert row.fee_rate == pytest.approx(0.02)
    assert row.category == "Politics"


def test_summarize_market_yes_fallback_and_category_chain() -> None:
    no_labels = _raw(outcomePrices='["0.42", "0.58"]',
                     outcomes='["Up", "Down"]', category="")
    row = summarize_market(no_labels)
    assert row.yes == pytest.approx(0.42), "first price when no Yes label"
    no_labels_events = _raw(category="", groupItemTitle="",
                            events=[{"tags": [{"label": "Elections"}]}])
    assert summarize_market(no_labels_events).category == "Elections"


def test_summarize_market_degrades_broken_fields() -> None:
    row = summarize_market(_raw(outcomePrices="not-json{",
                                outcomes='["Yes"]',
                                feeSchedule="}",
                                volume24hr=None, volumeNum=None,
                                volume=None))
    assert row.yes is None
    assert row.volume24hr is None and row.volume is None
    assert row.fee_rate is None


# ── egress contract (fail-closed) ──────────────────────────────────────


def test_resolve_egress_reads_only_the_product_env(monkeypatch) -> None:
    monkeypatch.delenv("STAMMTISCH_POLYMARKET_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient:1")
    assert resolve_egress(None) is None
    assert resolve_egress("https://127.0.0.1:9") is None
    monkeypatch.setenv("STAMMTISCH_POLYMARKET_PROXY", PROXY)
    assert resolve_egress(None).url == PROXY


def test_as_http_proxy_rejects_non_http_and_creds() -> None:
    for bad in ("", "https://p:1", "http://u:p@h:1", "http://h:1/x?q=1"):
        with pytest.raises(ValueError):
            as_http_proxy(bad)
    assert as_http_proxy("http://h:1") == "http://h:1"


# ── fetch seam ─────────────────────────────────────────────────────────


def test_fetch_markets_fails_closed_without_a_proxy(monkeypatch) -> None:
    monkeypatch.delenv("STAMMTISCH_POLYMARKET_PROXY", raising=False)
    tape = fetch_markets()
    assert not tape.ok and tape.markets == ()
    assert "no Polymarket proxy configured" in tape.error
    assert "direct network access is disabled" in tape.error
    assert tape.url.startswith(f"{GAMMA_BASE}/markets?")
    tape = fetch_markets(proxy_url="https://bad:1")
    assert "invalid Polymarket proxy" in tape.error


def test_fetch_markets_composes_the_tape_through_the_stub() -> None:
    body = json.dumps([_raw(), _raw(id="8", question="Second")]).encode()
    calls: list[tuple[str, str]] = []

    def transport(url, proxy, timeout):
        calls.append((url, proxy))
        return body

    tape = fetch_markets(limit=2, proxy_url=PROXY, transport=transport)
    assert tape.ok and tape.via == "configured proxy"
    assert [m.id for m in tape.markets] == ["7", "8"]
    (url, proxy), = calls
    assert url.startswith(f"{GAMMA_BASE}/markets?")
    assert "limit=2" in url and "order=volume24hr" in url
    assert proxy == PROXY, "the pinned proxy is the only egress"


def test_fetch_markets_degrades_errors() -> None:
    def http_error(url, proxy, timeout):
        raise HTTPError(url, 429, "Slow down", None, None)

    assert fetch_markets(proxy_url=PROXY,
                         transport=http_error).error == "HTTP 429"

    def junk(url, proxy, timeout):
        return b"<<"

    assert "invalid JSON" in fetch_markets(
        proxy_url=PROXY, transport=junk).error

    def refused(url, proxy, timeout):
        import errno

        raise URLError(
            ConnectionRefusedError(errno.ECONNREFUSED, "refused"))

    assert fetch_markets(proxy_url=PROXY, transport=refused).error == (
        "configured proxy is unavailable")

    def not_a_list(url, proxy, timeout):
        return b"{}"

    assert fetch_markets(proxy_url=PROXY, transport=not_a_list).error == (
        "Gamma /markets is not a list")


# ── formatters (old polymarket.py:157-189) ─────────────────────────────


def test_format_yes_and_vol() -> None:
    assert format_yes(None) == "—"
    assert format_yes(0.6543) == "65.4%"
    assert format_vol(None) == "—"
    assert format_vol(1_234_567.0) == "1.23M"
    assert format_vol(12_345.0) == "12.3k"
    assert format_vol(987.0) == "987"


def test_format_detail() -> None:
    row = summarize_market(_raw())
    detail = format_detail(row)
    assert "Will X happen by 2026?" in detail
    assert "YES 65.0%" in detail and "NO 35.0%" in detail
    assert "fee 2.0%" in detail and "1.23M" in detail
    assert "Read-only market data · no order path" in detail
