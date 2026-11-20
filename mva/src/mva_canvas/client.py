"""
Low-level Canvas REST client: authentication, pagination, and error mapping.

Canvas is a standard REST API. We authenticate with a personal access token via
the ``Authorization: Bearer`` header, and follow RFC-5988 ``Link`` headers for
pagination.
"""
from __future__ import annotations

import re
from typing import Any, Iterator, Optional

import requests

from .errors import (
    CanvasAuthError,
    CanvasHTTPError,
    CanvasNotFoundError,
)

_LINK_RE = re.compile(r'<(?P<url>[^>]+)>;\s*rel="(?P<rel>[^"]+)"')


def _next_link(link_header: Optional[str]) -> Optional[str]:
    """Return the url for rel="next" from a Link header, or None."""
    if not link_header:
        return None
    for m in _LINK_RE.finditer(link_header):
        if m.group("rel") == "next":
            return m.group("url")
    return None


class CanvasClient:
    """
    Thin HTTP wrapper around the Canvas API.

    Parameters
    ----------
    base_url: e.g. "https://minervavirtual.instructure.com"
    token:    personal access token
    session:  optional pre-built requests.Session (useful for tests)
    timeout:  per-request timeout in seconds
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        if not token and session is None:
            raise ValueError("either a token or a pre-authenticated session is required")
        self.base_url = base_url.rstrip("/")
        self.api_root = f"{self.base_url}/api/v1"
        self.timeout = timeout
        self._session = session or requests.Session()
        if token:
            # Personal access token (Bearer). Not available on all instances.
            self._session.headers.update({"Authorization": f"Bearer {token}"})
        # Otherwise the caller supplied a session already carrying Canvas cookies
        # (see mva_canvas.session.requests_session_from_cookies).

    # -- internals -------------------------------------------------------

    def _abs(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url
        if path_or_url.startswith("/api/"):
            return f"{self.base_url}{path_or_url}"
        return f"{self.api_root}/{path_or_url.lstrip('/')}"

    def _check(self, resp: requests.Response) -> None:
        if resp.status_code in (401, 403):
            raise CanvasAuthError(
                f"HTTP {resp.status_code}: token missing, invalid, or not permitted "
                f"for {resp.url}"
            )
        if resp.status_code == 404:
            raise CanvasNotFoundError(f"404 Not Found: {resp.url}")
        if not (200 <= resp.status_code < 300):
            raise CanvasHTTPError(resp.status_code, resp.url, resp.text)

    # -- public ----------------------------------------------------------

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET a single resource (or a single page). Returns parsed JSON."""
        resp = self._session.get(self._abs(path), params=params, timeout=self.timeout)
        self._check(resp)
        return resp.json()

    def get_paginated(self, path: str, params: Optional[dict] = None) -> list:
        """GET a list resource, following Link rel="next" until exhausted."""
        return list(self.iter_paginated(path, params))

    def iter_paginated(self, path: str, params: Optional[dict] = None) -> Iterator[Any]:
        """Yield items across all pages of a list endpoint."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url: Optional[str] = self._abs(path)
        first = True
        while url:
            resp = self._session.get(
                url,
                params=params if first else None,
                timeout=self.timeout,
            )
            first = False
            self._check(resp)
            page = resp.json()
            if isinstance(page, list):
                yield from page
            else:
                # Some list endpoints wrap results; yield the dict as-is.
                yield page
            url = _next_link(resp.headers.get("Link"))
