"""
Username/password login for Canvas instances that use Canvas's own login form
(``/login/canvas``), as MVA does.

This does exactly what the browser does, with plain ``requests`` and no browser:

  1. GET  /login/canvas  -> the form's hidden ``authenticity_token`` (and a
                            ``_csrf_token`` cookie);
  2. POST /login/canvas  -> ``pseudonym_session[unique_id]`` / ``[password]``;
                            on success Canvas redirects away from /login and
                            sets an authenticated ``canvas_session`` cookie;
  3. GET  /api/v1/users/self to confirm the session really works.

The resulting session is the same cookie session ``session.py`` builds from an
exported cookie file, so the rest of the library uses it unchanged.

Limitations: this cannot get past SSO, two-factor authentication or a CAPTCHA;
those fail with ``CanvasAuthError``.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from typing import Optional

import requests

from .errors import CanvasAuthError, CanvasHTTPError
from .session import new_browser_session

LOGIN_PATH = "/login/canvas"

_TOKEN_RE = re.compile(
    r'<input[^>]*name="authenticity_token"[^>]*value="([^"]*)"'
    r'|<input[^>]*value="([^"]*)"[^>]*name="authenticity_token"',
)


def _authenticity_token(page_html: str, sess: requests.Session) -> str:
    """The login form's CSRF token, falling back to the _csrf_token cookie."""
    m = _TOKEN_RE.search(page_html)
    if m:
        return html.unescape(m.group(1) or m.group(2))
    cookie = sess.cookies.get("_csrf_token")
    if cookie:
        return urllib.parse.unquote(cookie)
    raise CanvasAuthError(
        "Could not find the authenticity token on the Canvas login page; "
        "the login form may have changed or this instance may use SSO."
    )


def login_with_credentials(
    username: str,
    password: str,
    base_url: str,
    timeout: float = 30.0,
    session: Optional[requests.Session] = None,
) -> requests.Session:
    """
    Log in to Canvas with a username and password and return an authenticated
    ``requests.Session`` carrying the ``canvas_session`` cookie.

    Raises ``CanvasAuthError`` if the credentials are rejected, if Canvas asks
    for a second factor, or if the resulting session is not authenticated.
    """
    if not username or not password:
        raise ValueError("username and password are both required")

    base_url = base_url.rstrip("/")
    login_url = base_url + LOGIN_PATH
    sess = session or new_browser_session()

    page = sess.get(login_url, headers={"Accept": "text/html"}, timeout=timeout)
    if page.status_code != 200:
        raise CanvasHTTPError(page.status_code, login_url, page.text)
    token = _authenticity_token(page.text, sess)

    resp = sess.post(
        login_url,
        data={
            "utf8": "✓",
            "authenticity_token": token,
            "redirect_to_ssl": "1",
            "pseudonym_session[unique_id]": username,
            "pseudonym_session[password]": password,
            "pseudonym_session[remember_me]": "0",
        },
        headers={"Accept": "text/html", "Referer": login_url},
        allow_redirects=False,
        timeout=timeout,
    )

    # Success is a redirect away from /login. A wrong password re-renders the
    # form (400, or 200) or redirects back to the login page.
    location = resp.headers.get("Location", "")
    if resp.status_code not in (301, 302, 303):
        raise CanvasAuthError("Canvas rejected the username or password.")
    target = urllib.parse.urljoin(login_url, location)
    target_path = urllib.parse.urlparse(target).path
    if target_path.startswith("/login/otp"):
        raise CanvasAuthError(
            "Canvas asked for a two-factor code; username/password login "
            "cannot complete this. Use the browser login tools instead."
        )
    if target_path.startswith("/login"):
        raise CanvasAuthError("Canvas rejected the username or password.")

    check = sess.get(base_url + "/api/v1/users/self", timeout=timeout)
    if check.status_code != 200:
        raise CanvasAuthError(
            f"Login appeared to succeed but the API returned HTTP "
            f"{check.status_code} for /api/v1/users/self."
        )
    return sess
