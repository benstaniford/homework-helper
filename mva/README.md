# MVA Canvas API

A small Python library over the **Minerva Virtual Academy** Canvas instance
(`minervavirtual.instructure.com`). It logs in with a personal access token and
answers three questions about a student's work:

1. **Which courses** is the student taking?
2. **What homework** is outstanding or coming up?
3. **What are the course materials** for a given piece of homework?

It's a thin, typed wrapper over the standard Canvas REST API — no HTML scraping.

## Why these design choices

The live MVA data has some quirks this library handles for you:

- **Most assignments have no `due_at`.** Only the weekly *"SUBMIT: …"* tasks carry
  real deadlines. So `get_upcoming_homework()` is built from the **To-Do feed**
  (what Canvas still wants submitted) merged with assignments that genuinely have a
  future due date — de-duplicated and sorted soonest-first.
- **Materials live in weekly Modules**, not Pages (the `pages` endpoint is disabled
  for these courses). An assignment appears as an item inside its week's module, so
  `get_materials_for_homework()` finds that module and returns its items.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # or requirements.txt for runtime only
```

Create your token in Canvas:
**Account → Settings → Approved Integrations → + New Access Token**, then:

```bash
cp .env.example .env        # then paste your token into CANVAS_TOKEN
```

## Usage

```python
from mva_canvas import MvaCanvas

api = MvaCanvas.from_env()            # reads CANVAS_TOKEN / CANVAS_BASE_URL from .env
# or log in with a username and password (no SSO/2FA):
# api = MvaCanvas.from_credentials("student-username", "password")

print(api.get_self().name)

for course in api.get_courses():
    print(course.course_code, course.name)

for hw in api.get_upcoming_homework():
    due = hw.due_at.strftime("%d %b") if hw.due_at else "no deadline"
    print(f"[{due}] {hw.course_name}: {hw.title}")

# Materials for the most urgent task
top = api.get_upcoming_homework()[0]
for item in api.get_materials_for_homework(top):
    print(item.type, item.title, item.html_url)
```

Or run the example:

```bash
.venv/Scripts/python examples/show_homework.py
```

## API surface

| Method | Returns |
| --- | --- |
| `get_self()` | `User` |
| `get_courses(active_only=True)` | `list[Course]` |
| `get_assignments(course_id)` | `list[Assignment]` |
| `get_todo()` | `list[Homework]` (outstanding submissions) |
| `get_upcoming_homework(include_todo=True)` | `list[Homework]` (merged, sorted) |
| `get_modules(course_id, with_items=True)` | `list[Module]` |
| `get_current_module(course_id)` | `Module` (the current week) |
| `find_module_for_assignment(course_id, assignment_id)` | `Module` |
| `get_materials_for_homework(homework)` | `list[ModuleItem]` |

## Tests

```bash
# Fast, fully mocked — no network, no token needed:
.venv/Scripts/python -m pytest -m "not live"

# Live smoke tests against the real instance (read-only); needs CANVAS_TOKEN:
.venv/Scripts/python -m pytest -m live
```

## Project layout

```
src/mva_canvas/     the library (client, models, high-level api)
tests/              mocked unit tests + opt-in live smoke tests
examples/           runnable example
tools/              dev-only: login capture & schema probes (not part of the library)
```

## Security notes

- `.env`, `.pw-profile/` (saved browser session), and `capture/` (personal data) are
  **git-ignored**. Treat the access token like a password; revoke it any time in
  Canvas under *Account → Settings → Approved Integrations*.
```
