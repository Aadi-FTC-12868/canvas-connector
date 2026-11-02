"""Return-type models for intent bundles.

These are richer than the raw endpoint models — they aggregate data the
agent would otherwise have to compose from multiple calls. Every model has
a `summary: str` field with a template-generated human-legible TL;DR, so
an agent can pass that directly to a Telegram or status output without
additional LLM prompting.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _ToolModel(BaseModel):
    """Base for intent-bundle returns. Strict fields, no extras."""
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class SurfaceCoverage(_ToolModel):
    """Whether a requested surface was actually read successfully."""

    surface: str
    course_id: int | None = None
    status: Literal["complete", "unavailable", "skipped"]
    detail: str | None = None
    http_status: int | None = None


class BundleCoverage(_ToolModel):
    """Coverage of requested API surfaces, not proof of all classroom work."""

    status: Literal["complete", "partial", "unavailable"] = "unavailable"
    complete: bool = False
    surfaces: list[SurfaceCoverage] = Field(default_factory=list)


class _BundleModel(_ToolModel):
    coverage: BundleCoverage = Field(default_factory=BundleCoverage)


class UpcomingItem(_ToolModel):
    """One work item coming up (assignment, quiz, discussion, event)."""
    course_id: int | None = None
    course_name: str | None = None
    plannable_id: int | None = None
    plannable_type: str | None = None          # assignment|quiz|discussion_topic|announcement|calendar_event|planner_note
    title: str
    due_at: datetime | None = None
    html_url: str | None = None
    status: str | None = None                  # submitted|missing|late|none (from planner.submissions)
    points_possible: float | None = None

    @property
    def when_str(self) -> str:
        """Human-readable due string relative to now, e.g. 'today 3pm'."""
        if self.due_at is None:
            return "no due date"
        now = datetime.now(tz=self.due_at.tzinfo)
        delta = self.due_at - now
        total_hours = delta.total_seconds() / 3600
        if total_hours < 0:
            return f"overdue ({self.due_at.strftime('%a %b %d')})"
        if total_hours < 24:
            return f"today {self.due_at.strftime('%-I:%M%p').lower()}"
        if total_hours < 48:
            return f"tomorrow {self.due_at.strftime('%-I:%M%p').lower()}"
        return self.due_at.strftime("%a %b %d")


class RecentFeedback(_ToolModel):
    """A submission that has recent grader feedback (comment or grade)."""
    course_id: int | None = None
    course_name: str | None = None
    assignment_id: int | None = None
    assignment_name: str | None = None
    score: float | None = None
    grade: str | None = None
    graded_at: datetime | None = None
    has_comment: bool = False
    comment_count: int = 0


class AnnouncementBrief(_ToolModel):
    """A short announcement tuple for digests."""
    course_id: int | None = None
    course_name: str | None = None
    topic_id: int | None = None
    title: str
    posted_at: datetime | None = None
    unread: bool = False


class CourseCatchup(_BundleModel):
    """A rich per-course 'what is going on' bundle.

    Produced by `canvas.tools.catch_me_up_on_course(course_id)`.
    Every field is filled independently; if one sub-call fails, the
    corresponding field stays empty and a warning is recorded.
    """
    course_id: int
    course_name: str
    term_name: str | None = None

    current_grade: str | None = None           # 'A-' or similar from enrollment
    current_score: float | None = None         # percentage
    grade_reflects: str = "computed_current"   # which enrollment score we pulled

    urgent: list[UpcomingItem] = []
    upcoming: list[UpcomingItem] = []
    overdue: list[UpcomingItem] = []           # due_at in past but not submitted
    missing: list[UpcomingItem] = []           # submission.missing=True

    new_announcements: list[AnnouncementBrief] = []
    unread_discussions: list[AnnouncementBrief] = []
    recent_feedback: list[RecentFeedback] = []

    module_progress_completed: int | None = None
    module_progress_total: int | None = None

    warnings: list[str] = []
    generated_at: datetime
    summary: str                               # template-generated TL;DR


class CrossCourseDigest(_BundleModel):
    """Cross-course 'what is new since last check' digest.

    Produced by `canvas.tools.whats_new()`. Aggregates the activity stream
    summary, planner upcoming, missing work, and unread inbox into one
    rich return.
    """
    generated_at: datetime

    unread_activity_by_type: dict[str, int] = {}  # {"Announcement": 17, ...}
    unread_inbox_count: int = 0

    urgent: list[UpcomingItem] = []            # due in next 48h
    upcoming: list[UpcomingItem] = []          # due in next 7d (non-urgent)
    overdue: list[UpcomingItem] = []
    missing: list[UpcomingItem] = []

    new_announcements: list[AnnouncementBrief] = []
    recent_feedback: list[RecentFeedback] = []

    per_course_status: list[str] = []          # one-line per active course

    warnings: list[str] = []
    summary: str                               # template-generated TL;DR


class MissingInCourse(_ToolModel):
    """Missing/late work summary for a single course."""
    course_id: int
    course_name: str
    missing_count: int = 0
    late_count: int = 0
    oldest_missing_due_at: datetime | None = None
    items: list[UpcomingItem] = []


class MissingWorkAudit(_BundleModel):
    """Cross-course missing/late work audit.

    Produced by `canvas.tools.missing_work_audit()`. Walks every active
    course, pulls assignments with submission state, filters to missing
    and late.
    """
    generated_at: datetime
    total_missing: int = 0
    total_late: int = 0
    by_course: list[MissingInCourse] = []
    warnings: list[str] = []
    summary: str
