"""Submissions endpoints — reads, submit flavors, and comments.

Distilled from:
    - api-docs: docs/api-docs/submissions.md
    - research: docs/research/03-assignments-submissions-grades.md
    - iOS ref:  canvas-ios/Core/Core/Features/Submissions/,
                canvas-ios/Core/Core/Features/TextSubmission/,
                canvas-ios/Core/Core/Features/SubmitAssignmentExtension/

The submit endpoint takes different body shapes depending on submission_type.
We expose discrete Request classes per submission type rather than one mega
request — the coupling between type and required fields makes the typed
version much easier to read and test.
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.submission import Submission


# Always include these on student submission reads. Without submission_comments
# the Rettberg grade-via-comment pattern is invisible.
_STUDENT_DEFAULT_INCLUDES = [
    "submission_comments",
    "submission_history",
    "rubric_assessment",
]


class GetSubmission(CanvasRequest[Submission]):
    """`GET /api/v1/courses/:c/assignments/:a/submissions/:user_id`.

    user_id defaults to "self" for the current user.
    """

    method: ClassVar[str] = "GET"

    def __init__(
        self,
        course_id: int,
        assignment_id: int,
        user_id: int | str = "self",
        *,
        include: list[str] | None = None,
    ):
        self.course_id = course_id
        self.assignment_id = assignment_id
        self.user_id = user_id
        self.include = include if include is not None else list(_STUDENT_DEFAULT_INCLUDES)

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/assignments/{self.assignment_id}/submissions/{self.user_id}"

    def query_pairs(self) -> list[QueryPair]:
        return [("include[]", inc) for inc in self.include]

    def decode(self, data: Any) -> Submission:
        return Submission.model_validate(data)


class ListSubmissions(CanvasRequest[list[Submission]]):
    """`GET /api/v1/courses/:c/assignments/:a/submissions`.

    For students this typically only returns their own; instructor-scope
    accounts see the roster.
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(
        self,
        course_id: int,
        assignment_id: int,
        *,
        include: list[str] | None = None,
        per_page: int = 50,
    ):
        self.course_id = course_id
        self.assignment_id = assignment_id
        self.include = include if include is not None else list(_STUDENT_DEFAULT_INCLUDES)
        self.per_page = per_page

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/assignments/{self.assignment_id}/submissions"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = [("include[]", inc) for inc in self.include]
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[Submission]:
        if not isinstance(data, list):
            raise TypeError(f"expected list from /submissions, got {type(data).__name__}")
        return [Submission.model_validate(item) for item in data]


class _SubmitBase(CanvasRequest[Submission]):
    """Shared base for the various SubmitX variants.

    Subclasses set `submission_type` and implement `_submission_body()`
    returning the nested submission[...] fields.
    """
    method: ClassVar[str] = "POST"
    submission_type: ClassVar[str] = ""

    def __init__(
        self,
        course_id: int,
        assignment_id: int,
        *,
        comment: str | None = None,
        group_comment: bool = False,
    ):
        self.course_id = course_id
        self.assignment_id = assignment_id
        self.comment = comment
        self.group_comment = group_comment

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/assignments/{self.assignment_id}/submissions"

    def _submission_body(self) -> dict[str, Any]:
        """Subclass hook: return the submission[...] dict without
        submission_type (which _SubmitBase adds)."""
        raise NotImplementedError

    def json_body(self) -> dict[str, Any]:
        submission = {"submission_type": self.submission_type, **self._submission_body()}
        body: dict[str, Any] = {"submission": submission}
        if self.comment:
            body["comment"] = {"text_comment": self.comment}
            if self.group_comment:
                body["submission"]["group_comment"] = True
        return body

    def decode(self, data: Any) -> Submission:
        return Submission.model_validate(data)


class SubmitTextEntry(_SubmitBase):
    """Submit an assignment as `online_text_entry`."""

    submission_type: ClassVar[str] = "online_text_entry"

    def __init__(
        self,
        course_id: int,
        assignment_id: int,
        body: str,
        *,
        comment: str | None = None,
        group_comment: bool = False,
    ):
        super().__init__(course_id, assignment_id, comment=comment, group_comment=group_comment)
        if not body:
            raise ValueError("body cannot be empty for online_text_entry")
        self.body = body

    def _submission_body(self) -> dict[str, Any]:
        return {"body": self.body}


