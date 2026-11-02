"""Canvas — the public client class.

Composes the internal HTTP dispatcher with namespace objects that expose
endpoints grouped by domain. Users interact with `Canvas` and its namespaces
(.courses, .assignments, .modules, .files); everything underneath is
internal.

Example:

    from canvas_py import Canvas

    with Canvas.from_env() as canvas:
        courses = canvas.courses.list_active()
        for c in courses:
            print(c.display_name)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import httpx

from ._config import CanvasConfig, load_config
from ._errors import CanvasError
from ._http import CanvasHTTP
from .endpoints.activity import GetActivityStreamSummary, ListActivityStream
from .endpoints.announcements import ListAnnouncements
from .endpoints.assignments import GetAssignment, ListAssignments
from .endpoints.conversations import (
    GetConversation,
    ListConversations,
    MarkConversation,
)
from .endpoints.courses import GetCourse, ListCourses
from .endpoints.discussions import (
    GetDiscussionTopic,
    GetDiscussionView,
    ListDiscussionTopics,
    MarkAllEntriesRead,
    MarkEntryRead,
    MarkTopicRead,
    PostDiscussionEntry,
    PostDiscussionReply,
    SubscribeToTopic,
    UnsubscribeFromTopic,
)
from .endpoints.file_uploads import (
    UploadContext,
)
from .endpoints.files import (
    GetFile,
    GetFolder,
    GetUserQuota,
    ListCourseFiles,
    ListFolderFiles,
    ListSubfolders,
    ListUserFiles,
    ListUserFolders,
)
from .endpoints.modules import GetModuleItem, GetModuleItems, GetModuleItemSequence, ListModules
from .endpoints.navigation import GetCourseTabs, GetFrontPage, GetPage, GetQuiz, ListPages, ListQuizzes
from .endpoints.submissions import (
    AddSubmissionComment,
    GetSubmission,
    ListSubmissions,
    MarkSubmissionRead,
    SubmitFileUpload,
    SubmitMediaRecording,
    SubmitTextEntry,
    SubmitUrl,
)
from .models.submission import Submission
from .endpoints.planner import (
    CreatePlannerNote,
    CreatePlannerOverride,
    DeletePlannerNote,
    ListPlannerItems,
    ListPlannerNotes,
    UpdatePlannerNote,
    UpdatePlannerOverride,
)
from .models.activity import ActivityStreamItem
from .models.assignment import Assignment
from .models.conversation import Conversation
from .models.course import Course
from .models.discussion import DiscussionEntry, DiscussionTopic
from .models.file import CanvasFile
from .models.module import Module, ModuleItem
from .models.planner import PlannerItem, PlannerNote
from .models.submission import Submission as _Submission_reexport  # noqa: F401

if TYPE_CHECKING:
    from .tools import CourseCatchup, CrossCourseDigest, MissingWorkAudit

logger = logging.getLogger("canvas_py.client")


class _CoursesAPI:
    """Namespace for courses endpoints."""

    def __init__(self, http: CanvasHTTP):
        self._http = http

    def list_active(
        self,
        *,
        include: list[str] | None = None,
    ) -> list[Course]:
        """All active-enrollment courses for the current user."""
        req = ListCourses(enrollment_state="active", include=include)
        return self._http.exhaust(req)

    def list_archived(
        self,
        *,
        include: list[str] | None = None,
    ) -> list[Course]:
        """Past-semester courses — where the user's enrollment is completed.

        Canvas has two orthogonal state axes: course `workflow_state` and
        per-user `enrollment_state`. Past courses almost always stay
        `workflow_state=available` (the course still exists) but move to
        `enrollment_state=completed` (the student is no longer enrolled).
        This helper queries the latter, which is the correct axis for
        "semesters I've finished."

        Every downstream endpoint (assignments, modules, files) works the
        same against these course IDs as against active ones.
        """
        req = ListCourses(
            enrollment_state="completed",
            include=include,
        )
        return self._http.exhaust(req)

    def list_all(
        self,
        *,
        include: list[str] | None = None,
    ) -> list[Course]:
        """Full enrollment history — active plus completed.

        Concatenates `list_active()` and `list_archived()` calls (two queries,
        distinct enrollment_state axes — Canvas returns different rows for each).
        Useful for tutoring/study use cases where past-semester content is
        needed (e.g. all German classes across multiple terms).
        """
        active = self.list_active(include=include)
        archived = self.list_archived(include=include)
        # Preserve order; dedup by course id in case Canvas surfaces one
        # in both lists (it shouldn't, but cheap safety).
        seen: set[int] = set()
        out: list[Course] = []
        for c in active + archived:
            if c.id in seen:
                continue
            seen.add(c.id)
            out.append(c)
        return out

    def list(
        self,
        *,
        enrollment_state: str | None = "active",
        enrollment_type: str | None = None,
        states: list[str] | None = None,
        include: list[str] | None = None,
    ) -> list[Course]:
        """Arbitrary course list with custom filters.

        Pass `states=["available", "completed"]` to include archived.
        """
        req = ListCourses(
            enrollment_state=enrollment_state,
            enrollment_type=enrollment_type,
            states=states,
            include=include,
        )
        return self._http.exhaust(req)

    def get(self, course_id: int, *, include: list[str] | None = None) -> Course:
        return self._http.send(GetCourse(course_id, include=include))

    def tabs(self, course_id: int) -> list[dict[str, Any]]:
        """The server's actual course navigation tabs, including visibility."""
        return self._http.exhaust(GetCourseTabs(course_id))


