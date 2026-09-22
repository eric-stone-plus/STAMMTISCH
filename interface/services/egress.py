"""The pinned-proxy egress contract, shared by the HTTP service twins.

D-M6 finding 3 (interface/REVIEWS/D-M6-standards.md:11): services/energy.py
and services/polymarket.py each carried a near-verbatim ~110-line copy of
this machinery, self-admitted "kept in sync". One shared module —
parameterized by the product's env-var name and User-Agent — deletes the
shotgun-surgery risk. The contract itself is unchanged, copied verbatim
from the twins (originally ``tui/energy.py:238-310`` /
``tui/polymarket.py:55-221``):

- ambient proxy variables are ignored — only the explicit argument or
  the product's own env var is read;
- a missing or invalid proxy NEVER falls back to direct access
  (fail-closed);
- proxy credentials are rejected;
- :class:`PinnedProxyHandler` keeps the ``no_proxy`` bypass decision
  inside the opener, so a matching host cannot silently connect
  directly.

Services importing services is sanctioned: the rule (e) injection-only
boundary governs SCREEN modules, not the service lane.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

__all__ = [
    "Egress",
    "PinnedProxyHandler",
    "as_http_proxy",
    "open_via_pinned_proxy",
    "proxy_request_error",
    "resolve_egress",
]


def as_http_proxy(value: str) -> str:
    """Validate an explicit proxy URL supported by stdlib urllib.

    Copy of tui/energy.py:238-264: http:// only, one proxy origin, no
    credentials, valid port.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("proxy URL is empty")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy URL is invalid") from exc
    # CPython urllib reliably implements CONNECT for HTTPS targets through
    # an HTTP proxy. HTTPS-to-proxy (TLS-in-TLS) support varies by
    # interpreter and transport stack, so accepting it would make this
    # fail-closed path configuration-dependent.
    if parsed.scheme.lower() != "http":
        raise ValueError("proxy URL must use http://")
    if (not parsed.hostname or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
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


def resolve_egress(proxy_url: str | None = None, *,
                   env_var: str = "") -> Egress | None:
    """Resolve only the explicit argument or the product's own env var.

    ``env_var`` is the ONE environment variable this product reads (the
    twins pass their own name); every ambient proxy variable is ignored.
    An unresolvable or invalid configuration returns ``None`` — the
    caller renders its fail-closed error, never falls back to direct.
    """
    configured = (proxy_url or "").strip()
    if not configured and env_var:
        configured = (os.environ.get(env_var) or "").strip()
    if not configured:
        return None
    try:
        return Egress(url=as_http_proxy(configured))
    except ValueError:
        return None


class PinnedProxyHandler(ProxyHandler):
    """Explicit-proxy handler that never honors no_proxy/NO_PROXY.

    Copy of tui/energy.py:288-310 (see tui/polymarket.py:192-221 for the
    twin's fuller reasoning): urllib's ProxyHandler.proxy_open consults
    the ambient no_proxy list via the module-level ``proxy_bypass``
    function and silently connects DIRECTLY for matching hosts — which
    would void the fail-closed egress contract. Overriding a
    ``proxy_bypass`` method is ineffective because ProxyHandler does not
    dispatch through the instance. Keep the bypass decision inside this
    opener instead; no environment or urllib global is changed, so
    unrelated concurrent requests retain their own policy.
    """

    def proxy_open(self, req: Request, proxy: str, request_type: str):
        original_type = req.type
        parsed = urlsplit(proxy)
        proxy_type = (parsed.scheme or request_type).lower()
        if (proxy_type != "http" or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None):
            raise URLError("invalid explicit proxy")
        req.set_proxy(parsed.netloc, proxy_type)
        if original_type == proxy_type or original_type == "https":
            return None
        # Match ProxyHandler's restart behavior for an HTTP target while
        # deliberately omitting its ambient bypass.
        return self.parent.open(req, timeout=req.timeout)


def open_via_pinned_proxy(url: str, proxy: str, timeout: float = 20.0, *,
                          user_agent: str) -> bytes:
    """Single shared I/O seam. Nothing else talks to the net.

    The twins' ``_open`` default transports, unified: the URL is opened
    through a :class:`PinnedProxyHandler` opener pinned to the validated
    proxy, with the product's User-Agent header.
    """
    if not proxy:
        raise ValueError("an explicit proxy URL is required")
    pinned_proxy = as_http_proxy(proxy)
    req = Request(url, headers={"User-Agent": user_agent})
    opener = build_opener(
        PinnedProxyHandler({"http": pinned_proxy, "https": pinned_proxy})
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def proxy_request_error(error: BaseException | None = None, *,
                        timed_out: bool = False) -> str:
    """Stable English copy without exposing endpoint/OS details."""
    reason = getattr(error, "reason", error)
    if timed_out or isinstance(reason, TimeoutError):
        return "request timed out through the configured proxy"
    unavailable_errnos = {
        errno.ECONNREFUSED, errno.ECONNRESET,
        errno.EHOSTUNREACH, errno.ENETUNREACH,
    }
    if (isinstance(reason, OSError)
            and getattr(reason, "errno", None) in unavailable_errnos):
        return "configured proxy is unavailable"
    return "request failed through the configured proxy"
