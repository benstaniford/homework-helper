"""
Deep, headless probe that reuses the saved login session (.pw-profile) to capture
the richer Canvas endpoints we need: per-course assignments (incl. upcoming),
course modules/materials, pages, and a cross-course upcoming-homework view.

No user interaction: relies on the 'stay signed in' session already in the profile.
Run: .venv/Scripts/python.exe tools/probe_deep.py
"""
import json
import pathlib
from playwright.sync_api import sync_playwright

BASE = pathlib.Path(__file__).resolve().parent.parent
PROFILE = BASE / ".pw-profile"
OUT = BASE / "capture" / "deep"
OUT.mkdir(parents=True, exist_ok=True)
HOST = "https://minervavirtual.instructure.com"

# A few representative real courses (from the first probe).
SAMPLE_COURSES = {
    3517: "Biology",
    2799: "Maths-Foundation",
    3092: "English-Literature",
}

# Cross-course endpoints (account-wide for this student).
GLOBAL_PROBES = {
    # Upcoming assignments across all courses via planner with a forward date window.
    "planner_upcoming": "/api/v1/planner/items?start_date=2026-11-20&end_date=2027-02-01&per_page=100",
    # Account calendar assignment events, forward window, all contexts.
    "self_enrollments": "/api/v1/users/self/enrollments?state[]=active&per_page=100",
}

# Per-course endpoints.
def course_probes(cid):
    return {
        f"course_{cid}_assignments": f"/api/v1/courses/{cid}/assignments?order_by=due_at&per_page=100",
        f"course_{cid}_assignments_upcoming": f"/api/v1/courses/{cid}/assignments?bucket=upcoming&per_page=100",
        f"course_{cid}_modules": f"/api/v1/courses/{cid}/modules?include[]=items&include[]=content_details&per_page=100",
        f"course_{cid}_pages": f"/api/v1/courses/{cid}/pages?per_page=100",
        f"course_{cid}_front_page": f"/api/v1/courses/{cid}/front_page",
    }


def main():
    results = {}
    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(str(PROFILE), headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        probes = dict(GLOBAL_PROBES)
        for cid in SAMPLE_COURSES:
            probes.update(course_probes(cid))

        for name, path in probes.items():
            url = HOST + path
            try:
                resp = page.request.get(url)
                body = resp.text()
                (OUT / f"{name}.json").write_text(body, encoding="utf-8")
                results[name] = {"status": resp.status, "len": len(body)}
                print(f"{name}: HTTP {resp.status}, {len(body)} bytes", flush=True)
            except Exception as e:
                results[name] = {"error": str(e)}
                print(f"{name}: ERROR {e}", flush=True)

        # Check whether this account can create a Personal Access Token.
        # The settings page contains the 'New Access Token' control if allowed.
        try:
            page.goto(HOST + "/profile/settings", wait_until="domcontentloaded")
            html = page.content()
            has_token_ui = "access_token" in html.lower() or "New Access Token" in html
            (OUT / "_settings_page.html").write_text(html, encoding="utf-8")
            results["_token_ui_available"] = has_token_ui
            print(f"token UI on settings page: {has_token_ui}", flush=True)
        except Exception as e:
            results["_token_ui_available"] = f"error: {e}"
            print(f"settings page check ERROR {e}", flush=True)

        ctx.close()
    (OUT / "_summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\nDONE. Summary written to capture/deep/_summary.json")


if __name__ == "__main__":
    main()
