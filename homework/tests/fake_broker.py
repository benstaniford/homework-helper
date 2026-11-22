"""A stand-in claude-broker for an app's tests: Python standard library only, one file.

Copy it beside claude_broker_client.py. It speaks the same API on a unix socket, records
every request, and answers runs with events you script:

    fake = FakeBroker(tmp / "broker.sock")
    fake.reply = lambda body: [{"type": "result", "structured": {"answer": 42}}]
    fake.start()
    ...                       # the app under test talks to tmp/broker.sock
    assert fake.requests[-1]["body"]["prompt"].startswith("...")
    fake.stop()

`reply(body)` returns the events of a run (a `started` event is put first unless the list
has one; `seq` and `session_id` are filled in). Raise `FakeHTTPError(status, errors)` in it
to refuse the run instead.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit


class FakeHTTPError(Exception):
    def __init__(self, status: int, *errors: str):
        super().__init__(f"{status}: {'; '.join(errors)}")
        self.status = status
        self.errors = list(errors)


def _default_reply(body: dict) -> list[dict]:
    return [{"type": "result", "structured": None, "text": "ok"}]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return "fake"

    def log_message(self, format, *args):
        pass

    def _json(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _stream(self, run_id: str, events: list[dict], start: int = 0) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Run-Id", run_id)
        self.end_headers()
        for event in events[start:]:
            line = json.dumps(event).encode() + b"\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
        self.wfile.write(b"0\r\n\r\n")

    def _handle(self, method: str) -> None:
        fake: FakeBroker = self.server.fake          # type: ignore[attr-defined]
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        raw = self._body() if method in ("POST", "PUT") else b""
        record = {"method": method, "path": self.path, "body": None}
        if method == "POST":
            try:
                record["body"] = json.loads(raw or b"null")
            except ValueError:
                record["body"] = raw
        elif method == "PUT":
            record["body"] = raw
        with fake.lock:
            fake.requests.append(record)
        if parts[:1] != ["v1"] or len(parts) < 2:
            return self._json(404, {"errors": ["no such endpoint"]})
        what, rest = parts[1], parts[2:]
        if method == "GET" and what == "health":
            return self._json(200, {"ok": True, "version": "fake", "client": "fake", "claude": "/fake/claude",
                                    "tools_socket": False, "running": 0, "queued": 0})
        if method == "GET" and what == "usage":
            return self._json(200, {"rate_limits": fake.rate_limits})
        if what == "profiles":
            if method == "GET" and not rest:
                return self._json(200, {"profiles": [{"name": n, "sha256": s, "files": 0}
                                                     for n, s in sorted(fake.profiles.items())]})
            if method == "PUT" and len(rest) == 1:
                import hashlib
                fake.profiles[rest[0]] = hashlib.sha256(raw).hexdigest()
                return self._json(200, {"name": rest[0], "sha256": fake.profiles[rest[0]], "files": 0})
            if method == "DELETE" and len(rest) == 1 and fake.profiles.pop(rest[0], None):
                return self._json(200, {"deleted": rest[0]})
            return self._json(404, {"errors": ["no such profile"]})
        if what == "runs":
            if method == "GET" and not rest:
                return self._json(200, {"runs": [{"run_id": r, "status": "done"} for r in fake.runs]})
            if method == "POST" and not rest:
                body = record["body"] if isinstance(record["body"], dict) else {}
                run_id = body.get("run_id") or uuid.uuid4().hex
                if run_id in fake.runs:
                    return self._stream(run_id, fake.runs[run_id])
                try:
                    events = list(fake.reply(body))
                except FakeHTTPError as e:
                    return self._json(e.status, {"errors": e.errors})
                session = body.get("session") or {}
                session_id = session.get("resume") or str(uuid.uuid4())
                if not any(e.get("type") == "started" for e in events):
                    events.insert(0, {"type": "started", "run_id": run_id, "resumed": "resume" in session,
                                      "model": body.get("model"), "profile": body.get("profile") or "_default"})
                for i, event in enumerate(events):
                    event.setdefault("seq", i)
                    if event.get("type") in ("started", "result"):
                        event.setdefault("session_id", session_id)
                fake.runs[run_id] = events
                return self._stream(run_id, events)
            if method == "GET" and len(rest) == 1 and rest[0] in fake.runs:
                start = int((parse_qs(url.query).get("from") or ["0"])[0])
                return self._stream(rest[0], fake.runs[rest[0]], start)
            if method == "DELETE" and len(rest) == 1 and rest[0] in fake.runs:
                fake.cancelled.append(rest[0])
                return self._json(200, {"run_id": rest[0], "status": "done"})
            return self._json(404, {"errors": ["no such run"]})
        return self._json(404, {"errors": ["no such endpoint"]})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class FakeBroker:
    def __init__(self, socket_path: str | os.PathLike):
        self.socket_path = Path(socket_path)
        self.reply: Callable[[dict], list[dict]] = _default_reply
        self.requests: list[dict] = []
        self.runs: dict[str, list[dict]] = {}
        self.profiles: dict[str, str] = {}
        self.cancelled: list[str] = []
        self.rate_limits: dict | None = None
        self.lock = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "FakeBroker":
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = _Server(str(self.socket_path), _Handler)
        self._server.fake = self                     # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(5)
        try:
            self.socket_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "FakeBroker":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
