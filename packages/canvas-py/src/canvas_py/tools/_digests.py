"""Implementation of whats_new() and catch_me_up_on_course().

These compose primitive endpoint calls into rich digest returns. They
deliberately fail soft — any sub-call raising CanvasPermissionError or
CanvasNotFoundError is captured in `warnings` rather than propagating,
because a missing sub-surface should not take down the whole digest.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .._errors import CanvasAuthError, CanvasError
from ._models import (
    AnnouncementBrief,
    BundleCoverage,
    CourseCatchup,
    CrossCourseDigest,
    RecentFeedback,
    SurfaceCoverage,
    UpcomingItem,
)

if TYPE_CHECKING:
    from .._client import Canvas
    from ..models.course import Course
    from ..models.planner import PlannerItem

logger = logging.getLogger("canvas_py.tools.digests")


_DEFAULT_LOOKBACK_DAYS = 14
_DEFAULT_LOOKAHEAD_DAYS = 7
_URGENT_HOURS = 48


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class _Coverage:
    """Record every attempted surface and retain partial failures explicitly."""

    def __init__(self, warnings: list[str]):
        self.warnings = warnings
        self.surfaces: list[SurfaceCoverage] = []

    def ok(self, surface: str, course_id: int | None = None) -> None:
        self.surfaces.append(SurfaceCoverage(surface=surface, course_id=course_id, status="complete"))

    def failed(self, surface: str, error: CanvasError, course_id: int | None = None) -> None:
        if isinstance(error, CanvasAuthError):
            raise error
        self.warnings.append(f"{surface} unavailable" + (f" for course {course_id}" if course_id is not None else "") + f": {error}")
        self.surfaces.append(SurfaceCoverage(
            surface=surface, course_id=course_id, status="unavailable",
            detail=str(error), http_status=error.status,
        ))

    def skipped(self, surface: str, reason: str) -> None:
        self.warnings.append(f"{surface} not checked: {reason}")
        self.surfaces.append(SurfaceCoverage(surface=surface, status="skipped", detail=reason))

    def result(self) -> BundleCoverage:
        success = sum(s.status == "complete" for s in self.surfaces)
        complete = bool(self.surfaces) and success == len(self.surfaces)
        return BundleCoverage(
            complete=complete,
            status="complete" if complete else ("partial" if success else "unavailable"),
            surfaces=self.surfaces,
        )

    def summary(self, findings: str, *, empty: bool = False) -> str:
        coverage = self.result()
        if coverage.complete:
            return findings
        unavailable = sum(s.status != "complete" for s in self.surfaces)
        findings = "no findings in available data" if empty else findings.rstrip(".")
        return f"Incomplete results: {findings}; {unavailable} surface(s) could not be checked."


def _course_code_for(course: "Course") -> str:
    return f"course_{course.id}"


def _extract_upcoming_from_planner(
    items: list["PlannerItem"],
    *,
    course_names: dict[int, str] | None = None,
) -> list[UpcomingItem]:
    """Convert PlannerItem objects into UpcomingItem tuples."""
    out: list[UpcomingItem] = []
    for it in items:
        plannable = it.plannable or {}
        title = plannable.get("title") if isinstance(plannable, dict) else None
        if not title and isinstance(plannable, dict):
            title = plannable.get("name") or plannable.get("details") or "(untitled)"
        submissions = it.submissions if isinstance(it.submissions, dict) else None
        status = None
        if submissions:
            if submissions.get("excused"):
                status = "excused"
            elif submissions.get("missing"):
                status = "missing"
            elif submissions.get("late"):
                status = "late"
            elif submissions.get("submitted"):
                status = "submitted"
            elif submissions.get("graded"):
                status = "graded"
            elif submissions.get("submitted") is False:
                status = "unsubmitted"
        if isinstance(plannable, dict) and status != "missing":
            types = plannable.get("submission_types")
            if (types and set(types) <= {"none", "not_graded", "on_paper"}) or plannable.get("grading_type") == "not_graded":
                status = "not_required"
        points = plannable.get("points_possible") if isinstance(plannable, dict) else None

        # plannable_date is pydantic datetime; fall back to .get on dict if not validated
        due_at = it.plannable_date

        course_name = None
        if course_names and it.course_id is not None:
            course_name = course_names.get(it.course_id)
        if not course_name:
            course_name = it.context_name

        out.append(
            UpcomingItem(
                course_id=it.course_id,
                course_name=course_name,
                plannable_id=it.plannable_id,
                plannable_type=it.plannable_type,
                title=str(title or "(untitled)"),
                due_at=due_at,
                html_url=it.html_url,
                status=status,
                points_possible=points,
            )
        )
    return out


#: Plannable types that are actual student work (can be urgent/overdue/upcoming).
#: Announcements, calendar events, and planner notes are informational and
#: should not be classified as "overdue" just because their posted_at is in
#: the past — they are not work to complete.
_WORK_PLANNABLE_TYPES = {"assignment", "quiz", "discussion_topic"}


def _partition_upcoming(
    items: list[UpcomingItem],
) -> tuple[list[UpcomingItem], list[UpcomingItem], list[UpcomingItem]]:
    """Split items into (urgent, upcoming, overdue).

    Urgent = work due in next URGENT_HOURS.
    Upcoming = work due after urgent but still in the future.
    Overdue = work with due_at in the past and explicit missing/unsubmitted state.
    Unknown submission state is not evidence of missing work.

    Non-work plannable types (announcements, calendar events, planner notes)
    are excluded from all three buckets — they aren't work, so classifying
    them as "overdue" pollutes the student's sense of what is actually behind.
    """
    now = _now()
    urgent: list[UpcomingItem] = []
    upcoming: list[UpcomingItem] = []
    overdue: list[UpcomingItem] = []

    for it in items:
        if it.plannable_type not in _WORK_PLANNABLE_TYPES:
            continue
        if it.due_at is None:
            upcoming.append(it)
            continue
        delta_hours = (it.due_at - now).total_seconds() / 3600
        if delta_hours < 0:
            # past due
            if it.status in ("missing", "unsubmitted"):
                overdue.append(it)
        elif delta_hours <= _URGENT_HOURS:
            urgent.append(it)
        else:
            upcoming.append(it)

    urgent.sort(key=lambda x: x.due_at or now)
    upcoming.sort(key=lambda x: x.due_at or now)
    overdue.sort(key=lambda x: x.due_at or now, reverse=True)
    return urgent, upcoming, overdue


def _fetch_recent_feedback(
    canvas: "Canvas",
    course_id: int,
    course_name: str,
    *,
    since: datetime,
) -> list[RecentFeedback]:
    """List assignments where our submission has a grade posted since `since`
    or comments with authors other than us.

    We use the list-assignments endpoint with submission included, not
    the list-submissions endpoint, because students typically can only
    see their own via the assignment-level include.
    """
    out: list[RecentFeedback] = []
    assignments = canvas.assignments.list(course_id)

    for a in assignments:
        sub = a.submission if isinstance(a.submission, dict) else None
        if not sub:
            continue
        graded_at_raw = sub.get("graded_at")
        posted_at_raw = sub.get("posted_at")
        comments = sub.get("submission_comments") or []
        score = sub.get("score")
        grade = sub.get("grade")

        # parse iso timestamp if present
        graded_at = None
        for raw in (graded_at_raw, posted_at_raw):
            if raw:
                try:
                    graded_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    break
                except (ValueError, AttributeError):
                    continue

        recently_graded = graded_at is not None and graded_at >= since
        has_recent_comment = False
        if isinstance(comments, list):
            for c in comments:
                created = c.get("created_at") if isinstance(c, dict) else None
                if created:
                    try:
                        ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if ts >= since:
                            has_recent_comment = True
                            break
                    except (ValueError, AttributeError):
                        continue

        if not (recently_graded or has_recent_comment):
            continue

        out.append(
            RecentFeedback(
                course_id=course_id,
                course_name=course_name,
                assignment_id=a.id,
                assignment_name=a.name,
                score=score,
                grade=grade,
                graded_at=graded_at,
                has_comment=bool(comments),
                comment_count=len(comments) if isinstance(comments, list) else 0,
            )
        )

    out.sort(key=lambda r: r.graded_at or since, reverse=True)
    return out


def _fetch_announcements_brief(
    canvas: "Canvas",
    courses: list["Course"],
    *,
    since: datetime,
) -> list[AnnouncementBrief]:
    """Pull recent announcements across the given courses."""
    if not courses:
        return []
    raw = canvas.announcements.list_across(
        [c.id for c in courses],
        start_date=since.strftime("%Y-%m-%d"),
    )

    # Map context_code -> course name for display
    names: dict[str, tuple[int, str]] = {}
    for c in courses:
        names[f"course_{c.id}"] = (c.id, c.display_name)

    out: list[AnnouncementBrief] = []
    for topic in raw:
        ctx_code = getattr(topic, "context_code", None) or (
            topic.model_extra.get("context_code") if topic.model_extra else None
        )
        course_id, course_name = names.get(ctx_code or "", (None, None))
        out.append(
            AnnouncementBrief(
                course_id=course_id,
                course_name=course_name,
                topic_id=topic.id,
                title=topic.title or "(untitled)",
                posted_at=topic.posted_at,
                unread=(topic.read_state == "unread"),
            )
        )
    out.sort(key=lambda a: a.posted_at or since, reverse=True)
    return out


def _grade_from_enrollment(course: "Course") -> tuple[str | None, float | None, str]:
    """Extract a readable grade + numeric score from course.enrollments.

    Returns (grade_str, score, which) where `which` identifies which field
    we pulled from (for debugging).
    """
    enrollments = course.enrollments if isinstance(course.enrollments, list) else None
    if not enrollments:
        return None, None, "none"
    e = enrollments[0]
    # Prefer current period if available, else overall current.
    for key_grade, key_score, label in (
        ("current_period_computed_current_grade", "current_period_computed_current_score", "current_period_current"),
        ("computed_current_grade", "computed_current_score", "computed_current"),
        ("computed_final_grade", "computed_final_score", "computed_final"),
    ):
        grade = e.get(key_grade)
        score = e.get(key_score)
        if grade is not None or score is not None:
            return grade, score, label
    return None, None, "none"


# --- whats_new --------------------------------------------------------

def whats_new(
    canvas: "Canvas",
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = _DEFAULT_LOOKAHEAD_DAYS,
) -> CrossCourseDigest:
    """Cross-course 'what happened since last check' digest.

    Aggregates:
        - activity stream summary (unread counts)
        - inbox unread count
        - planner items (next `lookahead_days`)
        - cross-course announcements (last `lookback_days`)
        - missing/overdue work from the planner
    """
    warnings: list[str] = []
    coverage = _Coverage(warnings)
    since = _now() - timedelta(days=lookback_days)
    until = _now() + timedelta(days=lookahead_days)

    # Activity stream summary — cheap, reliable.
    unread_by_type: dict[str, int] = {}
    try:
        summary = canvas.activity.summary()
        for entry in summary:
            t = entry.get("type", "?")
            unread_by_type[t] = int(entry.get("unread_count", 0) or 0)
        coverage.ok("activity_summary")
    except CanvasError as e:
        coverage.failed("activity_summary", e)

    # Inbox unread.
    unread_inbox = 0
    try:
        unread_inbox = len(canvas.inbox.unread())
        coverage.ok("inbox_unread")
    except CanvasError as e:
        coverage.failed("inbox_unread", e)

    # Active courses for name resolution + announcements.
    try:
        courses = canvas.courses.list_active()
        coverage.ok("active_courses")
    except CanvasError as e:
        coverage.failed("active_courses", e)
        courses = []
    course_names = {c.id: c.display_name for c in courses}

    # Planner items — future.
    urgent: list[UpcomingItem] = []
    upcoming: list[UpcomingItem] = []
    overdue: list[UpcomingItem] = []
    missing: list[UpcomingItem] = []
    try:
        planner = canvas.planner.items(
            start_date=since.strftime("%Y-%m-%d"),
            end_date=until.strftime("%Y-%m-%d"),
        )
        items = _extract_upcoming_from_planner(planner, course_names=course_names)
        urgent, upcoming, overdue = _partition_upcoming(items)
        missing = [it for it in items if it.status == "missing"]
        coverage.ok("planner")
    except CanvasError as e:
        coverage.failed("planner", e)

    # Announcements.
    announcements: list[AnnouncementBrief] = []
    if courses:
        try:
            announcements = _fetch_announcements_brief(canvas, courses, since=since)
            coverage.ok("announcements")
        except CanvasError as e:
            coverage.failed("announcements", e)
    elif any(s.surface == "active_courses" and s.status != "complete" for s in coverage.surfaces):
        coverage.skipped("announcements", "active courses unavailable")
        coverage.skipped("recent_feedback", "active courses unavailable")

    # Recent feedback — every active course; never silently cap coverage.
    recent_feedback: list[RecentFeedback] = []
    for c in courses:
        try:
            fb = _fetch_recent_feedback(canvas, c.id, c.display_name, since=since)
            recent_feedback.extend(fb)
            coverage.ok("recent_feedback", c.id)
        except CanvasError as e:
            coverage.failed("recent_feedback", e, c.id)
    recent_feedback.sort(key=lambda r: r.graded_at or since, reverse=True)

    # Per-course one-line status.
    per_course: list[str] = []
    for c in courses:
        cid = c.id
        up_count = sum(1 for it in upcoming if it.course_id == cid)
        ur_count = sum(1 for it in urgent if it.course_id == cid)
        m_count = sum(1 for it in missing if it.course_id == cid)
        grade_str, score, _label = _grade_from_enrollment(c)
        bits: list[str] = []
        if grade_str or score is not None:
            bits.append(f"grade {grade_str or ''}{f' ({score}%)' if score is not None else ''}".strip())
        if ur_count:
            bits.append(f"{ur_count} urgent")
        if up_count:
            bits.append(f"{up_count} upcoming")
        if m_count:
            bits.append(f"{m_count} missing")
        if not bits:
            bits.append("no changes found in checked surfaces" if coverage.result().complete else "no findings in available data; checks incomplete")
        per_course.append(f"{c.display_name}: {', '.join(bits)}")

    # Summary template.
    parts: list[str] = []
    total_unread = sum(unread_by_type.values()) + unread_inbox
    if total_unread:
        parts.append(f"{total_unread} unread items")
    if urgent:
        parts.append(f"{len(urgent)} due in next {_URGENT_HOURS}h")
    if missing:
        parts.append(f"{len(missing)} missing")
    if overdue:
        parts.append(f"{len(overdue)} overdue")
    if recent_feedback:
        parts.append(f"{len(recent_feedback)} recent grader responses")
    summary = "; ".join(parts) if parts else "nothing urgent."
    summary = coverage.summary(summary, empty=not parts)

    return CrossCourseDigest(
        generated_at=_now(),
        unread_activity_by_type=unread_by_type,
        unread_inbox_count=unread_inbox,
        urgent=urgent,
        upcoming=upcoming,
        overdue=overdue,
        missing=missing,
        new_announcements=announcements,
        recent_feedback=recent_feedback,
        per_course_status=per_course,
        warnings=warnings,
        coverage=coverage.result(),
        summary=summary,
    )


# --- catch_me_up_on_course -------------------------------------------

def catch_me_up_on_course(
    canvas: "Canvas",
    course_id: int,
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = _DEFAULT_LOOKAHEAD_DAYS,
) -> CourseCatchup:
    """Rich per-course digest answering 'what is going on in this class.'"""
    warnings: list[str] = []
    coverage = _Coverage(warnings)
    since = _now() - timedelta(days=lookback_days)
    until = _now() + timedelta(days=lookahead_days)

    # Course metadata — authoritative source of name + term + grade.
    course = canvas.courses.get(course_id)
    coverage.ok("course", course_id)

    course_name = course.display_name
    term_name = None
    if isinstance(course.term, dict):
        term_name = course.term.get("name")

    grade_str, score, grade_reflects = _grade_from_enrollment(course)

    # Planner scoped to this course.
    urgent: list[UpcomingItem] = []
    upcoming: list[UpcomingItem] = []
    overdue: list[UpcomingItem] = []
    missing: list[UpcomingItem] = []
    try:
        planner = canvas.planner.items(
            start_date=since.strftime("%Y-%m-%d"),
            end_date=until.strftime("%Y-%m-%d"),
            context_codes=[f"course_{course_id}"],
        )
        items = _extract_upcoming_from_planner(
            planner, course_names={course_id: course_name}
        )
        urgent, upcoming, overdue = _partition_upcoming(items)
        missing = [it for it in items if it.status == "missing"]
        coverage.ok("planner", course_id)
    except CanvasError as e:
        coverage.failed("planner", e, course_id)

    # Announcements.
    new_announcements: list[AnnouncementBrief] = []
    try:
        topics = canvas.announcements.list_across(
            [course_id],
            start_date=since.strftime("%Y-%m-%d"),
        )
        for t in topics:
            new_announcements.append(
                AnnouncementBrief(
                    course_id=course_id,
                    course_name=course_name,
                    topic_id=t.id,
                    title=t.title or "(untitled)",
                    posted_at=t.posted_at,
                    unread=(t.read_state == "unread"),
                )
            )
        coverage.ok("announcements", course_id)
    except CanvasError as e:
        coverage.failed("announcements", e, course_id)

    # Unread discussions (excluding announcements — those are in announcements above).
    unread_discussions: list[AnnouncementBrief] = []
    try:
        topics = canvas.discussions.list(course_id)
        for t in topics:
            if (t.unread_count or 0) > 0:
                unread_discussions.append(
                    AnnouncementBrief(
                        course_id=course_id,
                        course_name=course_name,
                        topic_id=t.id,
                        title=t.title or "(untitled)",
                        posted_at=t.posted_at,
                        unread=True,
                    )
                )
        coverage.ok("discussions", course_id)
    except CanvasError as e:
        coverage.failed("discussions", e, course_id)

    # Recent feedback.
    recent_feedback: list[RecentFeedback] = []
    try:
        recent_feedback = _fetch_recent_feedback(canvas, course_id, course_name, since=since)
        coverage.ok("recent_feedback", course_id)
    except CanvasError as e:
        coverage.failed("recent_feedback", e, course_id)

    # Module progress.
    mod_completed = None
    mod_total = None
    if isinstance(course.course_progress, dict):
        mod_total = course.course_progress.get("requirement_count")
        mod_completed = course.course_progress.get("requirement_completed_count")

    # Template summary.
    parts: list[str] = []
    if grade_str or score is not None:
        g = grade_str or ""
        s = f" ({score}%)" if score is not None else ""
        parts.append(f"grade {g}{s}".strip())
    if urgent:
        parts.append(f"{len(urgent)} due in next {_URGENT_HOURS}h")
    if upcoming:
        parts.append(f"{len(upcoming)} upcoming")
    if missing:
        parts.append(f"{len(missing)} missing")
    if overdue:
        parts.append(f"{len(overdue)} overdue")
    if new_announcements:
        parts.append(f"{len(new_announcements)} recent announcements")
    if unread_discussions:
        parts.append(f"{len(unread_discussions)} unread discussions")
    if recent_feedback:
        parts.append(f"{len(recent_feedback)} recent grader responses")
    if mod_total:
        parts.append(f"module progress {mod_completed or 0}/{mod_total}")
    summary = "; ".join(parts) if parts else "quiet."
    summary = coverage.summary(summary, empty=not parts)

    return CourseCatchup(
        course_id=course_id,
        course_name=course_name,
        term_name=term_name,
        current_grade=grade_str,
        current_score=score,
        grade_reflects=grade_reflects,
        urgent=urgent,
        upcoming=upcoming,
        overdue=overdue,
        missing=missing,
        new_announcements=new_announcements,
        unread_discussions=unread_discussions,
        recent_feedback=recent_feedback,
        module_progress_completed=mod_completed,
        module_progress_total=mod_total,
        warnings=warnings,
        coverage=coverage.result(),
        generated_at=_now(),
        summary=summary,
    )
