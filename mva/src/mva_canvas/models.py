"""
Typed, friendly models over the raw Canvas JSON.

Each model has a ``from_api`` classmethod that takes the raw dict Canvas returns
and keeps the original payload on ``.raw`` so nothing is lost. Only the fields the
MVA app actually needs are promoted to attributes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a Canvas ISO-8601 timestamp (e.g. '2026-11-16T23:59:59Z') to aware UTC."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


_SUBMITTED_STATES = ("submitted", "graded", "pending_review", "complete")


def own_submission_done(assignment: dict) -> bool:
    """
    Whether *this* student has submitted (or been excused from) an assignment.

    Reads the student's own ``submission`` (present when the assignment was
    fetched with ``include[]=submission``; a one-element list if
    ``observed_users`` was also included). Deliberately ignores
    ``has_submitted_submissions``: that is true once *any* student in the
    course has submitted, so it hides work the student still has to do.
    """
    sub = assignment.get("submission")
    if isinstance(sub, list):
        sub = sub[0] if sub else None
    if not isinstance(sub, dict):
        return False
    return bool(
        sub.get("excused")
        or sub.get("submitted_at")
        or sub.get("workflow_state") in _SUBMITTED_STATES
    )


@dataclass
class User:
    id: int
    name: str
    short_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "User":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            short_name=d.get("short_name", d.get("name", "")),
            raw=d,
        )


@dataclass
class Course:
    id: int
    name: str
    course_code: str = ""
    workflow_state: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Course":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            course_code=d.get("course_code", ""),
            workflow_state=d.get("workflow_state", ""),
            raw=d,
        )


@dataclass
class Assignment:
    id: int
    course_id: int
    name: str
    due_at: Optional[datetime]
    html_url: str = ""
    points_possible: Optional[float] = None
    submission_types: list[str] = field(default_factory=list)
    has_submitted: bool = False
    workflow_state: str = ""
    description_html: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Assignment":
        return cls(
            id=d["id"],
            course_id=d.get("course_id"),
            name=d.get("name", ""),
            due_at=parse_dt(d.get("due_at")),
            html_url=d.get("html_url", ""),
            points_possible=d.get("points_possible"),
            submission_types=list(d.get("submission_types", [])),
            has_submitted=own_submission_done(d),
            workflow_state=d.get("workflow_state", ""),
            description_html=d.get("description") or "",
            raw=d,
        )

    @property
    def is_published(self) -> bool:
        return self.workflow_state == "published"


@dataclass
class ModuleItem:
    """An item inside a module: Page, File, Assignment, SubHeader, Discussion, etc."""

    id: int
    module_id: Optional[int]
    title: str
    type: str
    content_id: Optional[int] = None  # e.g. the assignment id for type == 'Assignment'
    html_url: str = ""
    url: str = ""          # API url to the underlying object (Page/File/etc.)
    page_url: str = ""     # slug for Page items
    indent: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "ModuleItem":
        return cls(
            id=d["id"],
            module_id=d.get("module_id"),
            title=d.get("title", ""),
            type=d.get("type", ""),
            content_id=d.get("content_id"),
            html_url=d.get("html_url", ""),
            url=d.get("url", ""),
            page_url=d.get("page_url", ""),
            indent=d.get("indent", 0),
            raw=d,
        )

    @property
    def is_material(self) -> bool:
        """SubHeaders are layout only; everything else is a real material/task."""
        return self.type != "SubHeader"

    @property
    def completion_type(self) -> str:
        """Canvas' requirement for ticking this item off ('must_view', 'must_mark_done',
        'must_submit', ...), or '' when the item has none (plain reference material)."""
        req = self.raw.get("completion_requirement") or {}
        return req.get("type", "")

    @property
    def is_todo(self) -> bool:
        """True for an item the student still has to do: it has a completion
        requirement (the circle in the Canvas module view) that isn't ticked yet."""
        req = self.raw.get("completion_requirement") or {}
        return bool(req) and not req.get("completed", False)

    @property
    def has_requirement(self) -> bool:
        """True when Canvas shows a tick/circle for this item in the module view."""
        return bool(self.raw.get("completion_requirement"))

    @property
    def is_done(self) -> bool:
        """True when this item's completion requirement is ticked off."""
        req = self.raw.get("completion_requirement") or {}
        return bool(req.get("completed", False))


