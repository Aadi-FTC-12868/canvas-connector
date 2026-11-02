"""Cross-course missing/late work audit with explicit evidence and coverage."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .._errors import CanvasError
from ._digests import _Coverage
from ._models import MissingInCourse, MissingWorkAudit, UpcomingItem

if TYPE_CHECKING:
    from .._client import Canvas


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def missing_work_audit(canvas: "Canvas") -> MissingWorkAudit:
    """Scan all active-course assignments for Canvas's missing/late flags.

    All assignment buckets are required: submitted late work is absent from
    the unsubmitted bucket. A past due date alone does not prove missing work,
    particularly for on-paper and non-submittable classroom activities.
    """
    warnings: list[str] = []
    coverage = _Coverage(warnings)
    try:
        courses = canvas.courses.list_active()
        coverage.ok("active_courses")
    except CanvasError as e:
        coverage.failed("active_courses", e)
        coverage.skipped("assignments", "active courses unavailable")
        return MissingWorkAudit(
            generated_at=_now(), warnings=warnings, coverage=coverage.result(),
            summary=coverage.summary("could not fetch active courses"),
        )

    by_course: list[MissingInCourse] = []
    total_missing = 0
    total_late = 0

    for c in courses:
        try:
            assignments = canvas.assignments.list(c.id, include=["submission"])
            coverage.ok("assignments", c.id)
        except CanvasError as e:
            coverage.failed("assignments", e, c.id)
            continue

        course_entry = MissingInCourse(course_id=c.id, course_name=c.display_name)
        for a in assignments:
            sub = a.submission if isinstance(a.submission, dict) else None
            if not sub or sub.get("excused"):
                continue
            is_missing = sub.get("missing") is True
            is_late = sub.get("late") is True
            if not (is_missing or is_late):
                continue
            course_entry.missing_count += int(is_missing)
            course_entry.late_count += int(is_late)
            total_missing += int(is_missing)
            total_late += int(is_late)
            course_entry.items.append(UpcomingItem(
                course_id=c.id, course_name=c.display_name,
                plannable_id=a.id, plannable_type="assignment",
                title=a.name or f"assignment_{a.id}", due_at=a.due_at,
                html_url=a.html_url, status="missing" if is_missing else "late",
                points_possible=a.points_possible,
            ))
            if is_missing and a.due_at is not None:
                if course_entry.oldest_missing_due_at is None or a.due_at < course_entry.oldest_missing_due_at:
                    course_entry.oldest_missing_due_at = a.due_at
        if course_entry.items:
            by_course.append(course_entry)

    if total_missing == 0 and total_late == 0:
        summary = "No Canvas-flagged missing or late work in checked active courses."
    else:
        parts = []
        if total_missing:
            parts.append(f"{total_missing} missing")
        if total_late:
            parts.append(f"{total_late} late")
        parts.append(f"across {len(by_course)} course{'s' if len(by_course) != 1 else ''}")
        summary = ", ".join(parts) + "."
    return MissingWorkAudit(
        generated_at=_now(), total_missing=total_missing, total_late=total_late,
        by_course=by_course, warnings=warnings, coverage=coverage.result(),
        summary=coverage.summary(summary, empty=not (total_missing or total_late)),
    )