class _AssignmentsAPI:
    """Namespace for assignments endpoints."""

    def __init__(self, http: CanvasHTTP):
        self._http = http

    def list(
        self,
        course_id: int,
        *,
        include: list[str] | None = None,
        bucket: str | None = None,
        search_term: str | None = None,
        order_by: str | None = None,
    ) -> list[Assignment]:
        req = ListAssignments(
            course_id,
            include=include,
            bucket=bucket,
            search_term=search_term,
            order_by=order_by,
        )
        return self._http.exhaust(req)

    def get(
        self,
        course_id: int,
        assignment_id: int,
        *,
        include: list[str] | None = None,
    ) -> Assignment:
        return self._http.send(GetAssignment(course_id, assignment_id, include=include))


class _ModulesAPI:
    """Namespace for modules endpoints. Preferred discovery surface."""

    def __init__(self, http: CanvasHTTP):
        self._http = http

    def list(
        self,
        course_id: int,
        *,
        include: list[str] | None = None,
        search_term: str | None = None,
    ) -> list[Module]:
        """List modules with items + content_details included by default.

        This is the most reliable "what's in this course" query — see
        research/05-real-world-quirks.md §5.
        """
        req = ListModules(course_id, include=include, search_term=search_term)
        modules = self._http.exhaust(req)
        if "items" in req.include:
            for module in modules:
                if module.items is None or (module.items_count is not None and len(module.items) < module.items_count):
                    module.items = self.items(course_id, module.id)
        return modules

    def items(self, course_id: int, module_id: int) -> list[ModuleItem]:
        return self._http.exhaust(GetModuleItems(course_id, module_id))

    def get_item(self, course_id: int, module_id: int, item_id: int) -> ModuleItem:
        return self._http.send(GetModuleItem(course_id, module_id, item_id))

    def sequence(self, course_id: int, asset_type: str, asset_id: str) -> dict[str, Any]:
        return self._http.send(GetModuleItemSequence(course_id, asset_type, asset_id))


class _PagesAPI:
    def __init__(self, http: CanvasHTTP):
        self._http = http

    def list(self, course_id: int) -> list[dict[str, Any]]:
        return self._http.exhaust(ListPages(course_id))

    def get(self, course_id: int, page_url: str) -> dict[str, Any]:
        return self._http.send(GetPage(course_id, page_url))

    def front_page(self, course_id: int) -> dict[str, Any]:
        return self._http.send(GetFrontPage(course_id))


class _QuizzesAPI:
    """Quiz metadata only; never starts an attempt or retrieves questions."""

    def __init__(self, http: CanvasHTTP):
        self._http = http

    def list(self, course_id: int) -> list[dict[str, Any]]:
        return self._http.exhaust(ListQuizzes(course_id))

    def get(self, course_id: int, quiz_id: int) -> dict[str, Any]:
        return self._http.send(GetQuiz(course_id, quiz_id))


