"""
Export the Canvas login cookies from the saved Firefox profile (.pw-profile) to
.pw-session/cookies.json, which the library uses for session-based auth.

Run this once after logging in via tools/capture_login.py, and again whenever the
session expires (symptom: the API starts returning 401/403). It launches Firefox
headlessly, refreshes the session by hitting the dashboard, verifies it is still
authenticated, then writes the cookies.

    .venv/Scripts/python.exe tools/export_session.py
"""
import json
import pathlib

from playwright.sync_api import sync_playwright

BASE = pathlib.Path(__file__).resolve().parent.parent
PROFILE = BASE / ".pw-profile"
OUT_DIR = BASE / ".pw-session"
OUT = OUT_DIR / "cookies.json"
HOST = "https://minervavirtual.instructure.com"


def main():
    if not PROFILE.exists():
        print("RESULT: ERROR no saved profile; run tools/capture_login.py and log in first.")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(str(PROFILE), headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Verify the session is still valid by calling an authenticated endpoint.
        resp = page.request.get(HOST + "/api/v1/users/self")
        if resp.status != 200:
            ctx.close()
            print(f"RESULT: ERROR session not authenticated (HTTP {resp.status}). "
                  f"Re-run tools/capture_login.py and log in again.")
            return
        who = resp.json()

        cookies = ctx.cookies()
        OUT.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        ctx.close()

    print(f"RESULT: OK session valid for {who.get('name')!r} (id {who.get('id')}); "
          f"{len(cookies)} cookies written to {OUT.relative_to(BASE)}")


if __name__ == "__main__":
    main()
