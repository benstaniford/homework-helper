"""
A small MCP server on a unix socket: the tools claude-broker hands to Claude.

The broker's relay connects to ``tools.sock`` in the folder it shares with this
container and passes newline-delimited JSON-RPC both ways (MCP's stdio framing).
Each connection is one MCP session. Only the parts of MCP that tools need are
spoken: initialize, ping, tools/list and tools/call.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import stat
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"


class ToolError(Exception):
    """A tool failed in a way Claude should see (returned with isError)."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., str]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


class McpServer:
    def __init__(self, path: str, tools: list[Tool], name: str = "homework", version: str = "1"):
        self.path = path
        self.tools = {t.name: t for t in tools}
        self.name = name
        self.version = version
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> "McpServer":
        try:
            st = os.lstat(self.path)
            if stat.S_ISSOCK(st.st_mode) or stat.S_ISLNK(st.st_mode):
                os.unlink(self.path)  # left over from a previous process
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self.path)
        os.chmod(self.path, 0o600)
        sock.listen(16)
        self._sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name="mcp-accept", daemon=True)
        self._thread.start()
        logger.info("MCP tools listening on %s (%s)", self.path, ", ".join(self.tools))
        return self

    def stop(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while self._sock is not None:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), name="mcp-conn", daemon=True).start()

    # -- one MCP session ------------------------------------------------------

    def _serve(self, conn: socket.socket) -> None:
        write_lock = threading.Lock()

        def send(msg: dict) -> None:
            data = json.dumps(msg, ensure_ascii=False).encode() + b"\n"
            with write_lock:
                try:
                    conn.sendall(data)
                except OSError:
                    pass

        with conn, conn.makefile("rb") as reader:
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                    continue
                if not isinstance(msg, dict) or "method" not in msg:
                    continue  # a response or junk: nothing to answer
                if msg.get("method") == "tools/call" and "id" in msg:
                    # Tools can be slow (web fetches): run them alongside each other.
                    threading.Thread(target=lambda m=msg: send(self.handle(m) or {}), daemon=True).start()
                    continue
                reply = self.handle(msg)
                if reply is not None:
                    send(reply)

    def handle(self, msg: dict) -> dict | None:
        """The reply to one JSON-RPC message (None for notifications)."""
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if mid is None:
            return None  # notifications/initialized, cancelled, ...
        if method == "initialize":
            result: Any = {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [t.spec() for t in self.tools.values()]}
        elif method == "tools/call":
            result = self._call(params.get("name"), params.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _call(self, name: Optional[str], args: dict) -> dict:
        tool = self.tools.get(name or "")
        if tool is None:
            return _text(f"No tool called {name}", error=True)
        if not isinstance(args, dict):
            return _text("Arguments must be an object", error=True)
        try:
            return _text(tool.fn(**args))
        except ToolError as e:
            return _text(str(e), error=True)
        except TypeError as e:
            return _text(f"Bad arguments for {name}: {e}", error=True)
        except Exception:
            logger.exception("Tool %s failed", name)
            return _text(f"{name} failed unexpectedly", error=True)


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}
