"""
High-level MVA Canvas API.

This is the surface the app consumes. It turns the raw Canvas endpoints into the
three things we care about:

  1. the courses the student is taking,
  2. the homework that is outstanding / coming up, and
  3. the course materials (weekly module items) for a given piece of homework.

Design notes (learned from the live MVA data):
  * Most assignments have ``due_at == null``; only the weekly "SUBMIT:" tasks carry
    real deadlines. So "upcoming homework" is built from the To-Do feed (what Canvas
    still wants submitted) MERGED with any assignments that do have a future due date.
  * Course materials live in weekly **Modules**; an assignment appears as a module
    item (``type == 'Assignment'``, ``content_id == assignment.id``). We use that link
    to find the materials for a homework.
"""
from __future__ import annotations

import os
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .client import CanvasClient
from .errors import CanvasNotFoundError
from .models import (
    Assignment,
    Course,
    Homework,
    Module,
    ModuleItem,
    User,
    now_utc,
)
from .login import login_with_credentials
from .session import requests_session_from_cookies

LOCAL_TZ = ZoneInfo(os.environ.get("TZ") or "Europe/London")

DEFAULT_BASE_URL = "https://minervavirtual.instructure.com"
DEFAULT_COOKIES_PATH = ".pw-session/cookies.json"


