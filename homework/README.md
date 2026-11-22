# Homework

A small website that shows a Minerva Virtual Academy student's homework, using
the `mva_canvas` library in `../mva`. Sign in with the student's MVA (Canvas)
username and password; the login is remembered for a year (`REMEMBER_DAYS`).

The page shows **This week** (due in the next 7 days, plus undated tasks in each
course's current weekly module), with collapsible **Overdue** and **Upcoming**
sections. Every item links to the assignment on Canvas. Results are cached for 5
minutes; use **Refresh** to reload.

## Run

```bash
cp .env.example .env     # set SECRET_KEY (keep it stable)
docker compose up --build -d
# open http://<host>:5050
```

## Test

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -v
```

## How login works

Credentials are checked by logging in to MVA. On success the browser gets a
signed, HttpOnly cookie valid for `REMEMBER_DAYS`. The cookie holds the username
and the password, encrypted with a key derived from `SECRET_KEY`. The server
logs in to Canvas again by itself when the Canvas session expires. Changing
`SECRET_KEY` logs everyone out. Set `SESSION_COOKIE_SECURE=true` when serving
over HTTPS.
