# Homework

A small website that shows a Minerva Virtual Academy student's homework, using
the `mva_canvas` library in `../mva`. Sign in with the student's MVA (Canvas)
username and password; the login is remembered for a year (`REMEMBER_DAYS`).

The page shows **This week** (due in the next 7 days, plus undated tasks in each
course's current weekly module), with collapsible **Overdue** and **Upcoming**
sections. Every item links to the assignment on Canvas. Results are cached for 5
minutes; use **Refresh** to reload.

## Homework helper

Every item that isn't done has an **Ask question** button. It opens a chat panel
where Claude helps with that item in one or two sentences at a time. It explains,
gives hints and points to the student's other course material (Focus pages,
videos, worksheets) or good sites such as BBC Bitesize, but it won't do the
homework for them. The rules are in `tutor_profile/SYSTEM.md`.

Claude runs through **claude-broker** on the host (`~/Code/claude-broker`).
- The app sends questions to `broker.sock` and streams the answers to the page.
- Claude reads the student's Canvas items and the web through tools that this app
  serves on `tools.sock` in the same folder: `list_items`, `read_item`,
  `web_search` and `fetch_url` (`tutor.py`, `mcp_server.py`, `web_tools.py`).
- The Canvas tools take the chat's random id, so they only reach the data of the
  student who opened that chat.
- `fetch_url` only fetches public addresses.

One-off set-up on the host, before `docker compose up`:

```bash
mkdir -p ~/Code/homework-helper/homework/broker-data
claude-broker client add homework --root ~/Code/homework-helper/homework/broker-data \
    --model claude-sonnet-5-5 --max-run-seconds 180
```

The container runs as uid 1000 and mounts `/run/user/1000/claude-broker/homework`.
Without that folder the page has no Ask buttons. The app runs one gunicorn worker,
because the chats and the tool socket live in that process.

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