class _FilesAPI:
    """Namespace for files endpoints.

    `get(file_id)` is the reliable workhorse. `list_course_files` often 403s
    for students — see research/05-real-world-quirks.md.
    """

    def __init__(self, http: CanvasHTTP, client: httpx.Client):
        self._http = http
        self._client = client

    def get(self, file_id: int, *, include: list[str] | None = None) -> CanvasFile:
        """Fetch a single file's metadata. Reliable across permission levels."""
        return self._http.send(GetFile(file_id, include=include))

    def list_course_files(
        self,
        course_id: int,
        *,
        content_types: list[str] | None = None,
        search_term: str | None = None,
    ) -> list[CanvasFile]:
        """Enumerate course files. WARNING: often 403s for students."""
        req = ListCourseFiles(
            course_id,
            content_types=content_types,
            search_term=search_term,
        )
        return self._http.exhaust(req)

    def list_user_files(self, *, search_term: str | None = None, content_types: list[str] | None = None) -> list[CanvasFile]:
        """Personal Canvas files, including readable submission attachments."""
        return self._http.exhaust(ListUserFiles(search_term=search_term, content_types=content_types))

    def list_folder_files(self, folder_id: int, *, search_term: str | None = None, content_types: list[str] | None = None) -> list[CanvasFile]:
        """Files directly inside a Canvas folder, honoring Canvas permissions."""
        return self._http.exhaust(ListFolderFiles(folder_id, search_term=search_term, content_types=content_types))

    def list_user_folders(self) -> list[dict[str, Any]]:
        """Flat list of all personal folders, retaining parent/lock metadata."""
        return self._http.exhaust(ListUserFolders())

    def list_subfolders(self, folder_id: int) -> list[dict[str, Any]]:
        return self._http.exhaust(ListSubfolders(folder_id))

    def get_folder(self, folder_id: int) -> dict[str, Any]:
        return self._http.send(GetFolder(folder_id))

    def user_quota(self) -> dict[str, int]:
        """Total/used personal-file storage in bytes; errors remain errors."""
        return self._http.send(GetUserQuota())

    def download(self, file: CanvasFile | str, destination: Path | str, *, max_bytes: int | None = None) -> Path:
        """Download a file to disk.

        Args:
            file: either a CanvasFile (we use its `url` field) or a direct URL.
            destination: file path to write to.

        Returns:
            The destination path (Path).
        """
        if isinstance(file, CanvasFile):
            url = file.url
            if not url:
                raise ValueError(f"file {file.id} has no download url")
        else:
            url = file

        return self._http.download(url, destination, max_bytes=max_bytes)

    def upload(
        self,
        path: Path | str,
        *,
        context: UploadContext,
        name: str | None = None,
        content_type: str | None = None,
        on_duplicate: str = "rename",
        parent_folder_path: str | None = None,
        parent_folder_id: int | None = None,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        verify: bool = True,
        timeout: float = 120.0,
    ) -> CanvasFile:
        """Upload exact local bytes, finalize, and re-read Canvas file metadata.

        ``context`` selects user files or assignment attachment storage. An
        assignment-context upload does not submit the assignment. Duplicate
        names are renamed unless overwrite was explicitly requested.

        Bind a reviewed file with ``expected_size`` and ``expected_sha256``.
        Upload uses a private snapshot so edits after validation cannot change
        the sent bytes. ``verify=True`` checks the resulting file ID and size
        through a fresh metadata read. This verifies metadata, not a remote
        content checksum (Canvas's File API does not expose one).

        Errors after sending bytes may have a ``file_id`` for inspection.
        Never automatically repeat an upload with an uncertain outcome.
        """
        from ._uploads import upload_file

        return upload_file(
            self._http, path, context=context, name=name, content_type=content_type,
            on_duplicate=on_duplicate, parent_folder_path=parent_folder_path,
            parent_folder_id=parent_folder_id, expected_size=expected_size,
            expected_sha256=expected_sha256, verify=verify, timeout=timeout,
        )


