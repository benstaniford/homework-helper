"""
Homework data for the web app: logs in to MVA Canvas, sorts the student's
unsubmitted work into overdue / this week / upcoming, and caches both the
Canvas session and the result per user.

The sorting mirrors mva/examples/due_this_week.py, plus an "upcoming" bucket
for dated work beyond the look-ahead window.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional, TypeVar
from zoneinfo import ZoneInfo

from mva_canvas import MvaCanvas
from mva_canvas.errors import CanvasAuthError, CanvasError
from mva_canvas.models import Homework, now_utc
from mva_canvas.text import html_to_text, pdf_to_text

logger = logging.getLogger(__name__)

T = TypeVar("T")

LOCAL_TZ = ZoneInfo(os.environ.get("TZ") or "Europe/London")
REPORT_TTL_SECONDS = int(os.environ.get("REPORT_TTL_SECONDS", "300"))
MAX_WORKERS = 6
# Overdue work older than this is left out: by then it's no longer worth chasing.
OVERDUE_DAYS = int(os.environ.get("OVERDUE_DAYS", "14"))


@dataclass
class Item:
    course_name: str
    title: str
    html_url: str
    due: str            # formatted local due date, or "" if none
    module_name: str = ""
    stage: str = "Other"  # Prepare / Share / Build / ... see STAGE_ORDER
    due_at: Optional[datetime] = None
    done: bool = False    # submitted, or ticked off in the Canvas module
    key: str = ""         # stable id within a user's report, e.g. "12:assignment:345"
    source: dict = field(default_factory=dict, repr=False)  # how to fetch it: see item_text
    material: bool = False  # reference material in the module, not a task


@dataclass
class StageGroup:
    name: str
    items: list[Item]

    @property
    def todo(self) -> int:
        return sum(not i.done for i in self.items)


@dataclass
class CourseGroup:
    course_name: str
    module_name: str
    stages: list[StageGroup]

    @property
    def count(self) -> int:
        """Items still to do."""
        return sum(s.todo for s in self.stages)


@dataclass
class Report:
    user_name: str
    overdue: list[Item] = field(default_factory=list)
    this_week_due: list[Item] = field(default_factory=list)
    this_week_tasks: list[Item] = field(default_factory=list)
    upcoming: list[Item] = field(default_factory=list)
    this_week: list[CourseGroup] = field(default_factory=list)  # due soon + tasks, by course and stage
    skipped: list[str] = field(default_factory=list)
    generated_at: str = ""
    days: int = 7
    overdue_days: int = OVERDUE_DAYS
    items_by_key: dict[str, Item] = field(default_factory=dict, repr=False)  # every item + material

    def recent_items(self) -> list[Item]:
        """Everything in the report, tasks first, for the tutor's item list."""
        return sorted(self.items_by_key.values(), key=lambda i: (i.material, i.course_name, i.title))


