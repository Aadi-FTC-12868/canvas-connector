# Canvas capabilities and boundaries

The connector gives an assistant a guided view of the Canvas resources the signed-in student can access. Its 71 registered MCP tools and 69 prepared action kinds are the contract for this release. Bundled library endpoints are not automatically public tools, and this release does not claim complete Canvas app parity.

For the full student-workflow acceptance ledger and remaining gaps, see [APP-PARITY.md](APP-PARITY.md). For tests actually completed, see [VALIDATION.md](VALIDATION.md). A row below describes supported implementation behavior, not proof that every institution enables it.

## Reading and navigation

| Student workflow | Connector behavior | Important boundary |
| --- | --- | --- |
| Course overview | Dashboard, course home, tabs, and course sections | A hidden tab or failed endpoint does not prove the course has no content. |
| Modules and pages | Follow module items, pages, and returned previous/next relationships | Prerequisites, lock dates, and user permissions still apply. Exact item reads use bounded collection lookup to avoid Horizon SHOW marking progress. Page bodies use access-checked latest saved HTML, explicitly partial; unsupported rendering or access requires Canvas. |
| Assignments | Read assignment details, allowed submission types, dates, attachments, and links | The description can be only one part of the instructions. Follow linked material. |
| Grades and feedback | Read the student's submission status, score/grade, comments, and returned rubric data | A previous grade may not describe the latest attempt. This is not an instructor gradebook editor. |
| Syllabus and course policies | Follow the syllabus and ordinary course content relationships | Materials may be placed in modules, files, pages, or external services rather than a tab named Syllabus. |
| Announcements and discussions | Read available topics, entries, and replies | Initial-post requirements and hidden/locked replies remain enforced by Canvas. |
| Inbox | List conversations and read threads | Reading should not silently mark a conversation read. Explicit inbox-state changes are writes. |
| Planner and calendar | Read available planner items, notes, activity, and calendar information | Planner completion is not assignment submission. Paper and verbal obligations may be absent. |
| Files and folders | Browse/search/filter files, inspect folders/quota, and extract supported document text | Extraction can be partial; inspect coverage and continue when required. |
| People and groups | Discover course users and own groups; read group members, pages, discussions, files and folders | Access is limited by enrollment, institutional privacy controls, and token scopes. |
| Course invitations | List own student invitations, including future-course invitations and previously declined records | Accept/decline uses a separate prepared action; course dates still govern access. |
| What-if Grades | Read saved hypothetical scores independently of recorded grades | Numeric-grade policy applies; reads do not calculate course totals. |
| Bookmarks | List or open own saved bookmarks | Destinations remain links; saving a link does not grant access to its provider. |
| Mastery Paths | Read the current student's offered assignment sets and selection state | Initial selection is separate from assignment release and coursework completion. |
| Group membership options | Discover visible course groups and exact self-membership options | Invitation response, leadership changes and exclusive-category switches can require Canvas. |
| Feedback markers | Read own submission, rubric and annotation markers without changing them | General submission state uses one fixed GraphQL read query; this needs a broader optional upstream scope. |
| Preferences | Read existing notification channels/frequencies, favorites, nicknames and colors | Delivery infrastructure, channel creation and card ordering remain separate. |

Page reads, including course home and module-linked pages, avoid page SHOW and body-including lists because those reads can mark module progress. Metadata-only listing and latest saved HTML are matched and rechecked for identity, publication, locks and revision freshness. `content_source=latest_saved_html_revision`, `content_complete=false` and the warning survive nested tool results. Known Horizon/block content, unavailable Pages listing, missing revisions or unknown access produce an unavailable result and Canvas handoff. An empty saved HTML string is not proof that Canvas displays an empty page.

## Documents and external links

PDF, DOCX, PPTX, HTML, CSV, and text extraction reuse the maintained document reader. Legacy Office formats require conversion. Optional OCR depends on Poppler/Tesseract and is bounded; extraction is not a guarantee that images, handwriting, equations, or page layout were understood. Follow the reader's warnings and continuation cursors.

External links identify their provider and an appropriate next step. The connector does not acquire Google or Microsoft access from a Canvas token. The assistant should use an already-connected service, request the appropriate connection or school-account access when needed, and use an authorized browser or user-provided content as a fallback. See the [companion skill](../skills/canvas-assistant/SKILL.md).

## Prepared actions

Writes require the connector's write permission plus the user's authorization for the exact action. The preparation step provides a target and payload for review. Preparation itself does not submit or send anything.