class _DiscussionsAPI:
    """Namespace for discussion topic endpoints.

    Covers both reads (list/get/view) and writes (post/reply/mark-read/
    subscribe). Announcements share this API — pass `only_announcements=True`
    to `list()` to filter. See research/04-content-and-communication.md §1.
    """

    def __init__(self, http: CanvasHTTP):
        self._http = http

    def list(
        self,
        course_id: int,
        *,
        only_announcements: bool = False,
        order_by: str | None = None,
        search_term: str | None = None,
    ) -> list[DiscussionTopic]:
        req = ListDiscussionTopics(
            course_id,
            only_announcements=only_announcements,
            order_by=order_by,
            search_term=search_term,
        )
        return self._http.exhaust(req)

    def get(self, course_id: int, topic_id: int) -> DiscussionTopic:
        return self._http.send(GetDiscussionTopic(course_id, topic_id))

    def get_view(self, course_id: int, topic_id: int) -> dict[str, Any]:
        """Full server-cached thread tree. Returns raw dict with keys
        `participants`, `unread_entries`, `entry_ratings`, `view`.

        Note: 403 here typically means `require_initial_post` is set and the
        caller hasn't posted yet.
        """
        return self._http.send(GetDiscussionView(course_id, topic_id))

    def post(
        self,
        course_id: int,
        topic_id: int,
        message: str,
        *,
        attachment_file_ids: list[int] | None = None,
    ) -> DiscussionEntry:
        """Post a new top-level entry to the topic."""
        req = PostDiscussionEntry(
            course_id,
            topic_id,
            message,
            attachment_file_ids=attachment_file_ids,
        )
        return self._http.send(req)

    def reply(
        self,
        course_id: int,
        topic_id: int,
        entry_id: int,
        message: str,
    ) -> DiscussionEntry:
        """Reply to an existing entry inside the topic."""
        return self._http.send(
            PostDiscussionReply(course_id, topic_id, entry_id, message)
        )

    def mark_read(
        self,
        course_id: int,
        topic_id: int,
        entry_id: int | None = None,
    ) -> None:
        """Mark either a single entry or the topic's intro as read.

        If `entry_id` is None, the topic-read endpoint is used (which only
        marks the root message, not replies). For "mark every entry read,"
        use `mark_all_read`.
        """
        if entry_id is None:
            req: Any = MarkTopicRead(course_id, topic_id)
        else:
            req = MarkEntryRead(course_id, topic_id, entry_id)
        self._http.send(req)

    def mark_all_read(self, course_id: int, topic_id: int) -> None:
        """Mark every entry in the topic as read."""
        self._http.send(MarkAllEntriesRead(course_id, topic_id))

    def subscribe(self, course_id: int, topic_id: int) -> None:
        self._http.send(SubscribeToTopic(course_id, topic_id))

    def unsubscribe(self, course_id: int, topic_id: int) -> None:
        self._http.send(UnsubscribeFromTopic(course_id, topic_id))


