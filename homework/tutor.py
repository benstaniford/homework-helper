"""
The homework tutor: a chat about one homework item, answered by Claude through
claude-broker (see ~/Code/claude-broker).

Flask streams each answer to the browser as NDJSON. Claude reads course material
and the web through MCP tools that this process serves on the broker's tools.sock.
Those tools take the chat id, so they can only reach the Canvas data of the student
who owns the chat.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

from claude_broker_client import BrokerClient, BrokerError
from homework_data import Item, Report, SessionCache, item_text
from mcp_server import McpServer, Tool, ToolError
import web_tools
from mva_canvas.errors import CanvasAuthError, CanvasError

logger = logging.getLogger(__name__)

PROFILE_NAME = "homework-tutor"
PROFILE_DIR = Path(__file__).resolve().parent / "tutor_profile"
MODEL = os.environ.get("TUTOR_MODEL", "claude-sonnet-5-5")
EFFORT = os.environ.get("TUTOR_EFFORT", "low")
RUN_TIMEOUT = int(os.environ.get("TUTOR_RUN_SECONDS", "150"))
QUESTIONS_PER_HOUR = int(os.environ.get("TUTOR_QUESTIONS_PER_HOUR", "30"))
CHAT_TTL_SECONDS = 2 * 3600
MAX_TURNS = 20
MAX_QUESTION_CHARS = 1000
CONTEXT_RESET_TOKENS = 60_000
MAX_LISTED_ITEMS = 80

TOOL_NAMES = ["list_items", "read_item", "web_search", "fetch_url"]

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "Your reply to the student: one or two short sentences, at most about 40 words.",
        },
        "resources": {
            "type": "array",
            "maxItems": 3,
            "description": "Links worth opening next: items from their list or good external pages. Often empty.",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
            },
        },
    },
    "required": ["answer", "resources"],
}


class TutorError(Exception):
    """A request the tutor refuses; the message is safe to show the student."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Chat:
    id: str
    username: str
    password: str  # in memory only, so the tools can log in to Canvas again if needed
    item_key: str
    session_id: Optional[str] = None
    turns: int = 0
    created: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    busy: threading.Lock = field(default_factory=threading.Lock, repr=False)


class ChatStore:
    def __init__(self, ttl: float = CHAT_TTL_SECONDS):
        self.ttl = ttl
        self._chats: dict[str, Chat] = {}
        self._lock = threading.Lock()

    def create(self, username: str, password: str, item_key: str) -> Chat:
        chat = Chat(id=secrets.token_urlsafe(18), username=username, password=password, item_key=item_key)
        with self._lock:
            self._expire()
            self._chats[chat.id] = chat
        return chat

    def get(self, chat_id: str, username: Optional[str] = None) -> Optional[Chat]:
        """The chat, if it exists, hasn't expired and (when given) belongs to username."""
        with self._lock:
            self._expire()
            chat = self._chats.get(chat_id or "")
        if chat is None or (username is not None and chat.username != username):
            return None
        chat.last_used = time.monotonic()
        return chat

    def forget_user(self, username: str) -> None:
        with self._lock:
            for cid in [c.id for c in self._chats.values() if c.username == username]:
                del self._chats[cid]

    def _expire(self) -> None:
        now = time.monotonic()
        for cid in [c.id for c in self._chats.values() if now - c.last_used > self.ttl]:
            del self._chats[cid]


class RateLimiter:
    def __init__(self, per_hour: int = QUESTIONS_PER_HOUR):
        self.per_hour = per_hour
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.per_hour:
                return False
            q.append(now)
            return True


def _first_name(name: str) -> str:
    return (name or "").split()[0] if name else ""


def _describe(item: Item) -> str:
    bits = [item.course_name]
    if item.material:
        bits.append("material")
    elif item.stage and item.stage != "Other":
        bits.append(item.stage)
    if item.due:
        bits.append(f"due {item.due}")
    if item.done:
        bits.append("done")
    return f"- [{item.key}] {item.title} ({', '.join(bits)})"