_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december"]
_MONTH = r"([a-z]{3,9})"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
# A date in a module name: "16th November", "16 of Nov", "7th Sept", or month-first
# "October 5". Years are ignored (see parse_week_start).
_DAY_MONTH = re.compile(r"\b" + _DAY + r"\s+(?:of\s+)?" + _MONTH + r"\b", re.I)
_MONTH_DAY = re.compile(r"\b" + _MONTH + r"\s+" + _DAY + r"\b", re.I)
# Numeric dates only after "w/c" / "w/b" / "week commencing", so "Weeks 1-2" isn't one.
_WC_NUM = re.compile(r"(?:\bw\.?\s*/?\s*[cb]\.?|\bweek\s+(?:commencing|beginning))\s*:?\s*"
                     r"(?:[a-z]+day\s+)?(\d{1,2})[/.-](\d{1,2})", re.I)


def _month(word: str) -> Optional[int]:
    """'Nov', 'Sept', 'November' -> 11; 'Decimal', 'Marking' -> None."""
    w = word.lower()
    for i, name in enumerate(_MONTH_NAMES, start=1):
        if len(w) >= 3 and (name.startswith(w) or (w == "sept" and i == 9)):
            return i
    return None


def parse_week_start(name: str, today: date) -> Optional[date]:
    """
    The week a module is for, from the date in its name: MVA teachers write
    "(w/c 16th November)", "(Mon 16th Nov 2026)", "- 16th Nov", "Monday October 5 2026",
    "w/c 16/11" and so on. If a name has several dates the last one wins (one module
    reads "Weeks 7-8 (Mon 19th Nov) ... (Mon 2 Nov 2026)"). Any year written is ignored
    because some are wrong ("5th October 2025" in the 2026 term); the year used is the
    one putting the date closest to today.
    """
    found: list[tuple[int, int, int]] = []  # (position in name, day, month)
    for m in _DAY_MONTH.finditer(name):
        month = _month(m.group(2))
        if month:
            found.append((m.start(), int(m.group(1)), month))
    for m in _MONTH_DAY.finditer(name):
        month = _month(m.group(1))
        if month:
            found.append((m.start(), int(m.group(2)), month))
    for m in _WC_NUM.finditer(name):
        found.append((m.start(), int(m.group(1)), int(m.group(2))))
    for _, day, month in sorted(found, reverse=True):
        options = []
        for y in (today.year - 1, today.year, today.year + 1):
            try:
                options.append(date(y, month, day))
            except ValueError:
                pass
        if options:
            return min(options, key=lambda d: abs((d - today).days))
    return None


@dataclass
class Module:
    id: int
    name: str
    position: int
    state: str = ""          # 'completed', 'started', 'locked', 'unlocked', or '' (unauth views)
    items_count: int = 0
    unlock_at: Optional[datetime] = None
    items: list[ModuleItem] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Module":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            position=d.get("position", 0),
            state=d.get("state", ""),
            items_count=d.get("items_count", 0),
            unlock_at=parse_dt(d.get("unlock_at")),
            items=[ModuleItem.from_api(i) for i in d.get("items", [])],
            raw=d,
        )

    def week_start(self, today: date) -> Optional[date]:
        """The week this module is for, from the date in its name, or None."""
        return parse_week_start(self.name, today)

    def assignment_item(self, assignment_id: int) -> Optional[ModuleItem]:
        for it in self.items:
            if it.type == "Assignment" and it.content_id == assignment_id:
                return it
        return None


@dataclass
class Homework:
    """
    A unified 'thing the student has to do', merged from the To-Do feed and from
    dated assignments. This is the primary object the app will show.
    """

    course_id: int
    course_name: str
    title: str
    assignment_id: Optional[int]
    due_at: Optional[datetime]
    html_url: str
    submitted: bool
    source: str  # 'todo' or 'assignment'
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_overdue(self) -> bool:
        return self.due_at is not None and self.due_at < now_utc() and not self.submitted

    @classmethod
    def from_todo(cls, d: dict) -> "Homework":
        a = d.get("assignment", {}) or {}
        return cls(
            course_id=d.get("course_id"),
            course_name=d.get("context_name", ""),
            title=a.get("name", ""),
            assignment_id=a.get("id"),
            due_at=parse_dt(a.get("due_at")),
            html_url=d.get("html_url") or a.get("html_url", ""),
            submitted=own_submission_done(a),
            source="todo",
            raw=d,
        )

    @classmethod
    def from_assignment(cls, a: Assignment, course_name: str = "") -> "Homework":
        return cls(
            course_id=a.course_id,
            course_name=course_name,
            title=a.name,
            assignment_id=a.id,
            due_at=a.due_at,
            html_url=a.html_url,
            submitted=a.has_submitted,
            source="assignment",
            raw=a.raw,
        )