class _PlannerAPI:
    """Namespace for Canvas planner endpoints.

    The planner is Canvas's best "what's on my plate" surface — one call
    covers assignments, quizzes, discussions, announcements, calendar events,
    and user notes with submission state pre-computed. See
    research/04-content-and-communication.md §7.
    """

    def __init__(self, http: CanvasHTTP):
        self._http = http

    def items(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        context_codes: list[str] | None = None,
        filter: str | None = None,
    ) -> list[PlannerItem]:
        """The unified planner feed.

        Args mirror `ListPlannerItems`. Pass `filter="new_activity"` to
        restrict to items with fresh updates.
        """
        req = ListPlannerItems(
            start_date=start_date,
            end_date=end_date,
            context_codes=context_codes,
            filter=filter,
        )
        return self._http.exhaust(req)

    def items_new_activity(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        context_codes: list[str] | None = None,
    ) -> list[PlannerItem]:
        """Convenience for `items(..., filter="new_activity")` — "what's new."""
        return self.items(
            start_date=start_date,
            end_date=end_date,
            context_codes=context_codes,
            filter="new_activity",
        )

    def notes(self) -> list[PlannerNote]:
        """List all of the current user's planner notes."""
        return self._http.exhaust(ListPlannerNotes())

    def create_note(
        self,
        title: str,
        *,
        details: str | None = None,
        todo_date: str | None = None,
        course_id: int | None = None,
        linked_object_type: str | None = None,
        linked_object_id: int | None = None,
    ) -> PlannerNote:
        """Create a new personal reminder."""
        req = CreatePlannerNote(
            title,
            details=details,
            todo_date=todo_date,
            course_id=course_id,
            linked_object_type=linked_object_type,
            linked_object_id=linked_object_id,
        )
        return self._http.send(req)

    def update_note(self, note_id: int, **fields: Any) -> PlannerNote:
        """Update an existing note. Only pass fields you want to change."""
        return self._http.send(UpdatePlannerNote(note_id, **fields))

    def delete_note(self, note_id: int) -> PlannerNote:
        """Soft-delete a planner note."""
        return self._http.send(DeletePlannerNote(note_id))

    def create_override(
        self,
        plannable_type: str,
        plannable_id: int,
        *,
        marked_complete: bool = False,
        dismissed: bool = False,
    ) -> dict[str, Any]:
        """Create a planner override — mark complete or dismiss an item."""
        req = CreatePlannerOverride(
            plannable_type,
            plannable_id,
            marked_complete=marked_complete,
            dismissed=dismissed,
        )
        return self._http.send(req)

    def update_override(
        self,
        override_id: int,
        *,
        marked_complete: bool | None = None,
        dismissed: bool | None = None,
    ) -> dict[str, Any]:
        """Flip marked_complete / dismissed on an existing override."""
        req = UpdatePlannerOverride(
            override_id,
            marked_complete=marked_complete,
            dismissed=dismissed,
        )
        return self._http.send(req)


class _AnnouncementsAPI:
    """Namespace for the cross-context announcements endpoint.

    The `/announcements` endpoint returns DiscussionTopic objects (with an
    added `context_code` field) where `is_announcement=true`. See
    research/04-content-and-communication.md §6 and api-docs/announcements.md.
    """

    def __init__(self, http: CanvasHTTP):
        self._http = http

    def list(
        self,
        context_codes: list[str],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        active_only: bool | None = None,
    ) -> list[DiscussionTopic]:
        """List announcements across the given context codes.

        `context_codes` is required — Canvas rejects the endpoint without it.
        """
        req = ListAnnouncements(
            context_codes,
            start_date=start_date,
            end_date=end_date,
            active_only=active_only,
        )
        return self._http.exhaust(req)

    def list_across(
        self,
        course_ids: list[int],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        active_only: bool | None = None,
    ) -> list[DiscussionTopic]:
        """List announcements across the given course IDs.

        Convenience wrapper that builds `context_codes=["course_<id>", ...]`
        from a plain list of course IDs — the common case when the caller
        already has Course objects handy.
        """
        codes = [f"course_{cid}" for cid in course_ids]
        return self.list(
            codes,
            start_date=start_date,
            end_date=end_date,
            active_only=active_only,
        )


class _ActivityAPI:
    """Namespace for the activity stream (Canvas notifications feed)."""

    def __init__(self, http: CanvasHTTP):
        self._http = http

    def stream(self, *, only_active_courses: bool = False) -> list[ActivityStreamItem]:
        """Full activity stream as typed items."""
        req = ListActivityStream(only_active_courses=only_active_courses)
        return self._http.exhaust(req)

    def summary(self) -> list[dict[str, Any]]:
        """Aggregated counts by type — cheap badge/unread data.

        Each entry looks like::

            {"type": "DiscussionTopic", "unread_count": 2, "count": 7,
             "notification_category": null}
        """
        return self._http.send(GetActivityStreamSummary())