def _safe_resources(raw) -> list[dict]:
    out = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        url, title = str(r.get("url") or ""), str(r.get("title") or "").strip()
        if urlparse(url).scheme in ("http", "https"):
            out.append({"title": title[:120] or url, "url": url})
    return out[:3]


class Tutor:
    """Chats, the broker connection and the MCP tools, for one app process."""

    def __init__(self, cache: Callable[[], SessionCache], broker_dir: Optional[str] = None,
                 web=web_tools):
        self._cache = cache
        self.broker_dir = Path(broker_dir or os.environ.get("BROKER_DIR", "/run/claude-broker"))
        self.broker = BrokerClient(self.broker_dir / "broker.sock", timeout=60)
        self.chats = ChatStore()
        self.limiter = RateLimiter()
        self.web = web
        self._profile_ready = False
        self._profile_lock = threading.Lock()
        self._mcp: Optional[McpServer] = None
        self._start_lock = threading.Lock()

    # -- set-up -------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether the broker's folder is mounted (the socket is there)."""
        return (self.broker_dir / "broker.sock").exists()

    def start(self) -> None:
        """Serve the MCP tools on tools.sock (once per process; call after forking)."""
        with self._start_lock:
            if self._mcp is not None or not self.broker_dir.is_dir():
                return
            try:
                self._mcp = McpServer(str(self.broker_dir / "tools.sock"), self.tools()).start()
            except OSError as e:
                logger.warning("Tutor tools not started: %s", e)

    def stop(self) -> None:
        if self._mcp:
            self._mcp.stop()
            self._mcp = None

    def _ensure_profile(self) -> None:
        with self._profile_lock:
            if not self._profile_ready:
                self.broker.put_profile(PROFILE_NAME, PROFILE_DIR)
                self._profile_ready = True

    # -- chats ----------------------------------------------------------------------

    def _report(self, chat: Chat) -> Report:
        return self._cache().report(chat.username, chat.password)

    def open_chat(self, username: str, password: str, item_key: str) -> Chat:
        report = self._cache().report(username, password)
        item = report.items_by_key.get(item_key)
        if item is None:
            raise TutorError("That item isn't in your homework list any more. Try refreshing.", 404)
        if item.done:
            raise TutorError("That one's already done!")
        return self.chats.create(username, password, item_key)

    def _prompt(self, chat: Chat, question: str, report: Report) -> str:
        item = report.items_by_key.get(chat.item_key)
        if item is None:
            raise TutorError("That item isn't in your homework list any more. Try refreshing.", 404)
        content = self._cache().call(chat.username, chat.password, lambda api: item_text(api, item))
        others = [i for i in report.recent_items() if i.key != item.key]
        # The same course's items first: they're the likeliest to help.
        others.sort(key=lambda i: i.course_name != item.course_name)
        listing = "\n".join(_describe(i) for i in others[:MAX_LISTED_ITEMS])
        more = f"\n(and {len(others) - MAX_LISTED_ITEMS} more: use list_items)" if len(others) > MAX_LISTED_ITEMS else ""
        return (
            f"Chat id for your tools: {chat.id}\n"
            f"Student: {_first_name(report.user_name) or 'the student'}\n\n"
            f"<homework_item key=\"{item.key}\">\n{content}\n</homework_item>\n\n"
            f"<other_recent_items>\n{listing or '(none)'}{more}\n</other_recent_items>\n\n"
            f"The student's question about this item:\n<question>\n{question}\n</question>"
        )

    def ask(self, chat: Chat, question: str) -> Iterator[dict]:
        """Answer one question. Yields app events for the browser:
        {"status"}, {"delta"}, {"reset"}, then {"done", "resources"} or {"error"}."""
        question = (question or "").strip()
        if not question:
            yield {"error": "Type a question first."}
            return
        if len(question) > MAX_QUESTION_CHARS:
            yield {"error": f"That's a long one. Keep it under {MAX_QUESTION_CHARS} characters."}
            return
        if chat.turns >= MAX_TURNS:
            yield {"error": "This chat is full. Close it and press Ask again to start a fresh one."}
            return
        if not self.limiter.allow(chat.username):
            yield {"error": "You've asked a lot of questions this hour. Have a go yourself and try again later."}
            return
        if not chat.busy.acquire(blocking=False):
            yield {"error": "Still answering your last question."}
            return
        try:
            yield from self._ask(chat, question)
        finally:
            chat.busy.release()

    def _ask(self, chat: Chat, question: str) -> Iterator[dict]:
        try:
            if chat.session_id:
                prompt = f"<question>\n{question}\n</question>"
            else:
                yield {"status": "Reading the task…"}
                prompt = self._prompt(chat, question, self._report(chat))
            self._ensure_profile()
        except TutorError as e:
            yield {"error": str(e)}
            return
        except CanvasAuthError:
            yield {"error": "Your MVA login has stopped working. Please sign in again."}
            return
        except (CanvasError, OSError, BrokerError) as e:
            logger.warning("Tutor couldn't start: %s", e)
            yield {"error": "The helper isn't available right now. Try again in a minute."}
            return

        chat.turns += 1
        resumed = chat.session_id is not None
        answer = ""
        try:
            events = self.broker.run(
                prompt,
                run_id=f"hw-{chat.id[:16]}-{chat.turns}",
                profile=PROFILE_NAME,
                session={"resume": chat.session_id} if resumed else {"new": True},
                schema=ANSWER_SCHEMA,
                stream_fields=["answer"],
                model=MODEL,
                effort=EFFORT,
                builtin_tools=[],
                mcp_tools=TOOL_NAMES,
                timeout=RUN_TIMEOUT,
            )
            yield {"status": "Thinking…"}
            for ev in events:
                kind = ev.get("type")
                if kind == "queued":
                    yield {"status": "Waiting for a turn…"}
                elif kind == "started":
                    if ev.get("session_id"):
                        chat.session_id = ev["session_id"]
                elif kind == "tool":
                    status = self._tool_status(chat, ev)
                    if status:
                        yield {"status": status}
                elif kind == "field" and ev.get("name") == "answer":
                    if "text" in ev:
                        answer = ev["text"]
                        yield {"reset": True}
                        if answer:
                            yield {"delta": answer}
                    elif ev.get("delta"):
                        answer += ev["delta"]
                        yield {"delta": ev["delta"]}
                elif kind == "fields_reset":
                    answer = ""
                    yield {"reset": True}
                elif kind == "result":
                    chat.session_id = ev.get("session_id") or chat.session_id
                    if (ev.get("context_tokens") or 0) > CONTEXT_RESET_TOKENS:
                        chat.session_id = None  # next question starts afresh, with the context re-read
                    structured = ev.get("structured") or {}
                    final = (structured.get("answer") or ev.get("text") or "").strip()
                    if final and final != answer:
                        yield {"reset": True}
                        yield {"delta": final}
                    yield {"done": True, "resources": _safe_resources(structured.get("resources"))}
                    return
                elif kind == "error":
                    yield {"error": self._error_message(chat, ev.get("kind"), ev.get("detail"))}
                    return
            yield {"error": "The helper stopped before answering. Try asking again."}
        except BrokerError as e:
            chat.turns -= 1
            if e.status == 429:
                yield {"error": "The helper is busy right now. Try again in a minute."}
            else:
                logger.warning("Broker refused a run: %s", e)
                yield {"error": "The helper isn't available right now. Try again in a minute."}
        except OSError as e:
            logger.warning("Lost the broker: %s", e)
            yield {"error": "Lost touch with the helper. Try asking again."}

    def _error_message(self, chat: Chat, kind: Optional[str], detail) -> str:
        logger.warning("Tutor run failed: %s %s", kind, detail)
        if kind == "session_missing":
            chat.session_id = None
            return "Sorry, I lost track of our chat. Please ask again."
        if kind == "timeout":
            return "That took too long to answer. Try a simpler question."
        if kind == "cancelled":
            return "The answer was cancelled."
        return "Something went wrong answering that. Try asking again."

    def _tool_status(self, chat: Chat, ev: dict) -> Optional[str]:
        name = (ev.get("name") or "").rsplit("__", 1)[-1]
        args = ev.get("input") or {}
        if name == "read_item":
            try:
                item = self._report(chat).items_by_key.get(args.get("key"))
            except Exception:
                item = None
            return f"Reading “{item.title}”…" if item else "Reading your course material…"
        if name == "list_items":
            return "Looking through your homework list…"
        if name == "web_search":
            q = str(args.get("query") or "")[:60]
            return f"Searching the web for “{q}”…" if q else "Searching the web…"
        if name == "fetch_url":
            host = urlparse(str(args.get("url") or "")).hostname or "a web page"
            return f"Reading {host.removeprefix('www.')}…"
        return None

    # -- MCP tools ---------------------------------------------------------------------

    def _tool_chat(self, chat: str) -> Chat:
        found = self.chats.get(chat)
        if found is None:
            raise ToolError("Unknown or expired chat id: use the one given at the top of the conversation")
        return found

    def tool_list_items(self, chat: str) -> str:
        c = self._tool_chat(chat)
        report = self._report(c)
        lines = [_describe(i) for i in report.recent_items()]
        return "\n".join(lines) or "(No items)"

    def tool_read_item(self, chat: str, key: str) -> str:
        c = self._tool_chat(chat)
        item = self._report(c).items_by_key.get(key)
        if item is None:
            raise ToolError(f"No item with key {key}: use list_items to see the keys")
        try:
            return self._cache().call(c.username, c.password, lambda api: item_text(api, item))
        except (CanvasError, OSError) as e:
            raise ToolError(f"Couldn't load it from Canvas: {e}")

    def tool_web_search(self, query: str) -> str:
        try:
            results = self.web.web_search(query)
        except (self.web.WebError, OSError) as e:
            raise ToolError(f"Search failed: {e}")
        if not results:
            return "No results."
        return "\n\n".join("\n".join(x for x in (r['title'], r['url'], r['snippet']) if x) for r in results)

    def tool_fetch_url(self, url: str) -> str:
        try:
            return self.web.fetch_url(url)
        except (self.web.WebError, OSError) as e:
            raise ToolError(f"Couldn't fetch {url}: {e}")

    def tools(self) -> list[Tool]:
        chat_prop = {"type": "string", "description": "The chat id given at the top of the conversation"}
        return [
            Tool("list_items",
                 "List the student's recent homework items and course materials (this week, overdue, "
                 "upcoming, and the reference material in each course's current module), with their keys.",
                 {"type": "object", "properties": {"chat": chat_prop}, "required": ["chat"]},
                 self.tool_list_items),
            Tool("read_item",
                 "Read one homework item or course material from Canvas in full: the task brief, page, "
                 "discussion prompt or PDF text, including links the teacher added.",
                 {"type": "object",
                  "properties": {"chat": chat_prop,
                                 "key": {"type": "string", "description": "The item's key, e.g. 3517:page:osmosis-notes"}},
                  "required": ["chat", "key"]},
                 self.tool_read_item),
            Tool("web_search",
                 "Search the web. Returns up to 6 results with title, link and snippet.",
                 {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                 self.tool_web_search),
            Tool("fetch_url",
                 "Read a public web page, PDF or shared Google Doc/Slides link as text.",
                 {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                 self.tool_fetch_url),
        ]
