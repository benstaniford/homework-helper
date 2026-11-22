"""A client for claude-broker: Python standard library only, one file.

Copy this file into your app. Inside a container, mount the broker's folder for your client
(e.g. /run/user/1000/claude-broker/<client> → /run/claude-broker) and:

    from claude_broker_client import BrokerClient, BrokerError

    broker = BrokerClient("/run/claude-broker/broker.sock")
    broker.put_profile("helper", "/app/profile")          # SYSTEM.md, CLAUDE.md, .claude/skills/...
    for event in broker.run(run_id="job-42", profile="helper", prompt="Summarise ...",
                            schema={...}, stream_fields=["summary"]):
        if event["type"] == "field":
            ...                                             # the summary so far
    result = broker.result(run_id="job-42", ...)           # or: just the final result

Events: queued, started, field, field_closed, fields_reset, tool, rate_limits, result,
error (each with `seq`, its place in the run). `ping`s are dropped here.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import tarfile
from pathlib import Path
from typing import Iterator
from urllib.parse import quote


class BrokerError(Exception):
    """An HTTP error (status, errors) or a run's `error` event (kind, detail)."""

    def __init__(self, message: str, status: int | None = None, errors: list[str] | None = None,
                 kind: str | None = None, event: dict | None = None):
        super().__init__(message)
        self.status = status
        self.errors = errors or []
        self.kind = kind
        self.event = event


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None):
        super().__init__("claude-broker", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def pack_profile(folder: str | os.PathLike) -> bytes:
    """A profile folder as a tar.gz: sorted, no owners or times, so it packs the same each time."""
    root = Path(folder)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            info = tarfile.TarInfo(rel)
            data = path.read_bytes()
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class BrokerClient:
    def __init__(self, socket_path: str | os.PathLike = "/run/claude-broker/broker.sock",
                 timeout: float | None = 60):
        """`timeout`: seconds to wait on any one read. A run sends a `ping` every 15 s, so
        anything above that is safe for long runs."""
        self.socket_path = str(socket_path)
        self.timeout = timeout

    # --- plain requests ---------------------------------------------------------------

    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type: str = "application/json"):
        conn = _UnixConnection(self.socket_path, self.timeout)
        headers = {"Content-Type": content_type} if body is not None else {}
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
        except BaseException:
            conn.close()
            raise
        return conn, resp

    def _json(self, method: str, path: str, body: bytes | None = None,
              content_type: str = "application/json") -> dict:
        conn, resp = self._request(method, path, body, content_type)
        try:
            data = resp.read()
        finally:
            conn.close()
        try:
            parsed = json.loads(data) if data else {}
        except ValueError:
            parsed = {}
        if resp.status >= 400:
            errors = parsed.get("errors") if isinstance(parsed, dict) else None
            raise BrokerError(f"HTTP {resp.status}: {'; '.join(errors or [data.decode(errors='replace')])}",
                              status=resp.status, errors=errors)
        return parsed

    def health(self) -> dict:
        return self._json("GET", "/v1/health")

    def usage(self) -> dict | None:
        return self._json("GET", "/v1/usage").get("rate_limits")

    def runs(self) -> list[dict]:
        return self._json("GET", "/v1/runs")["runs"]

    def cancel(self, run_id: str) -> dict:
        return self._json("DELETE", f"/v1/runs/{quote(run_id, safe='')}")

    def profiles(self) -> list[dict]:
        return self._json("GET", "/v1/profiles")["profiles"]

    def put_profile(self, name: str, folder_or_bytes) -> dict:
        """Upload a profile (a folder, or tar/tar.gz bytes). Unchanged content costs nothing."""
        data = folder_or_bytes if isinstance(folder_or_bytes, bytes) else pack_profile(folder_or_bytes)
        return self._json("PUT", f"/v1/profiles/{quote(name, safe='')}", data, "application/gzip")

    def delete_profile(self, name: str) -> dict:
        return self._json("DELETE", f"/v1/profiles/{quote(name, safe='')}")

    # --- runs ----------------------------------------------------------------------------

    def _events(self, method: str, path: str, body: bytes | None = None, pings: bool = False) -> Iterator[dict]:
        conn, resp = self._request(method, path, body)
        try:
            if resp.status >= 400:
                data = resp.read()
                try:
                    errors = json.loads(data).get("errors")
                except (ValueError, AttributeError):
                    errors = None
                raise BrokerError(f"HTTP {resp.status}: {'; '.join(errors or [data.decode(errors='replace')])}",
                                  status=resp.status, errors=errors)
            while True:
                line = resp.readline()
                if not line:
                    return
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "ping" and not pings:
                    continue
                yield event
        finally:
            conn.close()

    def run(self, prompt: str, *, run_id: str | None = None, profile: str | None = None,
            session: dict | None = None, schema: dict | None = None, model: str | None = None,
            effort: str | None = None, add_dirs: list | None = None, builtin_tools: list[str] | None = None,
            mcp_tools: list[str] | None = None, stream_fields: list[str] | None = None,
            timeout: float | None = None, pings: bool = False) -> Iterator[dict]:
        """Start a run (or rejoin the one with this `run_id`) and yield its events.
        `session`: {"new": True} (the default) or {"resume": "<session id>"}."""
        body: dict = {"prompt": prompt}
        for key, value in (("run_id", run_id), ("profile", profile), ("session", session), ("schema", schema),
                           ("model", model), ("effort", effort), ("add_dirs", add_dirs),
                           ("builtin_tools", builtin_tools), ("mcp_tools", mcp_tools),
                           ("stream_fields", stream_fields), ("timeout", timeout)):
            if value is not None:
                body[key] = value
        return self._events("POST", "/v1/runs", json.dumps(body).encode(), pings)

    def follow(self, run_id: str, start: int = 0, pings: bool = False) -> Iterator[dict]:
        """A run's events from event `start` (its `seq`) on, then the live ones."""
        return self._events("GET", f"/v1/runs/{quote(run_id, safe='')}?from={int(start)}", pings=pings)

    def result(self, prompt: str, **kwargs) -> dict:
        """Run and return the `result` event; raises BrokerError on an `error` event."""
        return final(self.run(prompt, **kwargs))


def final(events) -> dict:
    """The `result` event of an event stream; BrokerError for an `error` event."""
    for event in events:
        if event.get("type") == "result":
            return event
        if event.get("type") == "error":
            raise BrokerError(f"{event.get('kind')}: {event.get('detail')}", kind=event.get("kind"), event=event)
    raise BrokerError("the broker ended the stream without a result", kind="disconnected")