def fmt_due(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(LOCAL_TZ).strftime("%a %d %b %H:%M")


# The order of an MVA week: FOCUS (prepare, share), LIVE LESSON (build),
# then submit, develop, recap and extend.
STAGE_ORDER = ["Start", "Focus", "Prepare", "Share", "Live lesson", "Build",
               "Submit", "Develop", "Recap", "Extend", "Other"]
_TITLE_STAGE = re.compile(r"^\W*(start here|check|focus|prepare|share|build|submit|develop|recap|extend|extension)\b", re.I)
_HEADER_STAGES = [("live lesson", "Live lesson"), ("focus", "Focus"), ("prepare", "Prepare"),
                  ("share", "Share"), ("build", "Build"), ("submit", "Submit"),
                  ("develop", "Develop"), ("recap", "Recap"), ("exten", "Extend")]


def stage_for(title: str, header: Optional[str] = "") -> str:
    """The stage of a task: from its title prefix ("PREPARE: ...", "SUBMIT 2: ..."),
    else from the module section heading it sits under ("LIVE LESSON 1 💻"). A header
    of None means the item comes before the module's first heading."""
    m = _TITLE_STAGE.match(title)
    if m:
        word = m.group(1).lower()
        if word in ("start here", "check"):
            return "Start"
        return "Extend" if word == "extension" else word.capitalize()
    if header is None:
        return "Start"  # above the module's first heading: key sheet, "Before this module I can"
    h = header.lower()
    for key, name in _HEADER_STAGES:
        if key in h:
            return name
    return "Other"


def _section_headers(module) -> dict[tuple[str, int], Optional[str]]:
    """(item type, id) -> the SubHeader above it (None before the first one);
    assignments are keyed by assignment id."""
    headers: dict[tuple[str, int], Optional[str]] = {}
    current: Optional[str] = None
    for it in module.items if module else []:
        if it.type == "SubHeader":
            current = it.title
            continue
        headers[("item", it.id)] = current
        if it.type == "Assignment" and it.content_id is not None:
            headers[("assignment", it.content_id)] = current
    return headers


def _assignment_source(course_id, assignment_id) -> tuple[str, dict]:
    return (f"{course_id}:assignment:{assignment_id}",
            {"type": "Assignment", "course_id": course_id, "assignment_id": assignment_id})


def _module_source(course_id, it) -> tuple[str, dict]:
    """Key and source of a module item (page, file, discussion, link...)."""
    ref = it.content_id if it.content_id is not None else (it.page_url or it.id)
    return (f"{course_id}:{it.type.lower()}:{ref}",
            {"type": it.type, "course_id": course_id, "url": it.url,
             "external_url": (it.raw or {}).get("external_url", "")})


def _item(hw: Homework, module_name: str = "", header: Optional[str] = "", done: bool = False) -> Item:
    key, source = _assignment_source(hw.course_id, hw.assignment_id)
    return Item(
        key=key,
        source=source,
        course_name=hw.course_name,
        title=hw.title,
        html_url=hw.html_url,
        due=fmt_due(hw.due_at),
        module_name=module_name,
        stage=stage_for(hw.title, header),
        due_at=hw.due_at,
        done=done,
    )


def group_this_week(items: list[Item], module_names: dict[str, str]) -> list[CourseGroup]:
    """Group items by course, then by stage in STAGE_ORDER. Within a stage: outstanding
    before done, dated before undated (soonest first), then module order."""
    order = {id(i): n for n, i in enumerate(items)}

    def key(i: Item):
        return (i.done, i.due_at is None, i.due_at or datetime.max.replace(tzinfo=LOCAL_TZ), order[id(i)])

    groups = []
    for course in sorted({i.course_name for i in items}):
        mine = [i for i in items if i.course_name == course]
        stages = [StageGroup(name, sorted((i for i in mine if i.stage == name), key=key))
                  for name in STAGE_ORDER]
        groups.append(CourseGroup(course, module_names.get(course, ""), [s for s in stages if s.items]))
    return groups


def build_report(api: MvaCanvas, days: int = 7) -> Report:
    """Fetch every course's assignments and current module and sort them into buckets."""
    me = api.get_self()
    now = now_utc()
    horizon = now + timedelta(days=days)
    overdue_cutoff = now - timedelta(days=OVERDUE_DAYS)
    courses = api.get_courses()

    def fetch(course):
        try:
            return course, api.get_assignments(course.id), api.get_current_module(course.id), None
        except CanvasAuthError:
            raise
        except CanvasError as e:
            return course, None, None, e

    overdue: list[Homework] = []
    upcoming: list[Homework] = []
    week: list[Item] = []   # this week's work, done and outstanding
    module_names: dict[str, str] = {}
    skipped: list[str] = []
    materials: list[Item] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(fetch, courses))

    for course, assignments, module, err in results:
        if err is not None:
            logger.warning("Skipped course %s: %s", course.name, err)
            skipped.append(course.name)
            continue
        by_id = {a.id: a for a in assignments or [] if a.is_published}
        module_name = module.name if module else ""
        if module_name:
            module_names[course.name] = module_name
        headers = _section_headers(module)
        in_module: set[int] = set()

        def in_window(a) -> bool:
            return a.due_at is None or now <= a.due_at <= horizon

        # Everything the current module asks for, in module order, ticked or not.
        for it in module.items if module else []:
            header = headers.get(("item", it.id))
            if it.type == "Assignment":
                a = by_id.get(it.content_id) if it.content_id is not None else None
                if a is None:
                    continue
                in_module.add(a.id)
                if not in_window(a):
                    continue  # overdue or later: listed in those sections instead
                hw = Homework.from_assignment(a, course_name=course.name)
                week.append(_item(hw, module_name, header,
                                  done=a.has_submitted or it.is_done))
            elif it.type != "SubHeader":
                key, source = _module_source(course.id, it)
                entry = Item(course_name=course.name, title=it.title,
                             html_url=it.html_url or source["external_url"],
                             due="", module_name=module_name, stage=stage_for(it.title, header),
                             done=it.is_done, key=key, source=source)
                if it.has_requirement:
                    # Pages, discussions etc. ticked off by viewing / "Mark as done".
                    week.append(entry)
                else:
                    # Reference material (videos, worksheets, links): not a task,
                    # but the tutor can point students at it.
                    entry.material = True
                    materials.append(entry)

        for a in by_id.values():
            if a.has_submitted or a.due_at is None:
                continue
            hw = Homework.from_assignment(a, course_name=course.name)
            if a.due_at < now:
                if a.due_at >= overdue_cutoff:
                    overdue.append(hw)
            elif a.due_at > horizon:
                upcoming.append(hw)
            elif a.id not in in_module:
                # due this week but not in the current module
                week.append(_item(hw, "", headers.get(("assignment", a.id), "")))

    def by_due(h: Homework) -> datetime:
        return h.due_at or now

    overdue.sort(key=by_due)
    upcoming.sort(key=by_due)
    todo = [i for i in week if not i.done]
    overdue_items = [_item(h) for h in overdue]
    upcoming_items = [_item(h) for h in upcoming]
    items_by_key: dict[str, Item] = {}
    for i in materials + upcoming_items + overdue_items + week:  # tasks win over materials
        items_by_key[i.key] = i

    return Report(
        user_name=me.name,
        items_by_key=items_by_key,
        overdue=overdue_items,
        this_week_due=sorted((i for i in todo if i.due_at), key=lambda i: i.due_at or now),
        this_week_tasks=sorted((i for i in todo if not i.due_at), key=lambda i: (i.course_name, i.title)),
        this_week=group_this_week(week, module_names),
        upcoming=upcoming_items,
        skipped=sorted(skipped),
        generated_at=datetime.now(LOCAL_TZ).strftime("%a %d %b %H:%M"),
        days=days,
    )


