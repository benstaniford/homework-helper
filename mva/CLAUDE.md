# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`mva-canvas` is a small, typed Python wrapper over the **Minerva Virtual Academy**
Canvas LMS instance (`minervavirtual.instructure.com`). It answers three questions
about a student: which courses they take, what homework is outstanding/upcoming,
and what materials belong to a given piece of homework. It is a thin layer over the
standard Canvas REST API — no HTML scraping.

## Commands

The venv lives at `.venv/`. Its layout depends on the OS that created it: on
Windows use `.venv/Scripts/python` (as below); on Linux/macOS use
`.venv/bin/python` instead. Check which exists before running anything. Linux setup:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -e .
.venv/bin/python -m pytest -m "not live"
.venv/bin/python examples/due_this_week.py   # prompts for creds; lists unsubmitted work due in 7 days
```

Windows commands:

```bash
# Setup
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # or requirements.txt for runtime only
.venv/Scripts/python -m playwright install firefox            # only needed for the tools/ login scripts

# Tests — fast, fully mocked, no network/credentials:
.venv/Scripts/python -m pytest -m "not live"
# A single test:
.venv/Scripts/python -m pytest tests/test_api.py::test_upcoming_homework_merges_and_sorts
# Live read-only smoke tests against the real instance (needs credentials, see below):
.venv/Scripts/python -m pytest -m live

# Run the examples:
.venv/Scripts/python examples/show_homework.py
.venv/Scripts/python examples/due_this_week.py    # prompts for creds; --days N to change window
```

There is no linter or type-checker configured; `pytest` is the only check.

## Authentication reality (important)

The MVA instance **disables personal access tokens**, so in practice this library
authenticates with **browser session cookies**, not a token. `MvaCanvas.from_env()`
prefers `CANVAS_TOKEN` if set but falls back to a cookies file
(`CANVAS_COOKIES`, default `.pw-session/cookies.json`). Read-only GETs only need the
`canvas_session` cookie; this library never writes, so no CSRF handling is required.

Note: the top of `README.md` still describes the token happy-path; `.env.example`,
`src/mva_canvas/session.py`, and the `tools/` scripts reflect the real cookie-based
flow. Trust the code and `.env.example` over the README prose here.

`from_env()` also accepts `CANVAS_USERNAME` + `CANVAS_PASSWORD` (checked after the
token, before the cookies file). `MvaCanvas.from_credentials(username, password)`
(`src/mva_canvas/login.py`) logs in through the `/login/canvas` form with plain
`requests`: it reads the form's `authenticity_token`, posts the credentials, treats a
redirect away from `/login` as success, then checks `/api/v1/users/self`. Wrong
passwords and two-factor prompts raise `CanvasAuthError`; it cannot handle SSO, 2FA
or a CAPTCHA.

To (re)create the browser cookie session when the API starts returning 401/403:

```bash
.venv/Scripts/python tools/capture_login.py    # headed Firefox; log in by hand
.venv/Scripts/python tools/export_session.py    # writes .pw-session/cookies.json
```

## Architecture

Three layers, each a file in `src/mva_canvas/`:

- **`client.py` — `CanvasClient`**: the only code that does HTTP. Handles the Bearer
  header (token path) *or* a pre-authenticated cookie `requests.Session`, RFC-5988
  `Link`-header pagination (`iter_paginated`/`get_paginated`), and maps status codes
  to the exceptions in `errors.py` (401/403 → `CanvasAuthError`, 404 →
  `CanvasNotFoundError`).
- **`models.py`**: frozen-ish dataclasses (`User`, `Course`, `Assignment`, `Module`,
  `ModuleItem`, `Homework`). Every model has a `from_api(dict)` classmethod, keeps the
  untouched payload on `.raw`, and only promotes fields the app needs. `Homework` is
  the unified "thing to do" object, built either `from_todo` or `from_assignment`.
- **`api.py` — `MvaCanvas`**: the high-level surface the app consumes; the only layer
  that encodes domain logic. `session.py` builds a cookie `requests.Session` from an
  exported Playwright cookie file; `login.py` builds one by logging in with a
  username and password.

### Two domain quirks the logic exists to handle

These are the non-obvious reasons the code is shaped the way it is — learned from the
real MVA data and mirrored in the test fixtures:

1. **Most assignments have `due_at == null`.** Only the weekly *"SUBMIT: …"* tasks
   carry real deadlines. So `get_upcoming_homework()` does **not** just list dated
   assignments — it merges the **To-Do feed** (`/users/self/todo`, what Canvas still
   wants submitted) with assignments that genuinely have a *future* due date,
   de-duplicates by `assignment_id`, and sorts soonest-first (undated last).
2. **Materials live in weekly Modules, not Pages** (the `pages` endpoint is disabled
   for these courses). An assignment appears as a module item with
   `type == 'Assignment'` and `content_id == assignment.id`.
   `get_materials_for_homework()` uses that link to find the containing module and
   returns its items; `ModuleItem.is_material` filters out `SubHeader` layout items.
3. **`has_submitted_submissions` is course-wide, not per-student.** It turns true
   once *any* student submits, so it must not be used for "has this student done
   it". `get_assignments()` fetches with `include[]=submission` and
   `models.own_submission_done()` reads the student's own `submission` instead.

## Tests

- `tests/conftest.py` holds hand-built sample payloads that mirror the **exact live
  MVA schema**, including the edge cases above (null due dates, past-due to-dos,
  assignments embedded as module items). When changing parsing or the homework/
  materials logic, update these fixtures to keep them faithful — they are the spec.
- Mocked tests use the `responses` library; no real network. Live tests
  (`tests/test_live.py`, marker `live`) are opt-in and skip themselves unless a token
  or a cookies file is present.

## `tools/` and `capture/` (dev-only, not shipped)

`tools/` are one-off Playwright scripts for the login/session workflow and for
probing the live API; they are **not** part of the importable library.
`capture/` holds real captured JSON responses and screenshots from a logged-in
session — useful as a reference for the live schema when extending models, but it is
git-ignored (it contains a real student's personal data), as are `.env`,
`.pw-profile/`, and `.pw-session/`.