class MvaCanvas:
    def __init__(self, token: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, client: Optional[CanvasClient] = None):
        self.client = client or CanvasClient(base_url=base_url, token=token)
        self._course_name_cache: dict[int, str] = {}

    # -- construction helpers -------------------------------------------

    @classmethod
    def from_env(cls, dotenv_path: Optional[str] = None) -> "MvaCanvas":
        """
        Build from the environment / a .env file.

        Tries, in order: a personal access token (CANVAS_TOKEN); a username and
        password (CANVAS_USERNAME + CANVAS_PASSWORD), which logs in afresh; then
        an exported cookie file (CANVAS_COOKIES, default
        ``.pw-session/cookies.json``). MVA disables tokens, so one of the last
        two is needed there.
        """
        load_dotenv(dotenv_path)
        base_url = os.environ.get("CANVAS_BASE_URL", DEFAULT_BASE_URL)
        token = os.environ.get("CANVAS_TOKEN")
        if token:
            return cls(token=token, base_url=base_url)

        username = os.environ.get("CANVAS_USERNAME")
        password = os.environ.get("CANVAS_PASSWORD")
        if username and password:
            return cls.from_credentials(username, password, base_url=base_url)

        cookies_path = os.environ.get("CANVAS_COOKIES", DEFAULT_COOKIES_PATH)
        if os.path.exists(cookies_path):
            return cls.from_session(cookies_path=cookies_path, base_url=base_url)

        raise RuntimeError(
            "No credentials found. Set CANVAS_TOKEN or CANVAS_USERNAME + "
            "CANVAS_PASSWORD in .env, or create a session cookies file via tools/capture_login.py + tools/export_session.py "
            f"(expected at {cookies_path})."
        )

    @classmethod
    def from_session(
        cls,
        cookies_path: str = DEFAULT_COOKIES_PATH,
        base_url: str = DEFAULT_BASE_URL,
    ) -> "MvaCanvas":
        """Build using exported browser-login cookies (no access token needed)."""
        sess = requests_session_from_cookies(cookies_path)
        client = CanvasClient(base_url=base_url, session=sess)
        return cls(token=None, base_url=base_url, client=client)

    @classmethod
    def from_credentials(
        cls,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> "MvaCanvas":
        """
        Log in with a Canvas username and password (the ``/login/canvas`` form)
        and build on the resulting cookie session. Raises ``CanvasAuthError`` if
        the login is rejected or needs a second factor.
        """
        sess = login_with_credentials(username, password, base_url=base_url)
        client = CanvasClient(base_url=base_url, session=sess)
        return cls(token=None, base_url=base_url, client=client)

    # -- identity --------------------------------------------------------

    def get_self(self) -> User:
        return User.from_api(self.client.get("/users/self"))

    # -- courses ---------------------------------------------------------

    def get_courses(self, active_only: bool = True) -> list[Course]:
        params = {"include[]": "term"}
        if active_only:
            params["enrollment_state"] = "active"
        raw = self.client.get_paginated("/courses", params=params)
        courses = [Course.from_api(c) for c in raw if "id" in c]
        for c in courses:
            self._course_name_cache[c.id] = c.name
        return courses

    def _course_name(self, course_id: int) -> str:
        if course_id not in self._course_name_cache:
            try:
                c = Course.from_api(self.client.get(f"/courses/{course_id}"))
                self._course_name_cache[course_id] = c.name
            except CanvasNotFoundError:
                self._course_name_cache[course_id] = ""
        return self._course_name_cache[course_id]

    # -- assignments -----------------------------------------------------

    def get_assignments(self, course_id: int) -> list[Assignment]:
        raw = self.client.get_paginated(
            f"/courses/{course_id}/assignments",
            params={"order_by": "due_at", "include[]": "submission"},
        )
        return [Assignment.from_api(a) for a in raw]

    # -- homework (the headline feature) --------------------------------

    def get_todo(self) -> list[Homework]:
        """Outstanding items Canvas wants the student to submit, as Homework."""
        raw = self.client.get_paginated("/users/self/todo")
        items = [Homework.from_todo(t) for t in raw if t.get("type") == "submitting"]
        return items

    def get_upcoming_homework(self, include_todo: bool = True) -> list[Homework]:
        """
        The student's outstanding + upcoming homework, de-duplicated and sorted so the
        soonest deadline is first (undated items last).

        Combines:
          * the To-Do feed (outstanding submissions), and
          * published, unsubmitted assignments with a future due date across all courses.
        """
        by_assignment: dict[Optional[int], Homework] = {}
        extras: list[Homework] = []

        if include_todo:
            for hw in self.get_todo():
                if hw.assignment_id is not None:
                    by_assignment[hw.assignment_id] = hw
                else:
                    extras.append(hw)

        now = now_utc()
        for course in self.get_courses(active_only=True):
            for a in self.get_assignments(course.id):
                if not a.is_published or a.has_submitted:
                    continue
                if a.due_at is None or a.due_at < now:
                    continue
                if a.id in by_assignment:
                    continue
                by_assignment[a.id] = Homework.from_assignment(a, course_name=course.name)

        combined = list(by_assignment.values()) + extras
        combined.sort(key=lambda h: (h.due_at is None, h.due_at or now))
        return combined

    # -- modules / materials --------------------------------------------

    def get_modules(self, course_id: int, with_items: bool = True) -> list[Module]:
        params = {}
        if with_items:
            params["include[]"] = ["items", "content_details"]
        raw = self.client.get_paginated(f"/courses/{course_id}/modules", params=params)
        return [Module.from_api(m) for m in raw]

    def get_current_module(self, course_id: int) -> Optional[Module]:
        """
        Best guess at the week the student is currently working through.

        MVA module names say which week they are for ("... (w/c 16th November)"), so
        the module whose week started most recently, on or before today, wins. Canvas'
        own state is no use for this: a module stays 'started' until every item in it
        is done, so an old week with one unfinished task looks just as current.
        Only when no module names a past week does this fall back to the
        highest-positioned module that is unlocked, then to the last module.
        """
        modules = self.get_modules(course_id, with_items=False)
        if not modules:
            return None
        now = now_utc()
        today = now.astimezone(LOCAL_TZ).date()
        dated = [(m.week_start(today), m) for m in modules]
        dated = [(d, m) for d, m in dated if d is not None and d <= today]
        if dated:
            chosen = max(dated, key=lambda dm: (dm[0], dm[1].position))[1]
            return self._module_with_items(course_id, chosen.id) or chosen

        def available(m: Module) -> bool:
            if m.state in ("completed", "started", "unlocked"):
                return True
            if m.unlock_at is not None and m.unlock_at <= now:
                return True
            return m.state == ""

        candidates = [m for m in modules if m.state == "started"]
        if not candidates:
            candidates = [m for m in modules if available(m)]
        if not candidates:
            candidates = modules
        chosen = max(candidates, key=lambda m: m.position)
        # re-fetch with items so callers get the materials
        return self._module_with_items(course_id, chosen.id) or chosen

    def _module_with_items(self, course_id: int, module_id: int) -> Optional[Module]:
        try:
            raw = self.client.get(
                f"/courses/{course_id}/modules/{module_id}",
                params={"include[]": ["items", "content_details"]},
            )
            return Module.from_api(raw)
        except CanvasNotFoundError:
            return None

    # -- content (for reading a task or material in full) ---------------

    def get_assignment(self, course_id: int, assignment_id: int) -> Assignment:
        return Assignment.from_api(self.client.get(f"/courses/{course_id}/assignments/{assignment_id}"))

    def get_page(self, course_id: int, page_url: str) -> dict:
        """A wiki page: ``title`` and ``body`` (HTML), among others."""
        return self.client.get(f"/courses/{course_id}/pages/{page_url}")

    def get_discussion(self, course_id: int, topic_id: int) -> dict:
        """A discussion topic: ``title`` and ``message`` (HTML), among others."""
        return self.client.get(f"/courses/{course_id}/discussion_topics/{topic_id}")

    def get_file(self, file_id: int) -> dict:
        """File metadata: ``display_name``, ``content-type``, ``size`` and a download ``url``."""
        return self.client.get(f"/files/{file_id}")

    def download(self, url: str, max_bytes: int = 5_000_000) -> bytes:
        """Download a file (e.g. a file's ``url``) with the Canvas session, up to max_bytes."""
        return self.client.download(url, max_bytes=max_bytes)

    def find_module_for_assignment(self, course_id: int, assignment_id: int) -> Optional[Module]:
        """Find the weekly module that contains a given assignment."""
        for m in self.get_modules(course_id, with_items=True):
            if m.assignment_item(assignment_id) is not None:
                return m
        return None

    def get_materials_for_homework(self, homework: Homework) -> list[ModuleItem]:
        """
        The course materials for a piece of homework: the items of the weekly module
        that contains the homework's assignment (Pages, Files, the task itself, etc.).
        If the assignment can't be located in a module, falls back to the current
        module for that course.
        """
        module = None
        if homework.assignment_id is not None:
            module = self.find_module_for_assignment(homework.course_id, homework.assignment_id)
        if module is None:
            module = self.get_current_module(homework.course_id)
        if module is None:
            return []
        return [it for it in module.items if it.is_material]