| Execution tool | Supported scope |
| --- | --- |
| `canvas_upload_file` | Upload prepared inline file content, capped at 8 MiB per file; personal uploads can select a verified writable personal folder. |
| `canvas_submit_assignment` | Submit text, a URL, or files from verified upload receipts, when the assignment accepts that type. Group submissions require review in Canvas. |
| `canvas_add_submission_comment` | Add a text comment with optional files from verified comment-upload receipts bound to that assignment. Uploading alone does not send the comment. |
| `canvas_post_discussion` | Post an entry or reply to an existing course discussion entry. |
| `canvas_manage_discussion` | Edit/delete your own course discussion text entry, change subscription or read state, or rate an entry. Attached-entry edits are refused; use Canvas to preserve attachments. |
| `canvas_manage_submission_comments` | Edit/delete your own submission comment. |
| `canvas_manage_calendar` | Create/edit/delete your personal calendar event. Course/group, appointment, and recurring events require Canvas; create/update supports timed events. |
| `canvas_select_mastery_path` | Select an initially offered assignment set for an exact module item. Existing selections are read-only in this adapter. Selection, assignment release and coursework completion remain distinct. |
| `canvas_manage_group_membership` | Join/request membership, leave an ordinary course group, or cancel an own pending request. No implicit exclusive-category switching or leadership reassignment. |
| `canvas_post_discussion_attachment` | Post/reply in a course or group with one prepared attachment up to 8 MiB; verify entry ownership, association and downloaded bytes. Existing attached-entry edits remain separate. |
| `canvas_update_feedback_read_state` | Mark own submission feedback read/unread, or mark rubric/annotation feedback read, with independent state reads. This does not attest that the feedback was understood. |
| `canvas_send_message` | Send or reply to a conversation. |
| `canvas_update_inbox` | Apply supported conversation state changes. |
| `canvas_update_planner` | Create, update, or delete a personal note; apply a planner override. |
| `canvas_update_module_progress` | Mark/undo an allowed mark-done requirement, or explicitly record a must-view item as viewed. Fresh item/access state and exact completion readback are required. This does not submit work or alter grades. |
| `canvas_manage_personal_files` | Create personal folders; rename/move/delete personal files or folders. Only empty folders can be deleted; root, submitted/system folder trees and unsupported protected files are excluded. |
| `canvas_update_notifications` | Set one existing channel notification to immediately, daily, weekly or never, with exact readback. |
| `canvas_update_preferences` | Add/remove course/group favorites, set/delete a course nickname, or set a course color. |
| `canvas_create_discussion` | Create a plain discussion topic in an enrolled course or accepted group where student creation is permitted. |
| `canvas_post_group_discussion` | Post/reply within an explicitly identified group discussion with accepted membership. |
| `canvas_manage_group_discussion` | Edit/delete own group entry, subscribe/unsubscribe, change read state or rate a permitted entry. Attached-entry edits are refused. |
| `canvas_respond_to_invitation` | Accept or decline an own student invitation; acceptance can restore a declined invitation. This cannot withdraw an active enrollment. |
| `canvas_manage_bookmarks` | Create/edit/delete own bookmarks or move one to an explicit position, verifying affected ordering. No atomic bulk reorder. |
| `canvas_update_what_if_grades` | Set/reset a hypothetical assignment score or reset the course's hypothetical scores. Recorded grades are unchanged. Course reset independent verification covers visible submissions only and reports incomplete coverage for hidden rows. |

Execution verifies the relevant account and target. Treat an uncertain receipt as a reason to inspect Canvas, not as permission to retry a write. Hosted preparations and receipts are encrypted in the shared SQLite or PostgreSQL store; atomic claims and renewable execution leases fence competing workers. Prepared payloads expire after 10 minutes, and receipts are retained for up to seven days. Interrupted execution becomes uncertain once its lease expires. Local stdio actions remain process-local. Use SQLite only on one host with local disk, or PostgreSQL for separate hosts. See deployment guidance for measured boundaries.

