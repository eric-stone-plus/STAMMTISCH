"""Feed-layer error type."""

from __future__ import annotations


class FeedError(Exception):
    """One provider's failure, carrying the provider name for the registry."""

    def __init__(self, provider: str, message: str):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
