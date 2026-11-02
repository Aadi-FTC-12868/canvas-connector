"""Personal file navigation and private, local course workspace synchronization."""

from __future__ import annotations

from collections.abc import Callable
from itertools import islice
from typing import Any, Literal

from canvas_py import Canvas
from canvas_py.endpoints.files import (
    ListFolderFiles,
    ListSubfolders,
    ListUserFiles,
    ListUserFolders,
)
from canvas_py.sync import SyncLimits, run_sync

from .actions import actions_enabled
from .transport import _value


def register_workspace_tools(mcp: Any, provider: Callable[[], Canvas]) -> None:
    @mcp.tool()
    def canvas_capabilities() -> dict[str, Any]:
        """Describe this deployment's actual read, write, and local workspace capabilities.

        Account permissions and assignment restrictions still apply. This is
        deployment capability, not proof that a particular action succeeded.
        """
        local = _value("CANVAS_MCP_TRANSPORT", "stdio").lower() == "stdio"
        return {
            "navigation": [
                "dashboard",
                "course_home",
                "modules",
                "assignments",
                "pages",
                "announcements",
                "files",
                "submission_feedback",
                "inbox",
            ],
            "documents": "Call canvas_document_capabilities for formats and OCR availability",
            "file_actions_enabled": actions_enabled(),
            "file_actions": [
                "prepare_upload",
                "prepare_submission",
                "execute_after_conversation_authorization",
                "cancel_prepared_action",
            ],
            "upload_limit_bytes": (25 if local else 8) * 1024 * 1024,
            "uploads_are_separate_from_submissions": True,
            "confirmation": "The calling AI obtains explicit user authorization for exact files, target and upload versus submission in conversation, and carries that scope when delegating. No connector form is required; the server does not attest consent. Client/platform approvals remain applicable.",
            "confirmation_diagnostic": "canvas_confirmation_capabilities reports this conversation authorization policy and deployment action setting without prompting or contacting Canvas.",
            "action_receipts": "Process-local; after restart or an uncertain outcome, inspect Canvas before another write",
            "local_course_sync": local,
            "automation": "Read-only Canvas sync is available locally; scheduling is separate. A scan or schedule is not authorization to upload or submit.",
            "unsupported_actions": [
                "discussion_posts",
                "inbox_sends",
                "quiz_attempts",
                "group_submissions",
                "file_deletion",
            ],
            "permissions": "A student token has the same account restrictions; locked or external/LTI content is reported explicitly.",
        }

    @mcp.tool()
    def canvas_browse_files(
        kind: Literal["files", "folders"] = "files",
        folder_id: int | None = None,
        search_term: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Browse personal Canvas files or folders, including submission folders.

        Omit folder_id for personal files/all folders; provide it for immediate
        contents. Follow parent_folder_id to navigate. Read a file using
        canvas_read_file; download_file saves the original in local stdio.
        Offset windows are fresh reads and may shift if files change meanwhile.
        """
        if not 0 <= offset <= 5000 or not 1 <= limit <= 100:
            raise ValueError("offset must be 0..5000 and limit 1..100")
        canvas = provider()
        if kind == "folders":
            if search_term:
                raise ValueError("Folder search is not supported; browse folders instead")
            request = ListUserFolders() if folder_id is None else ListSubfolders(folder_id)
        else:
            request = (
                ListUserFiles(search_term=search_term)
                if folder_id is None
                else ListFolderFiles(folder_id, search_term=search_term)
            )
        iterator = canvas.paginate(request)
        try:
            found = list(islice(iterator, offset, offset + limit + 1))
        finally:
            iterator.close()
        allowed = {
            "id",
            "display_name",
            "filename",
            "size",
            "content-type",
            "content_type",
            "updated_at",
            "modified_at",
            "locked_for_user",
            "hidden_for_user",
            "folder_id",
            "name",
            "full_name",
            "parent_folder_id",
            "files_count",
            "folders_count",
        }
        items = []
        for item in found[:limit]:
            raw = (
                item.model_dump(mode="json", by_alias=True)
                if hasattr(item, "model_dump")
                else dict(item)
            )
            value = {key: entry for key, entry in raw.items() if key in allowed}
            if kind == "files":
                value["canvas_url"] = f"{canvas.config.base_url}/files/{value['id']}"
                value["read_tool"] = "canvas_read_file"
            items.append(value)
        return {
            "kind": kind,
            "folder_id": folder_id,
            "items": items,
            "offset": offset,
            "next_offset": offset + limit if len(found) > limit else None,
            "has_more": len(found) > limit,
            "cache": "fresh",
        }

    @mcp.tool()
    def canvas_sync_courses(
        output_root: str,
        course_ids: list[int] | None = None,
        all_active: bool = False,
        apply: bool = False,
        max_files: int = 100,
        max_operations: int = 250,
        max_total_mb: int = 250,
        max_file_mb: int = 25,
    ) -> dict[str, Any]:
        """Plan or update separate per-class folders; never write to Canvas.

        LOCAL STDIO ONLY. output_root must be an absolute private coursework
        directory outside connector source. Default apply=false discovers changes
        without local writes. apply=true preserves original bytes, versioned
        hashes, extraction previews, manifests and attention items in marked
        course-ID/canvas-managed folders. Existing drafts are never overwritten.
        Check partial coverage and issues; failed sources do not mean no work.
        No schedule is created. Remote cloud servers cannot write to your Mac.
        """
        if _value("CANVAS_MCP_TRANSPORT", "stdio").lower() != "stdio":
            raise ValueError("Local course folders require the stdio connector or local sync CLI")
        if (
            not 1 <= max_files <= 500
            or not 1 <= max_operations <= 2000
            or not 1 <= max_total_mb <= 1000
            or not 1 <= max_file_mb <= 100
        ):
            raise ValueError(
                "Sync bounds: 1..500 files, 1..2000 operations, 1..1000 MiB total, 1..100 MiB per file"
            )
        return run_sync(
            provider(),
            output_root,
            course_ids=course_ids,
            all_active=all_active,
            apply=apply,
            limits=SyncLimits(
                max_files=max_files,
                max_operations=max_operations,
                max_file_bytes=max_file_mb * 1024 * 1024,
                max_total_bytes=max_total_mb * 1024 * 1024,
            ),
        )
