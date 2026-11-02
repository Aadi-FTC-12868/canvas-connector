"""File upload endpoints — the three-step Canvas protocol.

Distilled from:
    - api-docs: docs/api-docs/file-uploads.md
    - research: docs/research/02-files-and-pdfs.md §1
    - iOS ref:  canvas-ios/Core/Core/Features/Files/FileSubmission/Model/PrepareUpload/

Three steps:
    1. POST to a context-specific endpoint → returns UploadTarget
    2. POST multipart to UploadTarget.upload_url WITHOUT Canvas auth
    3. GET the Location returned in step 2 WITH Canvas auth, if present.
       Modern storage services can return the finalized CanvasFile directly.

Steps 1 and 3 go through the normal CanvasHTTP dispatcher. Step 2 needs a
separate auth-less client and is orchestrated in the high-level helper in
`_client.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from pydantic import ValidationError

from .._errors import CanvasError
from .._request import CanvasRequest, QueryPair
from ..models.file import CanvasFile
from ..models.upload import UploadTarget


@dataclass(frozen=True)
class UploadContext:
    """Where a file upload is posted to.

    Use the factory methods rather than the constructor. Each context maps
    to a different step-1 endpoint and has different permission semantics.
    """
    kind: Literal["user", "course", "assignment_submission", "submission_comment", "group"]
    course_id: int | None = None
    assignment_id: int | None = None
    group_id: int | None = None

    @classmethod
    def user(cls) -> UploadContext:
        """Personal user-files folder, subject to Canvas permissions and quota."""
        return cls(kind="user")

    @classmethod
    def course(cls, course_id: int) -> UploadContext:
        """Course files folder. Usually teacher-only — students typically can't."""
        return cls(kind="course", course_id=course_id)

    @classmethod
    def assignment_submission(cls, course_id: int, assignment_id: int) -> UploadContext:
        """Assignment attachment storage. Uploading here does not submit work."""
        return cls(
            kind="assignment_submission",
            course_id=course_id,
            assignment_id=assignment_id,
        )

    @classmethod
    def submission_comment(cls, course_id: int, assignment_id: int) -> UploadContext:
        """Files attached to a comment on a submission."""
        return cls(
            kind="submission_comment",
            course_id=course_id,
            assignment_id=assignment_id,
        )

    @classmethod
    def group(cls, group_id: int) -> UploadContext:
        """Group files folder."""
        return cls(kind="group", group_id=group_id)

    def endpoint_path(self) -> str:
        """Path relative to /api/v1 for the step-1 POST."""
        for field, value in (("course_id", self.course_id), ("assignment_id", self.assignment_id), ("group_id", self.group_id)):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{field} must be a positive integer")
        if self.kind == "user":
            return "users/self/files"
        if self.kind == "course":
            if self.course_id is None:
                raise ValueError("course context requires course_id")
            return f"courses/{self.course_id}/files"
        if self.kind == "assignment_submission":
            if self.course_id is None or self.assignment_id is None:
                raise ValueError("assignment_submission context requires course_id and assignment_id")
            return (
                f"courses/{self.course_id}/assignments/{self.assignment_id}"
                "/submissions/self/files"
            )
        if self.kind == "submission_comment":
            if self.course_id is None or self.assignment_id is None:
                raise ValueError("submission_comment context requires course_id and assignment_id")
            return (
                f"courses/{self.course_id}/assignments/{self.assignment_id}"
                "/submissions/self/comments/files"
            )
        if self.kind == "group":
            if self.group_id is None:
                raise ValueError("group context requires group_id")
            return f"groups/{self.group_id}/files"
        raise ValueError(f"unknown upload context kind: {self.kind!r}")


class RequestUploadTarget(CanvasRequest[UploadTarget]):
    """Step 1: notify Canvas of an incoming upload and get the target URL+params.

    Attributes are required/optional per the Canvas spec. `size` is strongly
    recommended; Canvas uses it to enforce quotas pre-upload.
    """

    method: ClassVar[str] = "POST"

    def __init__(
        self,
        context: UploadContext,
        *,
        name: str,
        size: int | None = None,
        content_type: str | None = None,
        on_duplicate: Literal["overwrite", "rename"] = "rename",
        parent_folder_path: str | None = None,
        parent_folder_id: int | None = None,
        success_include: list[str] | None = None,
    ):
        context.endpoint_path()
        if not name or any(character in name for character in ("\0", "\r", "\n", "/", "\\")):
            raise ValueError("name must be a nonempty filename without path separators or control characters")
        if size is not None and (type(size) is not int or size < 0):
            raise ValueError("size must be a nonnegative integer")
        if on_duplicate not in ("rename", "overwrite"):
            raise ValueError("on_duplicate must be rename or overwrite")
        if parent_folder_path is not None and parent_folder_id is not None:
            raise ValueError("parent_folder_path and parent_folder_id are mutually exclusive")
        if parent_folder_id is not None and (type(parent_folder_id) is not int or parent_folder_id < 1):
            raise ValueError("parent_folder_id must be a positive integer")
        if (parent_folder_path is not None or parent_folder_id is not None) and context.kind not in ("user", "course", "group"):
            raise ValueError("folder options require a user, course, or group upload context")
        if content_type is not None and any(character in content_type for character in ("\0", "\r", "\n")):
            raise ValueError("content_type contains control characters")
        self.context = context
        self.name = name
        self.size = size
        self.content_type = content_type
        self.on_duplicate = on_duplicate
        self.parent_folder_path = parent_folder_path
        self.parent_folder_id = parent_folder_id
        self.success_include = success_include or []

    @property
    def path(self) -> str:
        return self.context.endpoint_path()

    def json_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "on_duplicate": self.on_duplicate,
        }
        if self.size is not None:
            body["size"] = self.size
        if self.content_type:
            body["content_type"] = self.content_type
        if self.parent_folder_path:
            body["parent_folder_path"] = self.parent_folder_path
        if self.parent_folder_id is not None:
            body["parent_folder_id"] = self.parent_folder_id
        if self.success_include:
            body["success_include"] = self.success_include
        return body

    def decode(self, data: Any) -> UploadTarget:
        if isinstance(data, dict) and "quota" in str(data.get("message", "")).lower():
            raise CanvasError("Canvas rejected upload because of file quota")
        try:
            target = UploadTarget.model_validate(data)
        except ValidationError:
            # Pydantic errors may include signed URLs and upload signatures.
            raise CanvasError("Canvas did not return a valid upload target") from None
        if "file" in target.upload_params or any(
            not isinstance(value, (str, int, float, bool, type(None)))
            for value in target.upload_params.values()
        ):
            raise CanvasError("Canvas returned unsupported signed upload parameters")
        return target


class ConfirmUpload(CanvasRequest[CanvasFile]):
    """Step 3: confirm upload success.

    The URL is the validated Canvas-origin Location returned from step 2.
    Current documentation requires GET; the legacy empty POST is available
    for older create_success endpoints that reject GET.
    """

    def __init__(self, confirm_url: str, *, method: Literal["GET", "POST"] = "GET"):
        self.confirm_url = confirm_url
        self.method = method

    @property
    def path(self) -> str:
        # Not used when dispatched via absolute_url; returned for repr.
        return self.confirm_url

    def headers(self) -> dict[str, str]:
        # Current documentation requires GET. Older create_success servers
        # can need the documented legacy POST with an explicit empty body.
        return {"Content-Length": "0"} if self.method == "POST" else {}

    def query_pairs(self) -> list[QueryPair]:
        return []

    def decode(self, data: Any) -> CanvasFile:
        return CanvasFile.model_validate(data)
