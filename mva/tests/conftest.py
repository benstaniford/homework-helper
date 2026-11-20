"""
Shared fixtures for the MVA Canvas tests.

The sample payloads below are compact, hand-built records that mirror the exact
schema of the live MVA Canvas instance (captured during development), including its
awkward real-world edge cases: assignments with null due dates, past-due to-do
items, and assignments embedded in weekly modules as module items.
"""
import json

import pytest
import responses

from mva_canvas import MvaCanvas
from mva_canvas.client import CanvasClient

BASE_URL = "https://canvas.test"
API = f"{BASE_URL}/api/v1"
TOKEN = "test-token-123"

# --- sample data -------------------------------------------------------

USER_SELF = {"id": 8101, "name": "Ollie Student", "short_name": "Ollie"}

COURSES = [
    {"id": 3517, "name": "GCSE Biology", "course_code": "10BioTer", "workflow_state": "available"},
    {"id": 2799, "name": "GCSE Maths", "course_code": "10MaFFor", "workflow_state": "available"},
]

# Biology to-do: one outstanding submission, already past its due date.
TODO = [
    {
        "type": "submitting",
        "course_id": 3517,
        "context_name": "GCSE Biology",
        "html_url": "https://canvas.test/courses/3517/assignments/312378#submit",
        "assignment": {
            "id": 312378,
            "course_id": 3517,
            "name": "SUBMIT week 8: enzymes practical",
            "due_at": "2026-11-16T23:59:59Z",
            "html_url": "https://canvas.test/courses/3517/assignments/312378",
            "submission_types": ["online_upload"],
            "has_submitted_submissions": False,
            "workflow_state": "published",
        },
    }
]

# Biology assignments: a FUTURE dated one, a null-dated practice task, a past SUBMITTED one.
# Fetched with include[]=submission, so each carries the student's own `submission`.
ASSIGNMENTS_3517 = [
    {
        "id": 400001,
        "course_id": 3517,
        "name": "SUBMIT week 11: osmosis write-up",
        "due_at": "2027-01-10T23:59:59Z",
        "html_url": "https://canvas.test/courses/3517/assignments/400001",
        "submission_types": ["online_upload"],
        "has_submitted_submissions": False,
        "submission": {"user_id": 8101, "workflow_state": "unsubmitted", "submitted_at": None, "excused": None},
        "workflow_state": "published",
        "points_possible": 10.0,
    },
    {
        "id": 400002,
        "course_id": 3517,
        "name": "BUILD: enzyme recap (no deadline)",
        "due_at": None,
        "html_url": "https://canvas.test/courses/3517/assignments/400002",
        "submission_types": ["online_text_entry"],
        # Live quirk: true because classmates submitted; Ollie's own submission is not.
        "has_submitted_submissions": True,
        "submission": {"user_id": 8101, "workflow_state": "unsubmitted", "submitted_at": None, "excused": None},
        "workflow_state": "published",
    },
    {
        "id": 312378,
        "course_id": 3517,
        "name": "SUBMIT week 8: enzymes practical",
        "due_at": "2026-11-16T23:59:59Z",
        "html_url": "https://canvas.test/courses/3517/assignments/312378",
        "submission_types": ["online_upload"],
        "has_submitted_submissions": False,
        "submission": {"user_id": 8101, "workflow_state": "unsubmitted", "submitted_at": None, "excused": None},
        "workflow_state": "published",
    },
    {
        "id": 399000,
        "course_id": 3517,
        "name": "SUBMIT week 1: already done",
        "due_at": "2026-09-17T23:59:59Z",
        "html_url": "https://canvas.test/courses/3517/assignments/399000",
        "submission_types": ["online_upload"],
        "has_submitted_submissions": True,
        "submission": {"user_id": 8101, "workflow_state": "submitted", "submitted_at": "2026-09-15T18:00:00Z", "excused": None},
        "workflow_state": "published",
    },
]

ASSIGNMENTS_2799 = [
    {
        "id": 500001,
        "course_id": 2799,
        "name": "BUILD: BIDMAS practice (no deadline)",
        "due_at": None,
        "html_url": "https://canvas.test/courses/2799/assignments/500001",
        "submission_types": ["online_quiz"],
        "has_submitted_submissions": False,
        "submission": {"user_id": 8101, "workflow_state": "unsubmitted", "submitted_at": None, "excused": None},
        "workflow_state": "published",
    },
]

# Biology modules. Module 77961 (week 11) contains the future assignment 400001 as a
# module item, alongside a Page, a File, and a SubHeader (layout only).
MODULES_3517 = [
    {
        "id": 77854,
        "name": "Course Introduction",
        "position": 1,
        "state": "completed",
        "items_count": 2,
        "unlock_at": None,
        "items": [
            {"id": 1, "module_id": 77854, "title": "Welcome", "type": "Page",
             "html_url": "https://canvas.test/courses/3517/modules/items/1", "page_url": "welcome"},
        ],
    },
    {
        "id": 77961,
        "name": "WEEKS 11 - Osmosis practical (w/c 4th January)",
        "position": 2,
        "state": "started",
        "items_count": 4,
        "unlock_at": "2027-01-04T00:00:00Z",
        "items": [
            {"id": 10, "module_id": 77961, "title": "USEFUL RESOURCES", "type": "SubHeader"},
            {"id": 11, "module_id": 77961, "title": "Osmosis notes", "type": "Page",
             "html_url": "https://canvas.test/courses/3517/modules/items/11", "page_url": "osmosis-notes"},
            {"id": 12, "module_id": 77961, "title": "Practical worksheet.pdf", "type": "File",
             "html_url": "https://canvas.test/courses/3517/modules/items/12",
             "url": "https://canvas.test/api/v1/courses/3517/files/555"},
            {"id": 13, "module_id": 77961, "title": "SUBMIT week 11: osmosis write-up",
             "type": "Assignment", "content_id": 400001,
             "html_url": "https://canvas.test/courses/3517/modules/items/13"},
        ],
    },
]


# --- HTTP mock registration -------------------------------------------

def _register(rsps):
    rsps.add(responses.GET, f"{API}/users/self", json=USER_SELF, status=200)
    rsps.add(responses.GET, f"{API}/courses", json=COURSES, status=200)
    rsps.add(responses.GET, f"{API}/courses/3517", json=COURSES[0], status=200)
    rsps.add(responses.GET, f"{API}/courses/2799", json=COURSES[1], status=200)
    rsps.add(responses.GET, f"{API}/users/self/todo", json=TODO, status=200)
    rsps.add(responses.GET, f"{API}/courses/3517/assignments", json=ASSIGNMENTS_3517, status=200)
    rsps.add(responses.GET, f"{API}/courses/2799/assignments", json=ASSIGNMENTS_2799, status=200)
    rsps.add(responses.GET, f"{API}/courses/3517/modules", json=MODULES_3517, status=200)
    rsps.add(responses.GET, f"{API}/courses/2799/modules", json=[], status=200)
    rsps.add(responses.GET, f"{API}/courses/3517/modules/77961",
             json=MODULES_3517[1], status=200)


@pytest.fixture
def mock_rsps():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _register(rsps)
        yield rsps


@pytest.fixture
def api(mock_rsps):
    return MvaCanvas(token=TOKEN, base_url=BASE_URL)


@pytest.fixture
def client(mock_rsps):
    return CanvasClient(base_url=BASE_URL, token=TOKEN)
