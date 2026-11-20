"""Tests for username/password login (mocked HTTP; mirrors the real MVA login form)."""
import urllib.parse

import pytest
import responses

from mva_canvas import CanvasAuthError, MvaCanvas
from mva_canvas.login import login_with_credentials

BASE = "https://canvas.test"
LOGIN = f"{BASE}/login/canvas"
API = f"{BASE}/api/v1"

# The relevant part of the live /login/canvas page.
LOGIN_HTML = """
<form id="login_form" action="/login/canvas" accept-charset="UTF-8" method="post">
<input name="utf8" type="hidden" value="&#x2713;" autocomplete="off" />
<input type="hidden" name="authenticity_token" value="tok+en/abc==" autocomplete="off" />
<input type="hidden" name="redirect_to_ssl" value="1"/>
<input class="ic-Input text" type="text" name="pseudonym_session[unique_id]" id="pseudonym_session_unique_id" />
<input class="ic-Input text" type="password" name="pseudonym_session[password]" />
</form>
"""


def _login_page(rsps):
    rsps.add(responses.GET, LOGIN, body=LOGIN_HTML, status=200,
             headers={"Set-Cookie": "_csrf_token=cookie-csrf; path=/"})


def _form(call) -> dict[str, str]:
    body = call.request.body
    if isinstance(body, bytes):
        body = body.decode()
    return dict(urllib.parse.parse_qsl(body))


@responses.activate
def test_successful_login_returns_authenticated_session():
    _login_page(responses)
    responses.add(responses.POST, LOGIN, status=302,
                  headers={"Location": f"{BASE}/?login_success=1",
                           "Set-Cookie": "canvas_session=logged-in; path=/"})
    responses.add(responses.GET, f"{API}/users/self",
                  json={"id": 8101, "name": "Ollie Student"}, status=200)

    sess = login_with_credentials("ollie", "s3cret", base_url=BASE)

    assert sess.cookies.get("canvas_session") == "logged-in"
    form = _form(responses.calls[1])
    assert form["authenticity_token"] == "tok+en/abc=="
    assert form["pseudonym_session[unique_id]"] == "ollie"
    assert form["pseudonym_session[password]"] == "s3cret"
    # The verification call carried the session cookie.
    assert "canvas_session=logged-in" in responses.calls[2].request.headers["Cookie"]


@responses.activate
def test_from_credentials_builds_working_api():
    _login_page(responses)
    responses.add(responses.POST, LOGIN, status=302,
                  headers={"Location": "/", "Set-Cookie": "canvas_session=ok; path=/"})
    responses.add(responses.GET, f"{API}/users/self",
                  json={"id": 8101, "name": "Ollie Student"}, status=200)

    api = MvaCanvas.from_credentials("ollie", "s3cret", base_url=BASE)
    assert api.get_self().id == 8101
    assert "Authorization" not in responses.calls[-1].request.headers


@responses.activate
@pytest.mark.parametrize("status,headers", [
    (400, {}),                                  # form re-rendered with an error
    (200, {}),
    (302, {"Location": f"{BASE}/login/canvas"}),
])
def test_bad_password_raises_auth_error(status, headers):
    _login_page(responses)
    responses.add(responses.POST, LOGIN, status=status, headers=headers, body="Invalid")
    with pytest.raises(CanvasAuthError, match="rejected"):
        login_with_credentials("ollie", "wrong", base_url=BASE)


@responses.activate
def test_two_factor_prompt_raises_auth_error():
    _login_page(responses)
    responses.add(responses.POST, LOGIN, status=302, headers={"Location": "/login/otp"})
    with pytest.raises(CanvasAuthError, match="two-factor"):
        login_with_credentials("ollie", "s3cret", base_url=BASE)


@responses.activate
def test_unauthenticated_after_redirect_raises():
    _login_page(responses)
    responses.add(responses.POST, LOGIN, status=302, headers={"Location": "/"})
    responses.add(responses.GET, f"{API}/users/self", status=401)
    with pytest.raises(CanvasAuthError, match="401"):
        login_with_credentials("ollie", "s3cret", base_url=BASE)


@responses.activate
def test_falls_back_to_csrf_cookie_when_form_token_missing():
    responses.add(responses.GET, LOGIN, body="<form></form>", status=200,
                  headers={"Set-Cookie": "_csrf_token=from%2Bcookie; path=/"})
    responses.add(responses.POST, LOGIN, status=302, headers={"Location": "/"})
    responses.add(responses.GET, f"{API}/users/self", json={"id": 1}, status=200)
    login_with_credentials("ollie", "s3cret", base_url=BASE)
    assert _form(responses.calls[1])["authenticity_token"] == "from+cookie"


def test_missing_credentials_rejected():
    with pytest.raises(ValueError):
        login_with_credentials("", "x", base_url=BASE)


@responses.activate
def test_from_env_uses_credentials_when_no_token(monkeypatch, tmp_path):
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    monkeypatch.setenv("CANVAS_BASE_URL", BASE)
    monkeypatch.setenv("CANVAS_USERNAME", "ollie")
    monkeypatch.setenv("CANVAS_PASSWORD", "s3cret")
    _login_page(responses)
    responses.add(responses.POST, LOGIN, status=302, headers={"Location": "/"})
    responses.add(responses.GET, f"{API}/users/self", json={"id": 8101}, status=200)

    api = MvaCanvas.from_env(dotenv_path=str(tmp_path / "none.env"))
    assert api.get_self().id == 8101
