"""
Typed, friendly models over the raw Canvas JSON.

Each model has a ``from_api`` classmethod that takes the raw dict Canvas returns
and keeps the original payload on ``.raw`` so nothing is lost. Only the fields the
MVA app actually needs are promoted to attributes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
            has_submitted=bool(d.get("has_submitted_submissions", False)),
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
            submitted=bool(a.get("has_submitted_submissions", False)),
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
