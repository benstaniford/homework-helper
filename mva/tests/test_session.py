"""Tests for cookie-based session auth (the MVA path, since tokens are disabled)."""
import json

import pytest
import responses

from mva_canvas import MvaCanvas
from mva_canvas.session import load_cookies, requests_session_from_cookies

BASE = "https://canvas.test"
API = f"{BASE}/api/v1"

SAMPLE_COOKIES = [
    {"name": "canvas_session", "value": "sess-abc", "domain": "canvas.test", "path": "/"},
    {"name": "_csrf_token", "value": "csrf-xyz", "domain": "canvas.test", "path": "/"},
    {"name": "unrelated", "value": "x", "domain": "other.test", "path": "/"},
]


@pytest.fixture
def cookies_file(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps(SAMPLE_COOKIES), encoding="utf-8")
    return str(p)


def test_load_cookies_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_cookies(tmp_path / "nope.json")


def test_requests_session_has_canvas_cookie(cookies_file):
    sess = requests_session_from_cookies(cookies_file)
    assert sess.cookies.get("canvas_session") == "sess-abc"
    # No bearer auth is used for session mode.
    assert "Authorization" not in sess.headers


@responses.activate
def test_from_session_authenticates_with_cookies(cookies_file):
    responses.add(responses.GET, f"{API}/users/self",
                  json={"id": 8101, "name": "Ollie Student"}, status=200)
    api = MvaCanvas.from_session(cookies_path=cookies_file, base_url=BASE)
    me = api.get_self()
    assert me.id == 8101
    # The request carried the session cookie and NO Authorization header.
    sent = responses.calls[0].request
    assert "Authorization" not in sent.headers
    assert "canvas_session=sess-abc" in sent.headers.get("Cookie", "")


def test_client_requires_some_auth():
    from mva_canvas.client import CanvasClient
    with pytest.raises(ValueError):
        CanvasClient(base_url=BASE)  # neither token nor session
