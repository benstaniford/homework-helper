"""Tests for the Homework web app. MVA Canvas is mocked; no network."""
import os
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")

import app as app_module  # noqa: E402
import homework_data  # noqa: E402
from mva_canvas.errors import CanvasAuthError, CanvasHTTPError  # noqa: E402
from mva_canvas.models import Assignment, Module, ModuleItem, now_utc  # noqa: E402


def _assignment(id, name, due=None, submitted=False, published=True, course_id=1):
    a = Assignment(
        id=id, course_id=course_id, name=name, due_at=due,
        html_url=f"https://canvas.example/courses/{course_id}/assignments/{id}",
        has_submitted=submitted, workflow_state="published" if published else "unpublished",
    )
    return a


class FakeApi:
    def __init__(self, assignments, module=None, failing_courses=()):
        self.assignments = assignments
        self.module = module
        self.failing = set(failing_courses)
        self.calls = 0

    def get_self(self):
        self.calls += 1
        return SimpleNamespace(name="Test Student")

    def get_courses(self):
        return [SimpleNamespace(id=1, name="Maths"), SimpleNamespace(id=2, name="Art")]

    def get_assignments(self, course_id):
        if course_id in self.failing:
            raise CanvasHTTPError(500, "x")
        return [a for a in self.assignments if a.course_id == course_id]

    def get_current_module(self, course_id):
        return self.module if course_id == 1 else None


def make_fake():
    now = now_utc()
    assignments = [
        _assignment(1, "Late essay", due=now - timedelta(days=2)),
        _assignment(2, "Soon worksheet", due=now + timedelta(days=2)),
        _assignment(3, "Later project", due=now + timedelta(days=20)),
        _assignment(4, "Build task", due=None),
        _assignment(5, "Not this week", due=None),
        _assignment(6, "Done already", due=now + timedelta(days=1), submitted=True),
        _assignment(7, "Draft", due=now + timedelta(days=1), published=False),
    ]
    module = Module(id=10, name="Week 12", position=12,
                    items=[ModuleItem(id=1, module_id=10, title="Build task", type="Assignment", content_id=4)])
    return FakeApi(assignments, module)


# -- report classification ---------------------------------------------------

def test_build_report_buckets():
    rep = homework_data.build_report(make_fake())
    assert rep.user_name == "Test Student"
    assert [i.title for i in rep.overdue] == ["Late essay"]
    assert [i.title for i in rep.this_week_due] == ["Soon worksheet"]
    assert [i.title for i in rep.upcoming] == ["Later project"]
    assert [(i.title, i.module_name) for i in rep.this_week_tasks] == [("Build task", "Week 12")]
    assert rep.overdue[0].html_url.endswith("/assignments/1")


def test_build_report_lists_unfinished_module_pages():
    fake = make_fake()

    def page(id, title, req=None):
        raw = {"completion_requirement": req} if req else {}
        return ModuleItem(id=id, module_id=10, title=title, type="Page",
                          html_url=f"https://canvas.example/items/{id}", raw=raw)

    fake.module.items += [
        page(2, "PREPARE: Revision", {"type": "must_mark_done", "completed": False}),
        page(3, "PREPARE: Experiments", {"type": "must_view", "completed": True}),
        page(4, "Learning objectives"),  # no requirement: reference material
        ModuleItem(id=5, module_id=10, title="WEEK B", type="SubHeader"),
    ]
    rep = homework_data.build_report(fake)
    assert [i.title for i in rep.this_week_tasks] == ["Build task", "PREPARE: Revision"]
    assert rep.this_week_tasks[1].html_url == "https://canvas.example/items/2"


def test_build_report_skips_failing_course():
    fake = make_fake()
    fake.failing = {2}
    rep = homework_data.build_report(fake)
    assert rep.skipped == ["Art"]


def test_session_cache_relogs_on_auth_error():
    logins = []

    class Expiring(FakeApi):
        def get_self(self):
            if not logins or len(logins) == 1:
                raise CanvasAuthError("expired")
            return super().get_self()

    def login(u, p):
        logins.append(u)
        return Expiring([]) if len(logins) == 1 else FakeApi([])

    cache = homework_data.SessionCache(login=login)
    cache.login("kid", "pw")
    rep = cache.report("kid", "pw")
    assert rep.user_name == "Test Student"
    assert len(logins) == 2


def test_report_is_cached():
    fake = make_fake()
    cache = homework_data.SessionCache(login=lambda u, p: fake)
    cache.report("kid", "pw")
    cache.report("kid", "pw")
    assert fake.calls == 1
    cache.report("kid", "pw", refresh=True)
    assert fake.calls == 2


# -- Flask routes ---------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    fake = make_fake()

    def login(u, p):
        if p != "right":
            raise CanvasAuthError("bad")
        return fake

    monkeypatch.setattr(app_module, "cache", homework_data.SessionCache(login=login))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "healthy"


def test_index_requires_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_bad_login(client):
    r = client.post("/login", data={"username": "kid", "password": "wrong"})
    assert r.status_code == 200
    assert b"MVA rejected" in r.data


def test_good_login_is_remembered_and_shows_homework(client):
    r = client.post("/login", data={"username": "kid", "password": "right"})
    assert r.status_code == 302
    cookie = r.headers["Set-Cookie"]
    assert "Expires=" in cookie and "HttpOnly" in cookie
    assert "right" not in cookie  # password is encrypted, not in clear

    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Test Student" in html
    assert "Soon worksheet" in html and "Late essay" in html and "Later project" in html
    assert 'href="https://canvas.example/courses/1/assignments/2"' in html
    assert "<details class=\"overdue\">" in html


def test_login_redirects_only_locally(client):
    r = client.post("/login?next=https://evil.example/", data={"username": "kid", "password": "right"})
    assert r.headers["Location"] == "/"


def test_logout(client):
    client.post("/login", data={"username": "kid", "password": "right"})
    client.get("/logout")
    assert client.get("/").status_code == 302