The action kinds are `upload`, `submit_files`, `submit_text`, `submit_url`, `submission_comment`, `discussion_post`, `discussion_reply`, `inbox_send`, `inbox_reply`, `inbox_update`, `planner_create`, `planner_update`, `planner_delete`, `planner_override`, `discussion_edit`, `discussion_delete`, `discussion_subscribe`, `discussion_unsubscribe`, `discussion_read`, `discussion_rate`, `submission_comment_edit`, `submission_comment_delete`, `calendar_create`, `calendar_update`, `calendar_delete`, `module_mark_done`, `module_mark_read`, `personal_folder_create`, `personal_folder_rename`, `personal_folder_move`, `personal_folder_delete`, `personal_file_rename`, `personal_file_move`, `personal_file_delete`, `notification_preference_update`, `course_favorite_add`, `course_favorite_remove`, `group_favorite_add`, `group_favorite_remove`, `course_nickname_set`, `course_nickname_delete`, `course_color_set`, `topic_create`, `group_discussion_post`, `group_discussion_reply`, `group_discussion_edit`, `group_discussion_delete`, `group_discussion_subscribe`, `group_discussion_unsubscribe`, `group_discussion_read`, `group_discussion_rate`, `course_invitation_accept`, `course_invitation_decline`, `bookmark_create`, `bookmark_update`, `bookmark_reorder`, `bookmark_delete`, `what_if_score_set`, `what_if_score_reset`, `what_if_course_reset`, `mastery_path_select`, `group_join`, `group_leave`, `group_cancel_join_request`, `discussion_attachment_post`, `discussion_attachment_reply`, `submission_feedback_read`, `rubric_feedback_read`, and `annotation_feedback_read`. Call `canvas_prepare_action`, inspect the review, then follow its `execute.tool` and `action_id`. Each execution tool accepts a prepared action ID and checks its action kind; there is no generic execution bypass. Use `canvas_action_status` to inspect a receipt and `canvas_cancel_action` to discard a preparation.

A repeated identical payload normally returns its existing receipt. When the user intentionally wants the same action again, supply a fresh `intent_id` to preparation and review the repeat warning. Reuse that ID when retrying the same preparation. It is bound to the payload; an action still being prepared or executed cannot be bypassed with a new ID. Verify uncertain outcomes in Canvas before any repeat.

Comment files must be uploaded with destination `submission_comment`; personal-file and assignment-submission receipts cannot be reused as comment attachments. Preparation binds the files to the exact course, assignment, user, and bytes, then rechecks them before sending.

The hosted service does not expose arbitrary HTTP requests, local filesystem paths, or course-folder writes. It does not grade other students, impersonate another user, or turn a personal planner override into forced module completion.

## Reading response envelopes

The adapter returns a consistent envelope with `schema_version`, `status`, `data`, `links`, `coverage`, `errors`, and `source`. Follow the response's access state and coverage before forming a conclusion. Error details are intended to help recover without exposing authentication material. A failed request must not be presented as a successful empty result.

Use source URLs for citations and follow the returned routes. Respect the tool's page limits; where results are bounded or paginated, do not describe them as an exhaustive account audit. Refresh time-sensitive facts before acting on them.

Results exceeding 60,000 characters use a continuation reference instead of a silently truncated object. Use `canvas_read_result` to retrieve JSON-text windows of up to 40,000 characters and join all windows before parsing the complete result. Continuations belong to the authenticated account, expire after 10 minutes, and use a bounded encrypted shared store in hosted mode, or an in-memory cache for local stdio. A hosted continuation can be read by another worker. Reread the source if it expires, or after restarting a local stdio server. A result too large for that cache returns an explicit `result_too_large` response and requires a narrower request.

## Why full app parity is not promised

Canvas enforces the signed-in user's permissions, enrollment, institution-approved developer-key scopes, and individual resource restrictions. Even an unscoped developer key only reaches resources available to its authorizing user. [Canvas developer keys](https://developerdocs.instructure.com/services/canvas/oauth2/file.developer_keys)

The submissions API requires active student enrollment and assignment-compatible submission types. Its documented gaps include Google Docs submission integration and creating media comments. The adapter implements its listed subset; it does not infer support for every possible submission type from the API. [Canvas submissions API](https://developerdocs.instructure.com/services/canvas/resources/submissions)

Discussion metadata includes whether posts are visible and whether an initial post is required. These are meaningful access restrictions, not errors to work around. [Canvas discussion API](https://developerdocs.instructure.com/services/canvas/resources/discussion_topics)

External LTI tools, institution-specific apps, browser-only activity, quizzes/proctoring, and unsupported document types can require their own workflow. Quiz-taking, quiz submission, and unrestricted account-setting changes are not exposed in this release. Return a clear unsupported or external next step instead of claiming the task was completed.
