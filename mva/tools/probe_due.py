"""
Dev-only probe: why is homework missing from examples/due_this_week.py?

Logs in (prompts for creds), then dumps the raw responses that matter to
capture/probe_due.json (git-ignored) and prints a short summary.

    .venv/bin/python tools/probe_due.py [course_id assignment_id]
"""
import getpass
import json
import os
import sys
from datetime import timedelta

from mva_canvas import MvaCanvas
from mva_canvas.errors import CanvasError
from mva_canvas.models import now_utc


def main() -> int:
    course_id, assignment_id = (sys.argv[1], sys.argv[2]) if len(sys.argv) == 3 else ("2977", "314734")
    api = MvaCanvas.from_credentials(input("Username: ").strip(), getpass.getpass("Password: "))
    c = api.client
    out: dict = {}

    def grab(key, path, params=None, paged=False):
        try:
            out[key] = c.get_paginated(path, params=params) if paged else c.get(path, params=params)
        except CanvasError as e:
            out[key] = {"error": str(e)}
        return out[key]

    me = grab("self", "/users/self")
    observees = grab("observees", "/users/self/observees", paged=True)
    courses = grab("courses", "/courses", {"include[]": ["term", "observed_users"], "enrollment_state": "active"}, paged=True)
    todo = grab("todo", "/users/self/todo", paged=True)
    grab("assignment", f"/courses/{course_id}/assignments/{assignment_id}",
         {"include[]": ["submission", "observed_users"]})

    now = now_utc()
    window = {"start_date": now.isoformat(), "end_date": (now + timedelta(days=7)).isoformat()}
    grab("planner_self", "/planner/items", window, paged=True)
    if isinstance(observees, list):
        for o in observees:
            oid = o.get("id")
            grab(f"planner_{oid}", "/planner/items", {**window, "observed_user_id": oid}, paged=True)
            grab(f"submission_{oid}", f"/courses/{course_id}/assignments/{assignment_id}/submissions/{oid}")

    os.makedirs("capture", exist_ok=True)
    with open("capture/probe_due.json", "w") as f:
        json.dump(out, f, indent=2, default=str)

    print(f"\nself: {me.get('id')} {me.get('name')}")
    print(f"observees: {[(o.get('id'), o.get('name')) for o in observees] if isinstance(observees, list) else observees}")
    if isinstance(courses, list):
        for co in courses:
            types = [e.get("type") for e in co.get("enrollments", [])]
            print(f"  course {co.get('id')}: {co.get('name')}  enrollments={types}")
    print(f"todo items: {len(todo) if isinstance(todo, list) else todo}")
    a = out["assignment"]
    keys = ["name", "due_at", "unlock_at", "lock_at", "workflow_state", "has_submitted_submissions",
            "submission_types", "is_quiz_assignment", "quiz_id"]
    print("assignment:", {k: a.get(k) for k in keys} if "error" not in a else a)
    for k, v in out.items():
        if k.startswith("planner_"):
            print(f"{k}: {len(v) if isinstance(v, list) else v} items")
    print("\nFull dump written to capture/probe_due.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