class _SubmissionsAPI:
    """Namespace for assignment submissions — reads + all write flavors.

    `get()` includes submission_comments by default because some teachers
    post grading feedback primarily in comments rather than score/grade
    (the Rettberg pattern); hiding comments would lose primary feedback.

    `submit_files()` orchestrates the full flow: upload each local file via
    UploadContext.assignment_submission, then POST the submission with
    the returned file_ids.
    """

    def __init__(self, http: CanvasHTTP, files_api: _FilesAPI):
        self._http = http
        self._files = files_api

    def get(
        self,
        course_id: int,
        assignment_id: int,
        user_id: int | str = "self",
        *,
        include: list[str] | None = None,
    ) -> Submission:
        return self._http.send(
            GetSubmission(course_id, assignment_id, user_id, include=include)
        )

    def list(
        self,
        course_id: int,
        assignment_id: int,
        *,
        include: list[str] | None = None,
    ) -> list[Submission]:
        return self._http.exhaust(
            ListSubmissions(course_id, assignment_id, include=include)
        )

    def submit_text(
        self,
        course_id: int,
        assignment_id: int,
        body: str,
        *,
        comment: str | None = None,
    ) -> Submission:
        """Submit text content as online_text_entry."""
        return self._http.send(SubmitTextEntry(course_id, assignment_id, body, comment=comment))

    def submit_url(
        self,
        course_id: int,
        assignment_id: int,
        url: str,
        *,
        comment: str | None = None,
    ) -> Submission:
        """Submit a URL as online_url."""
        return self._http.send(SubmitUrl(course_id, assignment_id, url, comment=comment))

    def submit_files(
        self,
        course_id: int,
        assignment_id: int,
        paths: list[Path | str],
        *,
        comment: str | None = None,
    ) -> Submission:
        """Upload files then submit them as online_upload.

        Full flow:
            1. For each local path, upload via UploadContext.assignment_submission
               (the 3-step Canvas upload flow against the submission-files endpoint).
            2. POST the submission with the collected file_ids.
        """
        if not paths:
            raise ValueError("paths cannot be empty")
        context = UploadContext.assignment_submission(course_id, assignment_id)
        file_ids: list[int] = []
        for p in paths:
            uploaded = self._files.upload(p, context=context)
            if uploaded.id is None:
                raise CanvasError(f"upload of {p} returned no file id")
            file_ids.append(uploaded.id)
        return self._http.send(
            SubmitFileUpload(course_id, assignment_id, file_ids, comment=comment)
        )

    def submit_media(
        self,
        course_id: int,
        assignment_id: int,
        media_comment_id: str,
        media_comment_type: str,
        *,
        comment: str | None = None,
    ) -> Submission:
        """Submit a media_recording. Caller must have obtained media_comment_id
        from Kaltura separately — Kaltura upload flow is not yet implemented."""
        return self._http.send(
            SubmitMediaRecording(
                course_id, assignment_id, media_comment_id, media_comment_type,
                comment=comment,
            )
        )

    def add_comment(
        self,
        course_id: int,
        assignment_id: int,
        text: str,
        *,
        user_id: int | str = "self",
        attachment_file_ids: list[int] | None = None,
    ) -> Submission:
        """Add a comment to a submission. Attachments must be uploaded first
        using UploadContext.submission_comment."""
        return self._http.send(
            AddSubmissionComment(
                course_id, assignment_id, text,
                user_id=user_id,
                file_ids=attachment_file_ids,
            )
        )

    def mark_read(
        self,
        course_id: int,
        assignment_id: int,
        user_id: int | str = "self",
    ) -> None:
        self._http.send(MarkSubmissionRead(course_id, assignment_id, user_id))


class _ToolsAPI:
    """Namespace for Tier-3 intent-bundle tools.

    These compose multiple primitive endpoint calls into rich digests. Each
    tool returns a typed Pydantic model with a `summary` string suitable for
    agent consumption or direct output. See canvas_py.tools for details.
    """

    def __init__(self, canvas: "Canvas"):
        self._canvas = canvas

    def whats_new(
        self,
        *,
        lookback_days: int = 14,
        lookahead_days: int = 7,
    ) -> "CrossCourseDigest":
        """Cross-course 'what is new since last check' digest.

        Aggregates activity stream, inbox unread, planner, announcements,
        recent feedback, and missing work into one typed return. Template
        summary in `.summary`.
        """
        from .tools import whats_new as _whats_new  # lazy import to avoid cycle
        return _whats_new(
            self._canvas,
            lookback_days=lookback_days,
            lookahead_days=lookahead_days,
        )

    def catch_me_up_on_course(
        self,
        course_id: int,
        *,
        lookback_days: int = 14,
        lookahead_days: int = 7,
    ) -> "CourseCatchup":
        """Rich per-course digest. See tools._digests.catch_me_up_on_course."""
        from .tools import catch_me_up_on_course as _catchup
        return _catchup(
            self._canvas,
            course_id,
            lookback_days=lookback_days,
            lookahead_days=lookahead_days,
        )

    def missing_work_audit(self) -> "MissingWorkAudit":
        """Cross-course missing/late work audit."""
        from .tools import missing_work_audit as _audit
        return _audit(self._canvas)


