"""Broker error types."""

from __future__ import annotations


class BrokerError(Exception):
    """One broker call failed (transport, auth, or exchange rejection)."""


class BrokerRefused(BrokerError):
    """A call was refused locally before any wire traffic.

    Raised for: trading_mode gate closed, missing credentials, or an
    endpoint that is not the pinned sandbox. Refusals are structural —
    they never depend on network state.
    """
