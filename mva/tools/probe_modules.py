"""
Dev-only probe: which module does the app treat as each course's "current week",
and why?

Logs in (prompts for creds), prints every course's modules with their Canvas state,
unlock date and parsed "w/c" date, marks the one get_current_module() picks, and
lists that module's items with their completion ticks. Raw module JSON is saved to
capture/probe_modules.json (git-ignored).

    .venv/bin/python tools/probe_modules.py
"""
import getpass
import json
import os
import sys

from mva_canvas import MvaCanvas
from mva_canvas.api import LOCAL_TZ
from mva_canvas.errors import CanvasError
from mva_canvas.models import now_utc


def main() -> int:
    api = MvaCanvas.from_credentials(input("Username: ").strip(), getpass.getpass("Password: "))
    today = now_utc().astimezone(LOCAL_TZ).date()
    dump: dict = {}
    print(f"\ntoday: {today}")
    for course in api.get_courses():
        print(f"\n=== {course.name} ({course.id})")
        try:
            modules = api.get_modules(course.id, with_items=False)
            current = api.get_current_module(course.id)
        except CanvasError as e:
            print(f"  error: {e}")
            continue
        dump[course.name] = {"modules": [m.raw for m in modules], "current": current.raw if current else None}
        for m in modules:
            mark = ">>" if current and m.id == current.id else "  "
            print(f"  {mark} pos={m.position:<3} state={m.state or '-':<10} "
                  f"unlock={m.unlock_at.date() if m.unlock_at else '-'}  week={m.week_start(today) or '-'}  {m.name}")
        if current:
            print(f"  items in picked module:")
            for it in current.items:
                req = it.raw.get("completion_requirement")
                tick = "-" if not req else ("done" if req.get("completed") else f"TODO ({req.get('type')})")
                print(f"     {it.type:<16} {tick:<24} {it.title}")
    os.makedirs("capture", exist_ok=True)
    with open("capture/probe_modules.json", "w") as f:
        json.dump(dump, f, indent=2, default=str)
    print("\nraw data saved to capture/probe_modules.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