class _InboxAPI:
    """Namespace for Canvas inbox / conversations (reads only for safety).

    Writes (compose, reply) are intentionally NOT exposed here — sending
    messages impersonating the account holder is an action that should
    require explicit authorization beyond "we have the token." Add later
    if/when a human-confirm flow is in place.
    """

    def __init__(self, http: CanvasHTTP):
        self._http = http

    def list(
        self,
        *,
        scope: str | None = None,
        filter_contexts: list[str] | None = None,
        include_participant_avatars: bool = False,
    ) -> list[Conversation]:
        req = ListConversations(
            scope=scope,
            filter_contexts=filter_contexts,
            include_participant_avatars=include_participant_avatars,
        )
        return self._http.exhaust(req)

    def unread(self) -> list[Conversation]:
        """Shortcut for `list(scope='unread')`."""
        return self.list(scope="unread")

    def get(self, conversation_id: int, *, auto_mark_as_read: bool = False) -> Conversation:
        return self._http.send(
            GetConversation(conversation_id, auto_mark_as_read=auto_mark_as_read)
        )

    def mark_read(self, conversation_id: int) -> Conversation:
        return self._http.send(MarkConversation(conversation_id, workflow_state="read"))

    def mark_unread(self, conversation_id: int) -> Conversation:
        return self._http.send(MarkConversation(conversation_id, workflow_state="unread"))

    def archive(self, conversation_id: int) -> Conversation:
        return self._http.send(MarkConversation(conversation_id, workflow_state="archived"))

    def star(self, conversation_id: int) -> Conversation:
        return self._http.send(MarkConversation(conversation_id, starred=True))

    def unstar(self, conversation_id: int) -> Conversation:
        return self._http.send(MarkConversation(conversation_id, starred=False))


class Canvas:
    """Top-level Canvas client.

    Prefer `Canvas.from_env()` to construct; it handles .env loading and
    config resolution. Use as a context manager to ensure the HTTP session
    is closed.
    """

    def __init__(
        self,
        config: CanvasConfig,
        *,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ):
        self._config = config
        self._http = CanvasHTTP(config, timeout=timeout, client=http_client)

        self.courses = _CoursesAPI(self._http)
        self.assignments = _AssignmentsAPI(self._http)
        self.modules = _ModulesAPI(self._http)
        self.pages = _PagesAPI(self._http)
        self.quizzes = _QuizzesAPI(self._http)
        self.files = _FilesAPI(self._http, self._http._client)  # shares session
        self.discussions = _DiscussionsAPI(self._http)
        self.planner = _PlannerAPI(self._http)
        self.announcements = _AnnouncementsAPI(self._http)
        self.activity = _ActivityAPI(self._http)
        self.submissions = _SubmissionsAPI(self._http, self.files)
        self.inbox = _InboxAPI(self._http)
        self.tools = _ToolsAPI(self)
        from .navigation import CanvasNavigation
        self.navigation = CanvasNavigation(self)

    @classmethod
    def from_env(
        cls,
        *,
        env_path: str | Path | None = None,
        timeout: float = 30.0,
    ) -> Canvas:
        config = load_config(env_path=env_path)
        return cls(config, timeout=timeout)

    @property
    def config(self) -> CanvasConfig:
        return self._config

    def close(self) -> None:
        self.navigation.clear_cache()
        self._http.close()

    def __enter__(self) -> Canvas:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- escape hatches for custom request dispatch ---

    def send(self, request: Any) -> Any:
        """Dispatch a raw CanvasRequest. Use for endpoints not yet wrapped."""
        return self._http.send(request)

    def paginate(self, request: Any) -> Iterator[Any]:
        """Paginate a raw CanvasRequest."""
        return self._http.paginate(request)
