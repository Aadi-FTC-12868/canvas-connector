"""Member-aware group reads and reviewed student discussion-topic creation.

Official contracts: https://developerdocs.instructure.com/services/canvas/resources/
{groups,courses,discussion_topics,pages,files}. Reads never mark content read,
join groups, or post to defeat an initial-post gate. All routes are fixed here.
"""
from __future__ import annotations

from itertools import islice
from typing import Annotated, Any, Literal
from urllib.parse import quote

from canvas_py import Canvas
from canvas_py._errors import CanvasError
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .responses import dump, envelope, window

Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CourseContext(_Model):
    context_type: Literal["course"]
    course_id: Id


class GroupContext(_Model):
    context_type: Literal["group"]
    group_id: Id


ContentContext = Annotated[CourseContext | GroupContext, Field(discriminator="context_type")]
_CONTEXT = TypeAdapter(ContentContext)
_ID = TypeAdapter(Id)


class TopicCreate(_Model):
    kind: Literal["topic_create"]
    context: ContentContext
    title: Annotated[str, Field(min_length=1, max_length=255, strict=True)]
    message: Annotated[str, Field(min_length=1, max_length=100000, strict=True)]

    @field_validator("title", "message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Discussion title and message cannot be blank")
        return value


GroupContentActionTypes = TopicCreate
KINDS = ["topic_create"]
ROUTES = {"topic_create": "canvas_create_discussion"}


class _Request(CanvasRequest[Any]):
    def __init__(self, path: str, *, query: list | None = None, body: dict | None = None):
        self._path, self._query, self._body = path, query or [], body
        self.method = "POST" if body is not None else "GET"

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self._query

    def json_body(self) -> dict | None:
        return self._body


class _ListRequest(_Request):
    response_is_paginated = True

    def decode(self, data: Any) -> list[dict]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("Canvas did not return the requested resource list")
        return data


def _object(canvas: Canvas, path: str, *, query: list | None = None, body: dict | None = None) -> dict:
    result = dump(canvas.send(_Request(path, query=query, body=body)))
    if not isinstance(result, dict):
        raise ValueError("Canvas did not return the requested resource object")  # noqa: TRY004 - public adapter reports invalid upstream shape
    return result


def _identity(value: dict, key: str, expected: int) -> None:
    if type(value.get(key)) is not int or value[key] != expected:
        raise ValueError(f"Canvas did not confirm the requested {key}")


def _self_id(canvas: Canvas) -> int:
    actor = _object(canvas, "users/self/profile").get("id")
    if type(actor) is not int or not 0 < actor <= 2**63 - 1:
        raise ValueError("Canvas did not confirm the current user's identity")
    return actor


def context_path(context: ContentContext | dict) -> str:
    value = _CONTEXT.validate_python(context)
    return f"courses/{value.course_id}" if isinstance(value, CourseContext) else f"groups/{value.group_id}"


def _group_access(canvas: Canvas, group_id: int, *, actor: int | None = None) -> dict:
    group_id = _ID.validate_python(group_id)
    actor = _self_id(canvas) if actor is None else actor
    group = _object(canvas, f"groups/{group_id}", query=[("include[]", "permissions")])
    _identity(group, "id", group_id)
    # Differentiation tags have group IDs but no collaborative content.
    if group.get("non_collaborative") is True or group.get("workflow_state", "available") not in {"available", "active"}:
        raise ValueError("This group is inactive or has no collaborative content")
    membership = _object(canvas, f"groups/{group_id}/users/self")
    _identity(membership, "user_id", actor)
    _identity(membership, "group_id", group_id)
    if membership.get("workflow_state") != "accepted":
        raise ValueError("Group content requires the current user's accepted membership")
    return {"user_id": actor, "group": group, "membership": membership}


def get_group(canvas: Canvas, group_id: int) -> dict:
    """Read one current member's group and exact self membership."""
    return _group_access(canvas, group_id)


def _page(canvas: Canvas, path: str, offset: int, limit: int, *, query: list | None = None,
          file_metadata: bool = False) -> dict:
    window([], offset, limit)
    iterator = canvas.paginate(_ListRequest(path, query=[*(query or []), ("per_page", limit)]))
    try:
        rows = list(islice(iterator, offset, offset + limit + 1))
    finally:
        iterator.close()
    more = len(rows) > limit
    selected = rows[:limit]
    if file_metadata:
        # Download credentials can occupy opaque URL paths, not just query
        # parameters. Never send the upstream download URL to the normalizer.
        selected = [{key: value for key, value in row.items() if key != "url"} for row in selected]
        for row in selected:
            file_id = row.get("id")
            if type(file_id) is int and 0 < file_id <= 2**63 - 1:
                row["html_url"] = f"{canvas.config.base_url}/files/{file_id}"
                row["read"] = {"tool": "canvas_read_file", "file_id": file_id}
    return envelope(selected, origin=canvas.config.base_url, coverage={
        "scope": "requested_list", "complete": offset == 0 and not more,
        "offset": offset, "returned": min(len(rows), limit),
        "next_offset": offset + limit if more else None,
        "pagination_note": "Offset windows may shift if Canvas changes between reads.",
    })