ITEM_TEXT_LIMIT = 12_000


def item_text(api: MvaCanvas, item: Item, limit: int = ITEM_TEXT_LIMIT) -> str:
    """The readable content of an item: the assignment brief, page body, discussion
    prompt or PDF text, with links kept. Fetched live from Canvas; truncated to limit."""
    src = item.source or {}
    kind = src.get("type", "")
    header = f"{item.title} ({item.course_name}{' · ' + item.module_name if item.module_name else ''})\n"
    if item.due:
        header += f"Due: {item.due}\n"
    if item.html_url:
        header += f"Link: {item.html_url}\n"
    body = ""
    try:
        if kind == "Assignment" and src.get("assignment_id") is not None:
            body = html_to_text(api.get_assignment(src["course_id"], src["assignment_id"]).description_html)
        elif kind == "Page" and src.get("url"):
            body = html_to_text(api.client.get(src["url"]).get("body"))
        elif kind == "Discussion" and src.get("url"):
            body = html_to_text(api.client.get(src["url"]).get("message"))
        elif kind == "File" and src.get("url"):
            meta = api.client.get(src["url"])
            name = meta.get("display_name") or item.title
            ctype = meta.get("content-type", "")
            if "pdf" in ctype or name.lower().endswith(".pdf"):
                body = pdf_to_text(api.download(meta["url"])) or f"(PDF {name}: no readable text)"
            else:
                body = f"(File {name}, type {ctype or 'unknown'}: contents can't be read here)"
        elif kind == "ExternalUrl":
            body = f"(External link: {src.get('external_url') or item.html_url})"
        else:
            body = f"(A Canvas {kind or 'item'}: open the link to see it)"
    except CanvasAuthError:
        raise
    except (CanvasError, OSError, KeyError, ValueError) as e:
        logger.info("Couldn't read item %s: %s", item.key, e)
        body = "(Couldn't load this item's content from Canvas)"
    text = header + "\n" + (body or "(No text content)")
    return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


class SessionCache:
    """Per-process cache of logged-in MvaCanvas clients and recent reports, keyed by username."""

    def __init__(self, login: Callable[[str, str], MvaCanvas] = MvaCanvas.from_credentials,
                 ttl: int = REPORT_TTL_SECONDS):
        self._login = login
        self._ttl = ttl
        self._apis: dict[str, MvaCanvas] = {}
        self._reports: dict[str, tuple[float, Report]] = {}
        self._lock = threading.Lock()

    def login(self, username: str, password: str) -> MvaCanvas:
        """Log in afresh (raises CanvasAuthError on bad credentials) and cache the client."""
        api = self._login(username, password)
        with self._lock:
            self._apis[username] = api
            self._reports.pop(username, None)
        return api

    def forget(self, username: str) -> None:
        with self._lock:
            self._apis.pop(username, None)
            self._reports.pop(username, None)

    def call(self, username: str, password: str, fn: Callable[[MvaCanvas], T]) -> T:
        """Run fn with a logged-in client, logging in again once if the Canvas session expired."""
        with self._lock:
            api = self._apis.get(username)
        if api is None:
            api = self.login(username, password)
            return fn(api)
        try:
            return fn(api)
        except CanvasAuthError:
            logger.info("Canvas session expired for a user; logging in again")
            api = self.login(username, password)
            return fn(api)

    def report(self, username: str, password: str, refresh: bool = False) -> Report:
        if not refresh:
            with self._lock:
                cached = self._reports.get(username)
            if cached and time.monotonic() - cached[0] < self._ttl:
                return cached[1]
        rep = self.call(username, password, build_report)
        with self._lock:
            self._reports[username] = (time.monotonic(), rep)
        return rep