class SubmitUrl(_SubmitBase):
    """Submit an assignment as `online_url`."""

    submission_type: ClassVar[str] = "online_url"

    def __init__(
        self,
        course_id: int,
        assignment_id: int,
        url: str,
        *,
        comment: str | None = None,
        group_comment: bool = False,
    ):
        super().__init__(course_id, assignment_id, comment=comment, group_comment=group_comment)
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("URL submission must start with http:// or https://")
        self.url = url

    def _submission_body(self) -> dict[str, Any]:
        return {"url": self.url}


class SubmitFileUpload(_SubmitBase):
    """Submit an assignment as `online_upload`.

    `file_ids` must be Canvas file IDs that were already uploaded via the
    submission-context file upload endpoint (see file_uploads.py and
    UploadContext.assignment_submission).
    """

    submission_type: ClassVar[str] = "online_upload"

    def __init__(
        self,
        course_id: int,
        assignment_id: int,
        file_ids: list[int],
        *,
        comment: str | None = None,
        group_comment: bool = False,
    ):
        super().__init__(course_id, assignment_id, comment=comment, group_comment=group_comment)
        if not file_ids:
            raise ValueError("file_ids cannot be empty for online_upload")
        self.file_ids = file_ids

    def _submission_body(self) -> dict[str, Any]:
        return {"file_ids": self.file_ids}


class SubmitMediaRecording(_SubmitBase):
    """Submit an assignment as `media_recording`.

    Media comments have their own upload flow (Kaltura) that is not yet
    implemented in this library; the caller must provide a `media_comment_id`
    already obtained elsewhere.
    """

    submission_type: ClassVar[str] = "media_recording"

    def __init__(
        self,
        course_id: int,
        assignment_id: int,
        media_comment_id: str,
        media_comment_type: str,
        *,
        comment: str | None = None,
    ):
        super().__init__(course_id, assignment_id, comment=comment)
        if media_comment_type not in ("audio", "video"):
            raise ValueError("media_comment_type must be 'audio' or 'video'")
        self.media_comment_id = media_comment_id
        self.media_comment_type = media_comment_type

    def _submission_body(self) -> dict[str, Any]:
        return {
            "media_comment_id": self.media_comment_id,
            "media_comment_type": self.media_comment_type,
        }


class AddSubmissionComment(CanvasRequest[Submission]):
    """`PUT /api/v1/courses/:c/assignments/:a/submissions/:user_id` with
    just a comment — Canvas's grading/comment endpoint doubles as the
    "add a comment to my own submission" path.
    """

    method: ClassVar[str] = "PUT"

    def __init__(
        self,
        course_id: int,
        assignment_id: int,
        text: str,
        *,
        user_id: int | str = "self",
        group_comment: bool = False,
        file_ids: list[int] | None = None,
    ):
        if not text:
            raise ValueError("comment text cannot be empty")
        self.course_id = course_id
        self.assignment_id = assignment_id
        self.text = text
        self.user_id = user_id
        self.group_comment = group_comment
        self.file_ids = file_ids or []

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/assignments/{self.assignment_id}/submissions/{self.user_id}"

    def json_body(self) -> dict[str, Any]:
        comment: dict[str, Any] = {"text_comment": self.text}
        if self.group_comment:
            comment["group_comment"] = True
        if self.file_ids:
            comment["file_ids"] = self.file_ids
        return {"comment": comment}

    def decode(self, data: Any) -> Submission:
        return Submission.model_validate(data)


class MarkSubmissionRead(CanvasRequest[None]):
    """`PUT /api/v1/courses/:c/assignments/:a/submissions/:user_id/read`."""

    method: ClassVar[str] = "PUT"

    def __init__(self, course_id: int, assignment_id: int, user_id: int | str = "self"):
        self.course_id = course_id
        self.assignment_id = assignment_id
        self.user_id = user_id

    @property
    def path(self) -> str:
        return (
            f"courses/{self.course_id}/assignments/{self.assignment_id}"
            f"/submissions/{self.user_id}/read"
        )

    def decode(self, data: Any) -> None:
        return None
