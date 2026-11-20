"""
Interactive login-capture harness for the Minerva Virtual Canvas instance.

Launches a headed Firefox window (persistent profile) so Ben can log in by hand.
While the window is open it records every /api/v1/ request the Canvas web UI makes,
and once login is detected it probes the key Canvas endpoints and saves the real
JSON responses. All output goes under ./capture/ for the model to inspect.

Run:  .venv/Scripts/python.exe tools/capture_login.py
Stop: create the file ./capture/STOP  (or just close the Firefox window)
"""
import json
import time
import pathlib
from playwright.sync_api import sync_playwright

BASE = pathlib.Path(__file__).resolve().parent.parent
PROFILE = BASE / ".pw-profile"
OUT = BASE / "capture"
NETLOG = OUT / "network.jsonl"
STATUS = OUT / "status.txt"
STOP = OUT / "STOP"
PROBE_DIR = OUT / "probes"
OUT.mkdir(parents=True, exist_ok=True)
PROBE_DIR.mkdir(parents=True, exist_ok=True)

HOST = "https://minervavirtual.instructure.com"
LOGIN_URL = f"{HOST}/login/canvas"
API_MARK = "/api/v1/"

# Endpoints to probe once we are authenticated. These mirror what the API will do.
PROBES = {
    "users_self": "/api/v1/users/self",
    "courses_active": "/api/v1/courses?enrollment_state=active&include[]=term&include[]=total_students&per_page=100",
    "self_profile": "/api/v1/users/self/profile",
    "upcoming_events": "/api/v1/users/self/upcoming_events",
    "todo": "/api/v1/users/self/todo",
    "planner_items": "/api/v1/planner/items?per_page=50",
    "observees": "/api/v1/users/self/observees",
}


def log_status(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with STATUS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def main():
    # fresh status each run
    STATUS.write_text("", encoding="utf-8")
    # mutable flag shared with the response handler
    seen_api_ok = {"hit": False}

    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(
            str(PROFILE),
            headless=False,
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        netf = NETLOG.open("a", encoding="utf-8")

        def on_response(resp):
            try:
                url = resp.url
                if API_MARK in url:
                    rec = {
                        "t": round(time.time(), 2),
                        "method": resp.request.method,
                        "status": resp.status,
                        "url": url,
                    }
                    netf.write(json.dumps(rec) + "\n")
                    netf.flush()
                    # Any authenticated API call from the UI means we are logged in.
                    if resp.status < 400 and "/login" not in url:
                        seen_api_ok["hit"] = True
            except Exception:
                pass

        page.on("response", on_response)

        page.goto(LOGIN_URL)
        try:
            page.bring_to_front()
        except Exception:
            pass
        log_status("OPENED login page. Log in HERE, in this clean Firefox window.")

        logged_in = False
        probed = False
        last_hb = 0.0
        last_url = None
        deadline = time.time() + 40 * 60  # 40 min session
        while time.time() < deadline:
            if STOP.exists():
                log_status("STOP file found; shutting down.")
                break
            try:
                cur = page.url
            except Exception:
                cur = ""

            # Heartbeat: every ~5s, record the current URL + a live screenshot so we
            # can confirm which window the controlled browser is actually showing.
            now = time.time()
            if now - last_hb >= 5:
                last_hb = now
                try:
                    page.screenshot(path=str(OUT / "live.png"))
                except Exception:
                    pass
                if cur != last_url:
                    last_url = cur
                    try:
                        title = page.title()
                    except Exception:
                        title = "?"
                    log_status(f"HEARTBEAT url={cur!r} title={title!r}")
            on_canvas = cur.startswith(HOST)
            url_says_in = on_canvas and "/login" not in cur
            # Logged in if the URL left /login, OR the UI made a good API call.
            if (url_says_in or seen_api_ok["hit"]) and not logged_in:
                logged_in = True
                log_status(f"LOGGED_IN detected (url={cur!r}, api_ok={seen_api_ok['hit']})")

            if logged_in and not probed:
                # Give cookies a moment to settle, then probe via the browser context.
                time.sleep(2)
                try:
                    page.screenshot(path=str(OUT / "logged_in.png"))
                except Exception:
                    pass
                probe_all(page)
                probed = True
                log_status("PROBES complete. Results in capture/probes/. Window stays open.")
            time.sleep(2)

        netf.close()
        try:
            ctx.close()
        except Exception:
            pass
        log_status("Session closed.")


def probe_all(page):
    summary = {}
    for name, path in PROBES.items():
        url = HOST + path
        try:
            resp = page.request.get(url)
            status = resp.status
            body = resp.text()
            # Save raw body
            (PROBE_DIR / f"{name}.json").write_text(body, encoding="utf-8")
            # Short summary for status log
            preview = body[:200].replace("\n", " ")
            summary[name] = {"status": status, "len": len(body), "preview": preview}
            log_status(f"  probe {name}: HTTP {status}, {len(body)} bytes")
        except Exception as e:
            summary[name] = {"error": str(e)}
            log_status(f"  probe {name}: ERROR {e}")
    (PROBE_DIR / "_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