def _group_page(canvas: Canvas, group_id: int, resource: str, offset: int, limit: int, *, query: list | None = None) -> dict:
    window([], offset, limit)  # reject invalid windows before any network request
    access = _group_access(canvas, group_id)
    result = _page(canvas, f"groups/{group_id}/{resource}", offset, limit, query=query, file_metadata=resource == "files")
    result["coverage"].update(group_id=group_id, current_user_id=access["user_id"], membership_state="accepted")
    return result


def list_group_members(canvas: Canvas, group_id: int, offset: int = 0, limit: int = 30) -> dict:
    """Read the permitted member roster; Canvas can restrict visible sections."""
    result = _group_page(canvas, group_id, "users", offset, limit)
    result["coverage"]["scope"] = "canvas_visible_group_members"
    return result


def list_group_pages(canvas: Canvas, group_id: int, offset: int = 0, limit: int = 30) -> dict:
    return _group_page(canvas, group_id, "pages", offset, limit, query=[("sort", "title"), ("order", "asc")])


def get_group_page(canvas: Canvas, group_id: int, page_url: str) -> dict:
    """Read a slug/ID (page_id:123 is unambiguous); preserve lock metadata."""
    if (type(page_url) is not str or not 1 <= len(page_url) <= 255 or page_url in {".", ".."}
            or any(c in page_url for c in "/\\%?#") or any(ord(c) < 32 for c in page_url)):
        raise ValueError("page_url must be one Canvas page slug or ID")
    explicit_id = None
    if page_url.startswith("page_id:"):
        tail = page_url.removeprefix("page_id:")
        if not tail.isascii() or not tail.isdigit():
            raise ValueError("page_id: requires a positive numeric page ID")
        explicit_id = _ID.validate_python(int(tail))
    _group_access(canvas, group_id)
    page = _object(canvas, f"groups/{group_id}/pages/{quote(page_url, safe='')}")
    if explicit_id is not None:
        _identity(page, "page_id", explicit_id)
    elif page.get("url") != page_url:
        if not page_url.isascii() or not page_url.isdigit():
            raise ValueError("Canvas returned a different page")
        _identity(page, "page_id", int(page_url))
    return page


def list_group_discussions(canvas: Canvas, group_id: int, offset: int = 0, limit: int = 30) -> dict:
    return _group_page(canvas, group_id, "discussion_topics", offset, limit)


def list_group_files(canvas: Canvas, group_id: int, offset: int = 0, limit: int = 30) -> dict:
    return _group_page(canvas, group_id, "files", offset, limit)


def list_group_folders(canvas: Canvas, group_id: int, offset: int = 0, limit: int = 30) -> dict:
    result = _group_page(canvas, group_id, "folders", offset, limit)
    result["coverage"]["scope"] = "flat_group_folder_list"
    return result


def get_group_file(canvas: Canvas, group_id: int, file_id: int) -> dict:
    file_id = _ID.validate_python(file_id)
    _group_access(canvas, group_id)
    result = _object(canvas, f"groups/{group_id}/files/{file_id}")
    _identity(result, "id", file_id)
    return result


def get_group_folder(canvas: Canvas, group_id: int, folder_id: int) -> dict:
    folder_id = _ID.validate_python(folder_id)
    _group_access(canvas, group_id)
    result = _object(canvas, f"groups/{group_id}/folders/{folder_id}")
    _identity(result, "id", folder_id)
    if result.get("context_type") != "Group":
        raise ValueError("Canvas returned a folder outside this group")
    _identity(result, "context_id", group_id)
    return result


def _topic(canvas: Canvas, context: ContentContext, topic_id: int) -> dict:
    topic = _object(canvas, f"{context_path(context)}/discussion_topics/{topic_id}")
    _identity(topic, "id", topic_id)
    key = "group_id" if isinstance(context, GroupContext) else "course_id"
    if key in topic:
        _identity(topic, key, getattr(context, key))
    if "context_id" in topic:
        _identity(topic, "context_id", getattr(context, key))
    if "context_type" in topic and topic["context_type"] not in {context.context_type, context.context_type.title()}:
        raise ValueError("Canvas returned a discussion from a different context")
    return topic


def get_topic(canvas: Canvas, context: ContentContext | dict, topic_id: int) -> dict:
    context, topic_id = _CONTEXT.validate_python(context), _ID.validate_python(topic_id)
    if isinstance(context, GroupContext):
        _group_access(canvas, context.group_id)
    return _topic(canvas, context, topic_id)


def _gate(canvas: Canvas, context: ContentContext, topic_id: int, reason: str, status: str = "forbidden") -> dict:
    return envelope({"context": context.model_dump(), "topic_id": topic_id, "access": reason},
        origin=canvas.config.base_url, status=status, coverage={"scope": "discussion_entries", "complete": False})


