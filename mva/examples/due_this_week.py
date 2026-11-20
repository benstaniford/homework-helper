"""
POC: ask for MVA Canvas credentials, log in, and list the student's unsubmitted
work in three groups:

  * overdue           - due date has passed, not submitted;
  * due soon          - due within the next N days (default 7);
  * this week's tasks - no due date, but in each course's current weekly module
                        (most MVA "BUILD:" tasks have no deadline).

    .venv/bin/python examples/due_this_week.py [--days N]
"""
import argparse
import getpass
import sys
from datetime import timedelta

from mva_canvas import MvaCanvas
from mva_canvas.errors import CanvasAuthError, CanvasError
from mva_canvas.models import Homework, now_utc


def show(heading: str, items: list[tuple[Homework, str]]) -> None:
    print(f"\n{heading} ({len(items)}):")
    if not items:
        print("  none")
    for hw, note in items:
        when = hw.due_at.astimezone().strftime("%a %d %b %H:%M") if hw.due_at else "no due date"
        print(f"  {when:16}  {hw.course_name}: {hw.title}{note}")
        if hw.html_url:
            print(f"  {'':16}  {hw.html_url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="List unsubmitted MVA work: overdue, due soon, and this week's tasks.")
    parser.add_argument("--days", type=int, default=7, help="look-ahead window in days (default 7)")
    args = parser.parse_args()

    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")

    try:
        api = MvaCanvas.from_credentials(username, password)
    except CanvasAuthError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        return 1

    me = api.get_self()
    print(f"\nLogged in as: {me.name}")

    now = now_utc()
    horizon = now + timedelta(days=args.days)
    overdue, due_soon, this_week = [], [], []

    for course in api.get_courses():
        try:
            assignments = api.get_assignments(course.id)
            module = api.get_current_module(course.id)
        except CanvasError as e:
            print(f"  (skipped {course.name}: {e})", file=sys.stderr)
            continue
        week_ids = {it.content_id for it in module.items if it.type == "Assignment"} if module else set()

        for a in assignments:
            if not a.is_published or a.has_submitted:
                continue
            hw = Homework.from_assignment(a, course_name=course.name)
            if a.due_at is None:
                if a.id in week_ids:
                    this_week.append((hw, f"  [{module.name}]" if module else ""))
            elif a.due_at < now:
                overdue.append((hw, ""))
            elif a.due_at <= horizon:
                due_soon.append((hw, ""))

    overdue.sort(key=lambda x: x[0].due_at)
    due_soon.sort(key=lambda x: x[0].due_at)
    this_week.sort(key=lambda x: (x[0].course_name, x[0].title))

    show("Overdue", overdue)
    show(f"Due in the next {args.days} days", due_soon)
    show("This week's tasks (no due date)", this_week)
    return 0


if __name__ == "__main__":
    sys.exit(main())
