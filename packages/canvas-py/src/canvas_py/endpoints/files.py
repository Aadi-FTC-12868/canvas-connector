"""Files endpoints.

Distilled from:
    - research: docs/research/02-files-and-pdfs.md
    - api-docs:  docs/api-docs/files-read.md
    - iOS ref:   canvas-ios/Core/Core/Features/Files/

Real-world note (see research/05-real-world-quirks.md §1): students often
get 403 on `/courses/:id/files` enumeration, but individual file access via
`/files/:id` reliably works. `GetFile` is the workhorse; list endpoints are
fallbacks.
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.file import CanvasFile


class GetFile(CanvasRequest[CanvasFile]):
    """`GET /api/v1/files/:id` — reliable, works for any file the student
    can access. Returns a pre-signed `url` field usable with the bearer
    token to download."""

    method: ClassVar[str] = "GET"

    def __init__(self, file_id: int, *, include: list[str] | None = None):
        self.file_id = file_id
        self.include = include or []

    @property
    def path(self) -> str:
        return f"files/{self.file_id}"

    def query_pairs(self) -> list[QueryPair]:
        return [("include[]", inc) for inc in self.include]

    def decode(self, data: Any) -> CanvasFile:
        return CanvasFile.model_validate(data)


class ListCourseFiles(CanvasRequest[list[CanvasFile]]):
    """`GET /api/v1/courses/:course_id/files`.

    WARNING: frequently returns 403 for students (enumeration disabled by
    default in most Canvas deployments). Prefer Modules-first discovery or
    assignment-description HTML parsing. See research/05-real-world-quirks.md.
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(
        self,
        course_id: int,
        *,
        content_types: list[str] | None = None,
        search_term: str | None = None,
        per_page: int = 50,
    ):
        self.course_id = course_id
        self.content_types = content_types or []
        self.search_term = search_term
        self.per_page = per_page

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/files"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = []
        for ct in self.content_types:
            pairs.append(("content_types[]", ct))
        if self.search_term:
            pairs.append(("search_term", self.search_term))
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[CanvasFile]:
        if not isinstance(data, list):
            raise TypeError(f"expected list from /files, got {type(data).__name__}")
        return [CanvasFile.model_validate(item) for item in data]


class ListUserFiles(CanvasRequest[list[CanvasFile]]):
    """Personal files, including submission folders, with full pagination."""

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(self, *, search_term: str | None = None, content_types: list[str] | None = None):
        self.search_term = search_term
        self.content_types = content_types or []

    @property
    def path(self) -> str:
        return "users/self/files"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = [("per_page", 50)]
        if self.search_term:
            pairs.append(("search_term", self.search_term))
        pairs.extend(("content_types[]", content_type) for content_type in self.content_types)
        return pairs

    def decode(self, data: Any) -> list[CanvasFile]:
        if not isinstance(data, list):
            raise TypeError("expected a list of Canvas files")
        return [CanvasFile.model_validate(item) for item in data]


class ListFolderFiles(ListUserFiles):
    def __init__(self, folder_id: int, *, search_term: str | None = None, content_types: list[str] | None = None):
        super().__init__(search_term=search_term, content_types=content_types)
        self.folder_id = _positive_id(folder_id)

    @property
    def path(self) -> str:
        return f"folders/{self.folder_id}/files"


def _positive_id(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("folder_id must be a positive integer")
    return value


def _folder(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] < 1:
        raise TypeError("expected Canvas folder metadata")
    return data


class ListUserFolders(CanvasRequest[list[dict[str, Any]]]):
    """All user folders as a flat, paginated list with parent IDs."""

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    @property
    def path(self) -> str:
        return "users/self/folders"

    def query_pairs(self) -> list[QueryPair]:
        return [("per_page", 50)]

    def decode(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            raise TypeError("expected a list of Canvas folders")
        return [_folder(item) for item in data]


class ListSubfolders(ListUserFolders):
    """Immediate subfolders only; each result can be followed independently."""

    def __init__(self, folder_id: int):
        self.folder_id = _positive_id(folder_id)

    @property
    def path(self) -> str:
        return f"folders/{self.folder_id}/folders"


class GetFolder(CanvasRequest[dict[str, Any]]):
    method: ClassVar[str] = "GET"

    def __init__(self, folder_id: int):
        self.folder_id = _positive_id(folder_id)

    @property
    def path(self) -> str:
        return f"folders/{self.folder_id}"

    def decode(self, data: Any) -> dict[str, Any]:
        return _folder(data)


class GetUserQuota(CanvasRequest[dict[str, int]]):
    method: ClassVar[str] = "GET"

    @property
    def path(self) -> str:
        return "users/self/files/quota"

    def decode(self, data: Any) -> dict[str, int]:
        if not isinstance(data, dict) or any(type(data.get(key)) is not int or data[key] < 0 for key in ("quota", "quota_used")):
            raise TypeError("expected Canvas user file quota metadata")
        return {"quota": data["quota"], "quota_used": data["quota_used"]}
