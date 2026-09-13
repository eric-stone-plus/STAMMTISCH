"""Shared stdlib HTTP for feed providers.

One tiny helper instead of per-module transports: a browser User-Agent,
an explicit timeout, and no ambient proxy support — only proxies
explicitly configured through ``configure_data_proxy`` are ever used
(the repo's pinned-egress rule: ambient proxy variables are never
consulted). The CN-side Tencent endpoint stays direct; keyless global
endpoints (yahoo/stooq/coingecko/binance) ride the configured data
proxy when one is set. Only stdlib urllib — the minimal TUI lock
carries no requests dependency.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import urllib.request

DEFAULT_TIMEOUT = 6.0

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# provider name -> proxy URL; None = direct. ``_DEFAULT`` covers every
# provider without its own entry (tencent is pinned direct at configure
# time by the TUI).
_PROXY: dict[str, str | None] = {"_DEFAULT": None}
# Secondary proxy for connection-level failures only: when the primary
# data proxy process is down (connection refused/reset), keyless global
# feeds retry through it instead of dying. HTTP-level errors (the server
# answered) never trigger the fallback.
_PROXY_FALLBACK: str | None = None
_LOCK = threading.Lock()


def configure_data_proxy(proxy_url: str | None) -> None:
    """Point the keyless global providers at the configured data proxy.

    Empty/None clears the proxy (direct everywhere). Tencent stays
    direct unconditionally: it is a CN-side endpoint that predates the
    egress classes and is reachable without one.
    """
    with _LOCK:
        _PROXY["_DEFAULT"] = (proxy_url or "").strip() or None
        _PROXY["tencent"] = None


def configure_proxy_fallback(proxy_url: str | None) -> None:
    """Secondary proxy used only when the primary refuses connections."""
    global _PROXY_FALLBACK
    with _LOCK:
        _PROXY_FALLBACK = (proxy_url or "").strip() or None


def proxy_for(provider: str) -> str | None:
    with _LOCK:
        if provider in _PROXY:
            return _PROXY[provider]
        return _PROXY.get("_DEFAULT")


def _opener(proxy: str):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def _open(request: urllib.request.Request, timeout: float, provider: str):
    proxy = proxy_for(provider)
    if not proxy:
        return urllib.request.urlopen(request, timeout=timeout)
    try:
        return _opener(proxy).open(request, timeout=timeout)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if not isinstance(reason, OSError):
            raise  # HTTP-level or DNS answer: not a dead-proxy signal
        with _LOCK:
            fallback = _PROXY_FALLBACK
        if not fallback or fallback == proxy:
            raise
        return _opener(fallback).open(request, timeout=timeout)


def get_text(url: str, *, timeout: float = DEFAULT_TIMEOUT,
             headers: dict[str, str] | None = None,
             encoding: str = "utf-8",
             provider: str | None = None) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with _open(request, timeout, provider or "") as resp:
        return resp.read().decode(encoding, errors="replace")


def get_json(url: str, *, timeout: float = DEFAULT_TIMEOUT,
             headers: dict[str, str] | None = None,
             provider: str | None = None) -> Any:
    return json.loads(get_text(url, timeout=timeout, headers=headers,
                               provider=provider))
