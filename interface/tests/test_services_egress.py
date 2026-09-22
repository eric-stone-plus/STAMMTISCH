"""Egress module tests (D-M6 fix): the pinned-proxy contract extracted
from the energy/polymarket twins — one shared implementation, the
env-var name as its ONLY product parameter. Everything here is
headless: the opener seam is tested through its validation and handler
logic, never a real socket."""

from __future__ import annotations

import errno
from urllib.error import URLError
from urllib.request import Request

import pytest

from interface.services.egress import (
    PinnedProxyHandler,
    as_http_proxy,
    open_via_pinned_proxy,
    proxy_request_error,
    resolve_egress,
)

PROXY = "http://127.0.0.1:8080"


def test_resolve_egress_reads_only_the_named_env_var(monkeypatch) -> None:
    monkeypatch.delenv("STAMMTISCH_ENERGY_PROXY", raising=False)
    monkeypatch.setenv("STAMMTISCH_POLYMARKET_PROXY", PROXY)
    assert resolve_egress(None, env_var="STAMMTISCH_ENERGY_PROXY") is None, (
        "the energy twin's resolver must not read the polymarket env")
    egress = resolve_egress(None, env_var="STAMMTISCH_POLYMARKET_PROXY")
    assert egress is not None and egress.url == PROXY
    assert egress.label == "configured proxy"


def test_resolve_egress_explicit_argument_wins_and_fails_closed(
        monkeypatch) -> None:
    monkeypatch.setenv("STAMMTISCH_ENERGY_PROXY", PROXY)
    assert resolve_egress("https://bad:1",
                          env_var="STAMMTISCH_ENERGY_PROXY") is None, (
        "an invalid EXPLICIT config fails closed (no env fallback)")
    assert resolve_egress(PROXY, env_var="") is not None, (
        "the explicit argument needs no env var at all")


def test_as_http_proxy_accepts_only_one_http_origin() -> None:
    assert as_http_proxy(PROXY) == PROXY
    assert as_http_proxy(f" {PROXY} ") == PROXY
    for bad in ("", "https://127.0.0.1:8080", "socks5://x:1080",
                "http://user:pass@127.0.0.1:8080", "http://127.0.0.1:8080/p",
                "http://127.0.0.1:8080?q=1", "http://127.0.0.1:99999",
                "http://127.0.0.1:notaport"):
        with pytest.raises(ValueError):
            as_http_proxy(bad)


def test_pinned_proxy_handler_pins_and_rejects_invalid_proxies() -> None:
    handler = PinnedProxyHandler({"http": PROXY, "https": PROXY})
    https_req = Request("https://api.example.com/data")
    # An HTTPS target through the http pinned proxy is handed back to the
    # opener (None) after re-pinning — no ambient bypass is consulted.
    assert handler.proxy_open(https_req, PROXY, "https") is None
    with pytest.raises(URLError):
        handler.proxy_open(Request("https://api.example.com"),
                           "http://u:p@127.0.0.1:8080", "https")
    with pytest.raises(URLError):
        handler.proxy_open(Request("https://api.example.com"),
                           "socks5://127.0.0.1:1080", "https")


def test_open_via_pinned_proxy_requires_a_proxy_before_any_io() -> None:
    with pytest.raises(ValueError):
        open_via_pinned_proxy("https://api.example.com", "",
                              user_agent="test-agent")
    with pytest.raises(ValueError):
        open_via_pinned_proxy("https://api.example.com", "not a proxy",
                              user_agent="test-agent")


def test_proxy_request_error_copy_is_stable() -> None:
    assert proxy_request_error(
        timed_out=True) == "request timed out through the configured proxy"
    refused = URLError(
        ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
    assert proxy_request_error(
        refused) == "configured proxy is unavailable"
    assert proxy_request_error(
        RuntimeError("boom")) == "request failed through the configured proxy"
