"""Submission models.

Reference:
    - api-docs: docs/api-docs/submissions.md
    - research: docs/research/03-assignments-submissions-grades.md

Important: `submission_comments` are treated as first-class on the Submission
object because some teachers (e.g. Aadi's Mr. Rettberg) post grading feedback
primarily through comments rather than through the numeric score+grade fields.
Any tool that surfaces a submission to an agent should present comments
prominently, not as an afterthought.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import computed_field

from .base import CanvasModel


class SubmissionComment(CanvasModel):
    """A single comment on a submission (from grader or student)."""
    id: int
    author_id: int | None = None
    author_name: str | None = None
    comment: str | None = None  # plain text
    created_at: datetime | None = None
    edited_at: datetime | None = None
    media_comment: dict[str, Any] | None = None
    attachments: list[dict[str, Any]] | None = None
    attempt: int | None = None  # which attempt this comment references


class RubricCriterionAssessment(CanvasModel):
    """One criterion's score within a rubric_assessment dict on a submission."""
    points: float | None = None
    rating_id: str | None = None
    comments: str | None = None


class Submission(CanvasModel):
    """A student's submission to an assignment.

    Reference: docs/api-docs/submissions.md §6.
    Submission objects include several include[]-dependent fields
    (submission_comments, submission_history, rubric_assessment, assignment).
    """
    id: int | None = None
    assignment_id: int | None = None
    user_id: int | None = None
    attempt: int | None = None
    body: str | None = None                          # for text submissions
    url: str | None = None                           # for URL submissions / preview
    submission_type: str | None = None
    submitted_at: datetime | None = None
    score: float | None = None
    grade: str | None = None
    grade_matches_current_submission: bool | None = None
    workflow_state: str | None = None                # submitted|unsubmitted|graded|pending_review
    late: bool | None = None
    missing: bool | None = None
    seconds_late: int | None = None
    late_policy_status: str | None = None
    points_deducted: float | None = None
    excused: bool | None = None
    posted_at: datetime | None = None
    read_status: str | None = None
    html_url: str | None = None
    preview_url: str | None = None
    grader_id: int | None = None
    graded_at: datetime | None = None
    extra_attempts: int | None = None
    redo_request: bool | None = None

    # include[]-dependent
    submission_comments: list[SubmissionComment] | None = None
    submission_history: list[dict[str, Any]] | None = None
    rubric_assessment: dict[str, Any] | None = None
    attachments: list[dict[str, Any]] | None = None
    assignment: dict[str, Any] | None = None
    user: dict[str, Any] | None = None
    group: dict[str, Any] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_feedback(self) -> bool:
        """True if there's any grader comment or a graded rubric assessment."""
        if self.submission_comments:
            return True
        if self.rubric_assessment:
            return True
        return False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status_summary(self) -> str:
        """One-line human summary combining workflow_state with late/missing flags."""
        parts: list[str] = []
        if self.excused:
            return "excused"
        if self.missing:
            parts.append("missing")
        if self.late:
            parts.append("late")
        if self.workflow_state:
            parts.append(self.workflow_state)
        if self.score is not None:
            parts.append(f"score={self.score}")
        return " / ".join(parts) if parts else "unknown"
