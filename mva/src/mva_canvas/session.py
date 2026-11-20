"""
Cookie-based authentication for Canvas instances that disable personal access
tokens (as the MVA instance does).

The Canvas web UI authenticates its own /api/v1 calls with the ``canvas_session``
cookie; read-only GETs need nothing more (CSRF is only required for writes, which
this library never does). So we can log in once in a browser, export the cookies,
and hand them to plain ``requests`` — no browser in the request path.

Use ``tools/export_session.py`` to produce the cookies file from a saved login.
"""
from __future__ import annotations

import json
import pathlib
from typing import Union

import requests

PathLike = Union[str, pathlib.Path]

# A browser carries lots of cookies; these are the ones Canvas auth actually needs.
AUTH_COOKIE_NAMES = {"canvas_session", "_csrf_token", "log_session_id"}


def load_cookies(cookies_path: PathLike) -> list[dict]:
    """Load the Playwright-exported cookie list from JSON."""
    path = pathlib.Path(cookies_path)
    if not path.exists():
        raise FileNotFoundError(
            f"No session cookies at {path}. Run tools/export_session.py first "
            f"(after logging in via tools/capture_login.py)."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    # Accept either a bare list or {"cookies": [...]}.
    return data["cookies"] if isinstance(data, dict) else data


def requests_session_from_cookies(cookies_path: PathLike) -> requests.Session:
    """
    Build a requests.Session pre-loaded with the Canvas auth cookies and a
    browser-like User-Agent (some instances are picky about missing UAs).
    """
    cookies = load_cookies(cookies_path)
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (mva-canvas session client)",
            "Accept": "application/json+canvas-string-ids, application/json",
        }
    )
    for c in cookies:
        name = c.get("name")
        if not name:
            continue
        sess.cookies.set(
            name,
            c.get("value", ""),
            domain=c.get("domain", "").lstrip("."),
            path=c.get("path", "/"),
        )
    return sess