def list_entries(canvas: Canvas, context: ContentContext | dict, topic_id: int, entry_id: int | None = None,
                 offset: int = 0, limit: int = 30) -> dict:
    """Read entries/replies with no implicit posting, subscription, or read write."""
    window([], offset, limit)
    context, topic_id = _CONTEXT.validate_python(context), _ID.validate_python(topic_id)
    if entry_id is not None:
        entry_id = _ID.validate_python(entry_id)
    topic = get_topic(canvas, context, topic_id)
    if topic.get("locked_for_user") is True or topic.get("published") is False:
        return _gate(canvas, context, topic_id, "locked_for_user", "locked")
    if topic.get("user_can_see_posts") is False:
        reason = "initial_post_required" if topic.get("require_initial_post") is True else "posts_not_visible"
        return _gate(canvas, context, topic_id, reason)
    path = f"{context_path(context)}/discussion_topics/{topic_id}/entries"
    if entry_id is not None:
        path += f"/{entry_id}/replies"
    try:
        result = _page(canvas, path, offset, limit)
    except CanvasError as error:
        if error.status == 403 and "require_initial_post" in (error.body or ""):
            return _gate(canvas, context, topic_id, "initial_post_required")
        raise
    result["coverage"]["nested_replies"] = (
        "Top-level entries may contain only recent_replies; provide entry_id to read its paginated replies."
        if entry_id is None else "This window covers replies to the requested entry only.")
    return result


def _create_permission(canvas: Canvas, path: str, resource: dict) -> bool:
    permissions = resource.get("permissions")
    value = permissions.get("create_discussion_topic") if isinstance(permissions, dict) else None
    if type(value) is not bool:
        # Both controllers accept rights_status keys, including this documented
        # derived permission. Missing include[] data is not a false permission.
        permissions = _object(canvas, path + "/permissions", query=[("permissions[]", "create_discussion_topic")])
        value = permissions.get("create_discussion_topic")
    if type(value) is not bool:
        raise ValueError("Canvas did not confirm discussion-creation permission; permission is unknown")
    if not value:
        raise ValueError("Canvas does not permit creating a discussion in this context")
    return value


def snapshot(canvas: Canvas, p: dict) -> dict:
    """Capture current student/member identity, audience and actual permission."""
    action = TopicCreate.model_validate(p)
    actor, context = _self_id(canvas), action.context
    path = context_path(context)
    if isinstance(context, GroupContext):
        access = _group_access(canvas, context.group_id, actor=actor)
        resource = access["group"]
        membership = {k: access["membership"].get(k) for k in ("id", "group_id", "user_id", "workflow_state")}
        audience = {k: resource.get(k) for k in ("id", "name", "context_type", "course_id", "account_id", "is_public", "non_collaborative")}
    else:
        resource = _object(canvas, path, query=[("include[]", "permissions")])
        _identity(resource, "id", context.course_id)
        if resource.get("workflow_state") != "available" or resource.get("access_restricted_by_date") is True:
            raise ValueError("The course is not currently available for a student discussion")
        enrollments = resource.get("enrollments")
        own = [e for e in enrollments if isinstance(e, dict) and type(e.get("user_id")) is int
               and e["user_id"] == actor and e.get("type") == "student" and e.get("enrollment_state") == "active"] if isinstance(enrollments, list) else []
        if not own:
            raise ValueError("Discussion creation requires the current user's active student enrollment")
        membership = sorted(({k: e.get(k) for k in ("user_id", "type", "role", "role_id", "enrollment_state")} for e in own),
                            key=lambda row: (str(row.get("role_id")), str(row.get("role"))))
        audience = {k: resource.get(k) for k in ("id", "name", "course_code", "workflow_state", "is_public", "is_public_to_auth_users")}
    return {"user_id": actor, "context": context.model_dump(), "audience": audience, "membership": membership,
            "permissions": {"create_discussion_topic": _create_permission(canvas, path, resource)}}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    """Create once and verify a fresh read by ID, current author and exact text."""
    action = TopicCreate.model_validate(p)
    actor = _ID.validate_python(target.get("user_id"))
    if target.get("context") != action.context.model_dump() or (target.get("permissions") or {}).get("create_discussion_topic") is not True:
        raise ValueError("Prepared discussion context or creation permission does not match")
    # ActionStore re-runs snapshot immediately before dispatch and rejects drift.
    created = _object(canvas, context_path(action.context) + "/discussion_topics",
                      body={"title": action.title, "message": action.message})
    topic_id = _ID.validate_python(created.get("id"))
    saved = _topic(canvas, action.context, topic_id)
    author = saved.get("author")
    if not isinstance(author, dict):
        raise ValueError("Created discussion author was not confirmed")  # noqa: TRY004 - failed readback must remain an uncertain action
    _identity(author, "id", actor)
    if saved.get("title") != action.title or saved.get("message") != action.message:
        raise ValueError("Exact created discussion title and message were not confirmed")
    if saved.get("is_announcement") is True or saved.get("assignment_id") is not None:
        raise ValueError("Canvas returned an announcement or graded discussion instead of the requested topic")
    return {"verified": True, "user_id": actor, "context": action.context.model_dump(), "topic_id": topic_id,
            "title": saved["title"], "message": saved["message"],
            "html_url": f"{canvas.config.base_url}/{context_path(action.context)}/discussion_topics/{topic_id}"}
