"""Exception types for the MVA Canvas client."""
from __future__ import annotations


class CanvasError(Exception):
    """Base error for all Canvas API problems."""


class CanvasAuthError(CanvasError):
    """401/403 - token missing, invalid, or lacking permission."""


class CanvasNotFoundError(CanvasError):
    """404 - resource does not exist or is not visible to this user."""


class CanvasHTTPError(CanvasError):
    """Any other non-2xx response."""

    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
