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

logger = logging.getLogger(__name__)

T = TypeVar("T")

LOCAL_TZ = ZoneInfo(os.environ.get("TZ") or "Europe/London")
REPORT_TTL_SECONDS = int(os.environ.get("REPORT_TTL_SECONDS", "300"))
MAX_WORKERS = 6


@dataclass
class Item:
    course_name: str
    title: str
    html_url: str
    due: str            # formatted local due date, or "" if none
    module_name: str = ""


@dataclass
class Report:
    user_name: str
    overdue: list[Item] = field(default_factory=list)
    this_week_due: list[Item] = field(default_factory=list)
    this_week_tasks: list[Item] = field(default_factory=list)
    upcoming: list[Item] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    generated_at: str = ""
    days: int = 7


def fmt_due(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(LOCAL_TZ).strftime("%a %d %b %H:%M")


def _item(hw: Homework, module_name: str = "") -> Item:
    return Item(
        course_name=hw.course_name,
        title=hw.title,
        html_url=hw.html_url,
        due=fmt_due(hw.due_at),
        module_name=module_name,
    )


def build_report(api: MvaCanvas, days: int = 7) -> Report:
    """Fetch every course's assignments and current module and sort them into buckets."""
    me = api.get_self()
    now = now_utc()
    horizon = now + timedelta(days=days)
    courses = api.get_courses()

    def fetch(course):
        try:
            return course, api.get_assignments(course.id), api.get_current_module(course.id), None
        except CanvasAuthError:
            raise
        except CanvasError as e:
            return course, None, None, e

    overdue: list[Homework] = []
    due_soon: list[Homework] = []
    upcoming: list[Homework] = []
    tasks: list[tuple[Homework, str]] = []
    page_tasks: list[Item] = []
    skipped: list[str] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(fetch, courses))

    for course, assignments, module, err in results:
        if err is not None:
            logger.warning("Skipped course %s: %s", course.name, err)
            skipped.append(course.name)
            continue
        week_items = module.items if module else []
        week_ids = {it.content_id for it in week_items if it.type == "Assignment"}
        module_name = module.name if module else ""
        # Pages and other non-assignment items the module still wants done
        # (e.g. "PREPARE: Revision", ticked off by viewing or "Mark as done").
        for it in week_items:
            if it.type in ("Assignment", "SubHeader") or not it.is_todo:
                continue
            page_tasks.append(Item(course_name=course.name, title=it.title,
                                   html_url=it.html_url, due="", module_name=module_name))
        for a in assignments or []:
            if not a.is_published or a.has_submitted:
                continue
            hw = Homework.from_assignment(a, course_name=course.name)
            if a.due_at is None:
                if a.id in week_ids:
                    tasks.append((hw, module_name))
            elif a.due_at < now:
                overdue.append(hw)
            elif a.due_at <= horizon:
                due_soon.append(hw)
            else:
                upcoming.append(hw)

    def by_due(h: Homework) -> datetime:
        return h.due_at or now

    overdue.sort(key=by_due)
    due_soon.sort(key=by_due)
    upcoming.sort(key=by_due)

    return Report(
        user_name=me.name,
        overdue=[_item(h) for h in overdue],
        this_week_due=[_item(h) for h in due_soon],
        this_week_tasks=sorted([_item(h, m) for h, m in tasks] + page_tasks,
                               key=lambda i: (i.course_name, i.title)),
        upcoming=[_item(h) for h in upcoming],
        skipped=sorted(skipped),
        generated_at=datetime.now(LOCAL_TZ).strftime("%a %d %b %H:%M"),
        days=days,
    )


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
