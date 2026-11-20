"""
Create a Canvas personal access token using the saved login session, and write it
to .env as CANVAS_TOKEN.

Canvas will not let you mint a token using another token (anti-escalation), so this
uses the browser *session* cookies plus the _csrf_token, exactly like the web UI's
"New Access Token" button does, via the documented Access Tokens API:
    POST /api/v1/users/self/tokens

The full token is written ONLY to .env (which is git-ignored). Nothing secret is
printed to stdout.
"""
import json
import pathlib
import urllib.parse

from playwright.sync_api import sync_playwright

BASE = pathlib.Path(__file__).resolve().parent.parent
PROFILE = BASE / ".pw-profile"
ENV = BASE / ".env"
HOST = "https://minervavirtual.instructure.com"
PURPOSE = "MVA Canvas API (Claude Code)"


def main():
    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(str(PROFILE), headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Make sure the session is live and cookies (incl. _csrf_token) are set.
        page.goto(HOST + "/profile/settings", wait_until="domcontentloaded")

        csrf = None
        for c in ctx.cookies():
            if c["name"] == "_csrf_token":
                csrf = urllib.parse.unquote(c["value"])
                break
        if not csrf:
            print("RESULT: ERROR no _csrf_token cookie; session may have expired.")
            ctx.close()
            return

        resp = page.request.post(
            HOST + "/api/v1/users/self/tokens",
            headers={
                "X-CSRF-Token": csrf,
                "Accept": "application/json",
            },
            form={"token[purpose]": PURPOSE},
        )
        status = resp.status
        try:
            data = resp.json()
        except Exception:
            data = {}
        ctx.close()

        if status not in (200, 201):
            # Don't leak anything sensitive; show status + canvas error message only.
            msg = data.get("errors") if isinstance(data, dict) else resp.text()[:200]
            print(f"RESULT: ERROR HTTP {status}: {json.dumps(msg)[:300]}")
            return

        token = data.get("visible_token")
        if not token:
            print(f"RESULT: ERROR no visible_token in response (HTTP {status}).")
            return

        # Write/merge .env without printing the token.
        write_env(token)
        print(f"RESULT: OK token created (purpose={PURPOSE!r}), "
              f"id={data.get('id')}, length={len(token)}, written to .env")


def write_env(token: str):
    lines = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                lines[k.strip()] = v
    lines.setdefault("CANVAS_BASE_URL", HOST)
    lines["CANVAS_TOKEN"] = token
    body = "\n".join(f"{k}={v}" for k, v in lines.items()) + "\n"
    ENV.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    main()
