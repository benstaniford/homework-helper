"""Tests for model parsing and small behaviours."""
from datetime import datetime, timezone

from mva_canvas.models import (
    Assignment,
    Homework,
    Module,
    parse_dt,
)


def test_parse_dt_handles_z_and_none():
    assert parse_dt(None) is None
    assert parse_dt("") is None
    dt = parse_dt("2026-11-16T23:59:59Z")
    assert dt == datetime(2026, 11, 16, 23, 59, 59, tzinfo=timezone.utc)
    assert parse_dt("not-a-date") is None


def test_assignment_from_api_with_null_due():
    a = Assignment.from_api({
        "id": 1, "course_id": 9, "name": "BUILD: recap", "due_at": None,
        "workflow_state": "published", "submission_types": ["online_text_entry"],
    })
    assert a.due_at is None
    assert a.is_published
    assert a.has_submitted is False


def test_module_finds_assignment_item():
    m = Module.from_api({
        "id": 5, "name": "Week 11", "position": 2, "state": "started",
        "items": [
            {"id": 1, "title": "Header", "type": "SubHeader"},
            {"id": 2, "title": "Task", "type": "Assignment", "content_id": 400001},
        ],
    })
    item = m.assignment_item(400001)
    assert item is not None and item.content_id == 400001
    assert m.assignment_item(999) is None
    # SubHeader is layout, not a material
    assert m.items[0].is_material is False
    assert m.items[1].is_material is True


def test_homework_overdue_flag():
    hw = Homework.from_todo({
        "course_id": 1, "context_name": "Bio", "html_url": "u",
        "assignment": {"id": 2, "name": "x", "due_at": "2000-01-01T00:00:00Z",
                       "has_submitted_submissions": False},
    })
    assert hw.is_overdue is True
    assert hw.source == "todo"


def test_submitted_uses_own_submission_not_classmates():
    # Live quirk: has_submitted_submissions is true once ANY student submits.
    a = Assignment.from_api({"id": 1, "course_id": 2, "name": "BUILD", "due_at": None,
                             "has_submitted_submissions": True,
                             "submission": {"workflow_state": "unsubmitted", "submitted_at": None}})
    assert a.has_submitted is False


def test_submitted_states():
    def done(sub):
        return Assignment.from_api({"id": 1, "course_id": 2, "submission": sub}).has_submitted
    assert done({"workflow_state": "submitted"}) is True
    assert done({"workflow_state": "graded", "submitted_at": "2026-11-01T10:00:00Z"}) is True
    assert done({"workflow_state": "unsubmitted", "excused": True}) is True
    assert done([{"workflow_state": "graded"}]) is True   # include[]=observed_users shape
    assert done({"workflow_state": "unsubmitted"}) is False
    assert Assignment.from_api({"id": 1, "course_id": 2}).has_submitted is False  # no submission info
