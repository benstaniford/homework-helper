"""Tests for the low-level CanvasClient: auth, errors, pagination."""
import pytest
import responses

from mva_canvas.client import CanvasClient, _next_link
from mva_canvas.errors import (
    CanvasAuthError,
    CanvasNotFoundError,
    CanvasHTTPError,
)

BASE = "https://canvas.test"
API = f"{BASE}/api/v1"


def test_requires_base_url_and_token():
    with pytest.raises(ValueError):
        CanvasClient(base_url="", token="x")
    with pytest.raises(ValueError):
        CanvasClient(base_url=BASE, token="")


def test_sends_bearer_token(client, mock_rsps):
    client.get("/users/self")
    assert mock_rsps.calls[0].request.headers["Authorization"] == "Bearer test-token-123"


@responses.activate
def test_auth_error_maps_to_exception():
    responses.add(responses.GET, f"{API}/users/self", status=401, json={"errors": "nope"})
    c = CanvasClient(base_url=BASE, token="bad")
    with pytest.raises(CanvasAuthError):
        c.get("/users/self")


@responses.activate
def test_not_found_maps_to_exception():
    responses.add(responses.GET, f"{API}/courses/999", status=404, body="not found")
    c = CanvasClient(base_url=BASE, token="t")
    with pytest.raises(CanvasNotFoundError):
        c.get("/courses/999")


@responses.activate
def test_other_http_error():
    responses.add(responses.GET, f"{API}/boom", status=500, body="server error")
    c = CanvasClient(base_url=BASE, token="t")
    with pytest.raises(CanvasHTTPError) as ei:
        c.get("/boom")
    assert ei.value.status == 500


@responses.activate
def test_pagination_follows_link_header():
    page1 = [{"id": 1}, {"id": 2}]
    page2 = [{"id": 3}]
    responses.add(
        responses.GET,
        f"{API}/things",
        json=page1,
        status=200,
        headers={"Link": f'<{API}/things?page=2>; rel="next", <{API}/things?page=2>; rel="last"'},
    )
    responses.add(responses.GET, f"{API}/things", json=page2, status=200)
    c = CanvasClient(base_url=BASE, token="t")
    out = c.get_paginated("/things")
    assert [x["id"] for x in out] == [1, 2, 3]


def test_next_link_parser():
    hdr = '<https://x/api/v1/c?page=2>; rel="next", <https://x/api/v1/c?page=9>; rel="last"'
    assert _next_link(hdr) == "https://x/api/v1/c?page=2"
    assert _next_link(None) is None
    assert _next_link('<https://x>; rel="last"') is None
