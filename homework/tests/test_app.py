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
        _assignment(10, "Ancient essay", due=now - timedelta(days=15)),
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


def test_stage_for():
    s = homework_data.stage_for
    assert s("PREPARE: Revision (45 minutes)") == "Prepare"
    assert s("Prepare: Newspaper Articles") == "Prepare"
    assert s("SUBMIT 2: Analyse a metope (40 mins)") == "Submit"
    assert s("BUILD AND DEVELOP: Short and Long Term Impacts") == "Build"
    assert s("BUILD- Osmosis graph drawing (30 MINS)") == "Build"
    assert s("EXTENSION: Deepen Your Understanding") == "Extend"
    assert s("Confidence Checker", "Confidence Checker End of Unit") == "Other"
    assert s("Personalised periodic table", "EXTEND 💠") == "Extend"
    assert s("Osmosis worksheet", "LIVE LESSON 1  💻") == "Live lesson"
    assert s("Before This Module I Can", None) == "Start"
    assert s("START HERE: Download/ print the module key sheet", "WEEK A") == "Start"
    assert s("CHECK: Before starting this module I can... (5 mins)") == "Start"


def test_this_week_grouped_by_course_and_stage():
    fake = make_fake()
    fake.module.items = [
        ModuleItem(id=20, module_id=10, title="LIVE LESSON 1", type="SubHeader"),
        ModuleItem(id=21, module_id=10, title="Build task", type="Assignment", content_id=4),
        ModuleItem(id=22, module_id=10, title="FOCUS 🔷", type="SubHeader"),
        ModuleItem(id=23, module_id=10, title="PREPARE: Revision", type="Page", html_url="u",
                   raw={"completion_requirement": {"type": "must_mark_done", "completed": False}}),
    ]
    rep = homework_data.build_report(fake)
    assert [g.course_name for g in rep.this_week] == ["Maths"]
    maths = rep.this_week[0]
    assert maths.module_name == "Week 12"
    assert maths.count == 3
    assert [(s.name, [i.title for i in s.items]) for s in maths.stages] == [
        ("Prepare", ["PREPARE: Revision"]),
        ("Build", ["Build task"]),  # title prefix beats the LIVE LESSON heading
        ("Other", ["Soon worksheet"]),
    ]


def test_this_week_shows_done_items_ticked():
    fake = make_fake()
    fake.assignments.append(_assignment(8, "SHARE: Done share", submitted=True))
    fake.assignments.append(_assignment(9, "BUILD: Marked done"))
    done = {"type": "must_view", "completed": True}
    fake.module.items = [
        ModuleItem(id=30, module_id=10, title="PREPARE: Experiments", type="Page", html_url="u",
                   raw={"completion_requirement": done}),
        ModuleItem(id=31, module_id=10, title="PREPARE: Revision", type="Page", html_url="u",
                   raw={"completion_requirement": {"type": "must_mark_done", "completed": False}}),
        ModuleItem(id=32, module_id=10, title="SHARE: Done share", type="Assignment", content_id=8),
        ModuleItem(id=33, module_id=10, title="BUILD: Marked done", type="Assignment", content_id=9,
                   raw={"completion_requirement": {"type": "must_mark_done", "completed": True}}),
        ModuleItem(id=34, module_id=10, title="Learning objectives", type="Page", html_url="u"),
    ]
    rep = homework_data.build_report(fake)
    stages = {s.name: [(i.title, i.done) for i in s.items] for s in rep.this_week[0].stages}
    # outstanding first, then done; reference pages with no tick are left out
    assert stages["Prepare"] == [("PREPARE: Revision", False), ("PREPARE: Experiments", True)]
    assert stages["Share"] == [("SHARE: Done share", True)]
    assert stages["Build"] == [("BUILD: Marked done", True)]  # Canvas tick counts, not just submission
    assert rep.this_week[0].count == 2  # PREPARE: Revision + Soon worksheet
    assert "PREPARE: Experiments" not in [i.title for i in rep.this_week_tasks]


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
def mva():
    """What the fake MVA accepts; tests change .password to simulate a rejection."""
    return SimpleNamespace(password="right")


@pytest.fixture
def client(monkeypatch, mva):
    fake = make_fake()

    def login(u, p):
        if p != mva.password:
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


def test_auth_failure_keeps_saved_login(client, mva):
    client.post("/login", data={"username": "kid", "password": "right"})
    mva.password = "changed"  # MVA now refuses the saved password
    app_module.cache.forget("kid")  # as after a restart: the next request logs in to MVA
    r = client.get("/?refresh=1")
    assert r.status_code == 502
    assert "saved login" in r.data.decode()

    mva.password = "right"  # MVA accepts it again: still logged in
    r = client.get("/")
    assert r.status_code == 200
    assert "Test Student" in r.data.decode()
