# Canvas developer-key scope manifest

This is the reviewed endpoint inventory for the new adapter in `src/canvas_connector/tools.py` and `actions.py`, `student_actions.py`, `module_progress.py`, `personal_files.py`, `preferences.py`, `group_content.py`, `enrollments.py`, `bookmarks.py`, `what_if_grades.py`, `mastery_paths.py`, `group_memberships.py`, `discussion_attachments.py`, `feedback.py`, `appointments.py`, `self_profile.py`, `dashboard_cards.py`, `inbox_state.py`, `inbox_messages.py`, `inbox_attachments.py`, `page_views.py`, `discussion_edits.py`, `group_files.py`, and `group_file_uploads.py`, plus the navigation and document-reader calls they use. It covers the implemented student surface, not every endpoint in the bundled library or every Canvas feature. Reconcile this inventory whenever those call sites change.

Canvas developer-key scopes use the exact route-template spelling below. For example, an API call containing `self` can correspond to a scope containing `:user_id`; keep Canvas's documented placeholder names rather than substituting real IDs. These upstream scopes are separate from this connector's `canvas:read` and `canvas:write` grants. An institutional administrator enables the upstream scopes. See [Canvas developer-key configuration](https://developerdocs.instructure.com/services/canvas/oauth2/file.developer_keys).

The current hosted authorization request does not send a narrower upstream `scope` parameter; it inherits the configured developer key's allowed scope set. The connector separately enforces its read/write grant. For an institution that wants upstream read-only credentials, configure a developer key with only the applicable REST read scopes below and omit the optional GraphQL scope, which is not read-only upstream.

## Identity, courses, and navigation

```text
url:GET|/api/v1/users/:user_id/profile
url:GET|/api/v1/courses
url:GET|/api/v1/courses/:id
url:GET|/api/v1/courses/:course_id/tabs
url:GET|/api/v1/courses/:course_id/modules
url:GET|/api/v1/courses/:course_id/modules/:id
url:GET|/api/v1/courses/:course_id/modules/:module_id/items
url:GET|/api/v1/courses/:course_id/module_item_sequence
url:GET|/api/v1/courses/:course_id/pages
url:GET|/api/v1/courses/:course_id/pages/:url_or_id/revisions/latest
url:GET|/api/v1/courses/:course_id/quizzes
url:GET|/api/v1/courses/:course_id/quizzes/:id
```

Sources: [users](https://developerdocs.instructure.com/services/canvas/resources/users), [courses](https://developerdocs.instructure.com/services/canvas/resources/courses), [tabs](https://developerdocs.instructure.com/services/canvas/resources/tabs), [modules](https://developerdocs.instructure.com/services/canvas/resources/modules), [pages](https://developerdocs.instructure.com/services/canvas/resources/pages), [quiz metadata](https://developerdocs.instructure.com/services/canvas/resources/quizzes).

Exact module-item reads use the collection route with a 1,000-record bound. The adapter avoids single-item SHOW because Canvas Horizon can record progress during that GET. Collection redirects and pagination links to a different path are refused before dispatch. Explicit progress writes retain their separate scopes below.

Passive page reads use metadata-only collection requests and the literal `revisions/latest` route, with fresh access/lock checks before and after fetching saved HTML. Page SHOW, front-page SHOW, body-including lists and historical revision routes are not used by the passive adapter. These ordinary SHOW/body-list reads can mark module progress in Canvas; explicit progress marking remains separate. Pages-tab denial or unsupported content returns a handoff rather than an unsafe fallback. Latest saved HTML carries `content_complete=false` because it may not include additional block/external rendering. See the [page serializer](https://github.com/instructure/canvas-lms/blob/master/lib/api/v1/wiki_page.rb) and [controller](https://github.com/instructure/canvas-lms/blob/master/app/controllers/wiki_pages_api_controller.rb).

## Explicit native page viewing

`page_view` uses a separate `canvas_view_page` write-permission group even though
Canvas implements its native view as GET. Preparation uses current-self context
access, bounded metadata and the exact latest-revision summary. It does not call
SHOW. Execution sends one exact `page_id:ID` SHOW with retries and redirects
disabled; subsequent access checks never repeat it. Native access records and
module-read effects are disclosed separately from returned content coverage.

```text
url:GET|/api/v1/courses/:course_id/pages/:url_or_id
url:GET|/api/v1/groups/:group_id/pages/:url_or_id
```

These scopes authorize a potentially state-changing view and can be omitted from
an upstream credential intended only for passive page reads. No page body update,
creation, deletion, publication, historical revision or front-page SHOW is added.
See the [native page controller](https://github.com/instructure/canvas-lms/blob/1c9f0bb8013ed69c4f2efe11fd483025469b7e6c/app/controllers/wiki_pages_api_controller.rb).

## Assignments, discussions, communication, and planning reads

```text
url:GET|/api/v1/courses/:course_id/assignments
url:GET|/api/v1/courses/:course_id/assignments/:id
url:GET|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id
url:GET|/api/v1/announcements
url:GET|/api/v1/courses/:course_id/discussion_topics
url:GET|/api/v1/courses/:course_id/discussion_topics/:topic_id
url:GET|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries
url:GET|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries/:entry_id/replies
url:GET|/api/v1/conversations
url:GET|/api/v1/conversations/:id
url:GET|/api/v1/planner/items
url:GET|/api/v1/planner_notes
url:GET|/api/v1/users/self/activity_stream
url:GET|/api/v1/users/self/activity_stream/summary
url:GET|/api/v1/calendar_events
url:GET|/api/v1/courses/:course_id/users
url:GET|/api/v1/courses/:course_id/groups
url:GET|/api/v1/users/self/groups
```

Sources: [assignments](https://developerdocs.instructure.com/services/canvas/resources/assignments), [submissions](https://developerdocs.instructure.com/services/canvas/resources/submissions), [announcements](https://developerdocs.instructure.com/services/canvas/resources/announcements), [discussion topics](https://developerdocs.instructure.com/services/canvas/resources/discussion_topics), [conversations](https://developerdocs.instructure.com/services/canvas/resources/conversations), [planner](https://developerdocs.instructure.com/services/canvas/resources/planner), [activity](https://developerdocs.instructure.com/services/canvas/resources/users), [calendar](https://developerdocs.instructure.com/services/canvas/resources/calendar_events), [course users](https://developerdocs.instructure.com/services/canvas/resources/courses), [groups](https://developerdocs.instructure.com/services/canvas/resources/groups).

## File and folder reads

```text
url:GET|/api/v1/files/:id
url:GET|/api/v1/courses/:course_id/files
url:GET|/api/v1/users/:user_id/files
url:GET|/api/v1/users/:user_id/files/quota
url:GET|/api/v1/folders/:id/files
url:GET|/api/v1/courses/:course_id/folders
url:GET|/api/v1/users/:user_id/folders
url:GET|/api/v1/folders/:id/folders
```

Document extraction first reads file metadata, then downloads the actual file through the validated URL returned by Canvas. Storage/CDN download URLs are not additional Canvas developer-key scope strings. [Files and folders API](https://developerdocs.instructure.com/services/canvas/resources/files)

## Optional actions and their additional readback scopes

Include the relevant read scopes above when enabling an action. Writes are followed by resource reads to verify the result; a successful mutation with a denied verification read is reported as uncertain.

```text
url:POST|/api/v1/users/:user_id/files
url:POST|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/comments/files
url:PUT|/api/v1/courses/:course_id/modules/:module_id/items/:id/done
url:DELETE|/api/v1/courses/:course_id/modules/:module_id/items/:id/done
url:POST|/api/v1/courses/:course_id/modules/:module_id/items/:id/mark_read
url:POST|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/files
url:POST|/api/v1/courses/:course_id/assignments/:assignment_id/submissions
url:PUT|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id
url:POST|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries
url:POST|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries/:entry_id/replies
url:GET|/api/v1/courses/:course_id/discussion_topics/:topic_id/entry_list
url:POST|/api/v1/conversations
url:POST|/api/v1/conversations/:id/add_message
url:PUT|/api/v1/conversations/:id
url:POST|/api/v1/planner_notes
url:PUT|/api/v1/planner_notes/:id
url:DELETE|/api/v1/planner_notes/:id
url:GET|/api/v1/planner_notes/:id
url:POST|/api/v1/planner/overrides
url:PUT|/api/v1/planner/overrides/:id
url:GET|/api/v1/planner/overrides/:id
url:GET|/api/v1/courses/:course_id/discussion_topics/:topic_id/view
url:PUT|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries/:id
url:DELETE|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries/:id
url:PUT|/api/v1/courses/:course_id/discussion_topics/:topic_id/subscribed
url:DELETE|/api/v1/courses/:course_id/discussion_topics/:topic_id/subscribed
url:PUT|/api/v1/courses/:course_id/discussion_topics/:topic_id/read
url:DELETE|/api/v1/courses/:course_id/discussion_topics/:topic_id/read
url:PUT|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries/:entry_id/read
url:DELETE|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries/:entry_id/read
url:POST|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries/:entry_id/rating
url:PUT|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/comments/:id
url:DELETE|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/comments/:id
url:POST|/api/v1/calendar_events
url:GET|/api/v1/calendar_events/:id
url:PUT|/api/v1/calendar_events/:id
url:DELETE|/api/v1/calendar_events/:id
```

Sources: [personal file uploads](https://developerdocs.instructure.com/services/canvas/resources/users), [submission uploads and comments](https://developerdocs.instructure.com/services/canvas/resources/submissions), [discussion posts and readback](https://developerdocs.instructure.com/services/canvas/resources/discussion_topics), [messages](https://developerdocs.instructure.com/services/canvas/resources/conversations), [planner actions](https://developerdocs.instructure.com/services/canvas/resources/planner), [personal calendar actions](https://developerdocs.instructure.com/services/canvas/resources/calendar_events), [comment editing](https://developerdocs.instructure.com/services/canvas/resources/submission_comments).

Upload finalization follows Canvas's validated returned confirmation URL. Some installations use an older `create_success` confirmation route. The static list above does not claim to enumerate every installation-specific finalization permission; exercise a small upload in a test course with the exact developer key before enabling uploads publicly. Do not broaden the key to unrelated administration endpoints to hide a denied call.

The submission-update scope also exists for grading, but this connector's action only sends its supported comment payload; Canvas still enforces the student's underlying permissions. No course/assignment creation, recorded-grade editing, quiz attempt, user administration, or arbitrary API-call tool is enabled by this manifest. Student What-if scores use the separate endpoints below.

## Personal file and folder management

The `canvas_manage_personal_files` group prepares folder creation, file/folder rename or move, file deletion, and empty-folder deletion. These additional routes are restricted in the adapter to the authenticated user's ordinary personal storage. The exact metadata reads establish ownership and root ancestry before mutation and verify the result afterward.

```text
url:GET|/api/v1/users/:user_id/files/:id
url:GET|/api/v1/users/:user_id/folders/:id
url:GET|/api/v1/folders/:id
url:POST|/api/v1/users/:user_id/folders
url:PUT|/api/v1/files/:id
url:DELETE|/api/v1/files/:id
url:PUT|/api/v1/folders/:id
url:DELETE|/api/v1/folders/:id
```

`canvas_get_folder` uses the general folder GET. Personal actions use the user-scoped reads with literal `self`; `root` is an allowed folder `:id`. Chosen-folder uploads remain in `canvas_upload_file`, using the existing `url:POST|/api/v1/users/:user_id/files` scope with a validated `parent_folder_id`. They do not require a folder-upload scope. This personal-file group has no forced or recursive deletion, course/group target selector, or replacement of an existing file. Separate group-file actions are documented below. Canvas's ordinary file representation omits edit/delete permissions; the adapter honors any returned permissions and preserves server-side denial for submitted files. [Files and folders API](https://developerdocs.instructure.com/services/canvas/resources/files)

## Notification preferences

`canvas_communication_channels` and `canvas_notification_preferences` use only these GET routes, in addition to the current-user profile scope. `canvas_update_notifications` is a separate mutation group for one existing channel and one exact notification.

```text
url:GET|/api/v1/users/:user_id/communication_channels
url:GET|/api/v1/users/:user_id/communication_channels/:communication_channel_id/notification_preferences
url:PUT|/api/v1/users/self/communication_channels/:communication_channel_id/notification_preferences/:notification
```

The single PUT sends only `notification_preferences[frequency]`; `immediately`, `daily`, `weekly`, and `never` are the allowed values. A `never` review explicitly identifies notification suppression. The list GET independently confirms the new frequency and unchanged other effective preferences. No channel creation/deletion, category update, or bulk preference scope is requested. [Communication channels](https://developerdocs.instructure.com/services/canvas/resources/communication_channels), [notification preferences](https://developerdocs.instructure.com/services/canvas/resources/notification_preferences).

Canvas's implementation can lazily persist missing default policy rows during its notification list GET. This is server housekeeping without an effective frequency change; connector read tools call no setters. [Official notification policy source](https://github.com/instructure/canvas-lms/blob/master/app/models/notification_policy.rb), [notification controller](https://github.com/instructure/canvas-lms/blob/master/app/controllers/notification_preferences_controller.rb).

## Dashboard preferences

`canvas_preferences` reads own favorites/default selections, course nicknames, or course colors. `canvas_update_preferences` prepares a single favorite add/remove, nickname set/delete, or course color change. Course actions verify an actual current-user enrollment; group actions verify accepted self membership. These operations also need the profile/course reads above and the group reads in the next section.

```text
url:GET|/api/v1/courses/:course_id/enrollments
url:GET|/api/v1/users/self/favorites/courses
url:GET|/api/v1/users/self/favorites/groups
url:POST|/api/v1/users/self/favorites/courses/:id
url:DELETE|/api/v1/users/self/favorites/courses/:id
url:POST|/api/v1/users/self/favorites/groups/:id
url:DELETE|/api/v1/users/self/favorites/groups/:id
url:GET|/api/v1/users/self/course_nicknames
url:GET|/api/v1/users/self/course_nicknames/:course_id
url:PUT|/api/v1/users/self/course_nicknames/:course_id
url:DELETE|/api/v1/users/self/course_nicknames/:course_id
url:GET|/api/v1/users/:id/colors
url:PUT|/api/v1/users/:id/colors/:asset_string
```

Enrollment requests filter to the verified current-user ID. Color writes fix the asset string to `course_<validated course_id>` and leave other colors intact. No dashboard-position, favorite reset, nickname clear-all, arbitrary profile, or custom-data mutation scope is used. [Favorites](https://developerdocs.instructure.com/services/canvas/resources/favorites), [users](https://developerdocs.instructure.com/services/canvas/resources/users), [enrollments](https://developerdocs.instructure.com/services/canvas/resources/enrollments).

Favorite-list membership can reflect Canvas default selections. Verification instead reads the exact course/group with `include[]=favorites` and requires a boolean `is_favorite`. Group-show's include is implemented by the official serializer but absent from that endpoint's documented include list; an installation omitting it is unsupported for group favorite mutation. [Official group serializer](https://github.com/instructure/canvas-lms/blob/master/lib/api/v1/group.rb).

## Group content and student discussion creation

Group read tools require accepted current-user membership and preserve Canvas locks, initial-post gates, and visibility restrictions. They issue GET requests only, without joining, marking read, or posting. Their additional scopes are:

```text
url:GET|/api/v1/groups/:group_id
url:GET|/api/v1/groups/:group_id/users/:user_id
url:GET|/api/v1/groups/:group_id/users
url:GET|/api/v1/groups/:group_id/pages
url:GET|/api/v1/groups/:group_id/pages/:url_or_id/revisions/latest
url:GET|/api/v1/groups/:group_id/discussion_topics
url:GET|/api/v1/groups/:group_id/discussion_topics/:topic_id
url:GET|/api/v1/groups/:group_id/discussion_topics/:topic_id/entries
url:GET|/api/v1/groups/:group_id/discussion_topics/:topic_id/entries/:entry_id/replies
url:GET|/api/v1/groups/:group_id/files
url:GET|/api/v1/groups/:group_id/files/:id
url:GET|/api/v1/groups/:group_id/folders
url:GET|/api/v1/groups/:group_id/folders/:id
```

`canvas_create_discussion` is a separate mutation group for a plain course/group topic with a reviewed audience, title, and message. It requires a confirmed `create_discussion_topic` permission and verifies a new topic by ID, current author, and exact text. Permission fallback and creation use:

```text
url:GET|/api/v1/courses/:course_id/permissions
url:GET|/api/v1/groups/:group_id/permissions
url:POST|/api/v1/courses/:course_id/discussion_topics
url:POST|/api/v1/groups/:group_id/discussion_topics
```

Course topic readback uses the existing course-discussion GET scope; group readback uses the group-discussion GET above. This adds no group creation/membership management, announcement or graded-topic creation, group-file writes, or quiz-attempt scope. [Groups and memberships](https://developerdocs.instructure.com/services/canvas/resources/groups), [course permissions](https://developerdocs.instructure.com/services/canvas/resources/courses), [discussion topics](https://developerdocs.instructure.com/services/canvas/resources/discussion_topics), [pages](https://developerdocs.instructure.com/services/canvas/resources/pages), [files](https://developerdocs.instructure.com/services/canvas/resources/files).

## Group discussion participation

`canvas_post_group_discussion` posts an entry or reply to one explicitly selected group topic. `canvas_manage_group_discussion` edits/deletes an own entry or changes the current user's subscription, topic/entry read state, or entry rating. Both reuse the profile, group membership, and exact-topic GET scopes above. Additional mutation and readback scopes are:

```text
url:GET|/api/v1/groups/:group_id/discussion_topics/:topic_id/entry_list
url:GET|/api/v1/groups/:group_id/discussion_topics/:topic_id/view
url:POST|/api/v1/groups/:group_id/discussion_topics/:topic_id/entries
url:POST|/api/v1/groups/:group_id/discussion_topics/:topic_id/entries/:entry_id/replies
url:PUT|/api/v1/groups/:group_id/discussion_topics/:topic_id/entries/:id
url:DELETE|/api/v1/groups/:group_id/discussion_topics/:topic_id/entries/:id
url:PUT|/api/v1/groups/:group_id/discussion_topics/:topic_id/subscribed
url:DELETE|/api/v1/groups/:group_id/discussion_topics/:topic_id/subscribed
url:PUT|/api/v1/groups/:group_id/discussion_topics/:topic_id/read
url:DELETE|/api/v1/groups/:group_id/discussion_topics/:topic_id/read
url:PUT|/api/v1/groups/:group_id/discussion_topics/:topic_id/entries/:entry_id/read
url:DELETE|/api/v1/groups/:group_id/discussion_topics/:topic_id/entries/:entry_id/read
url:POST|/api/v1/groups/:group_id/discussion_topics/:topic_id/entries/:entry_id/rating
```

Topic read-state changes affect the initial topic text; they do not mark every entry. Initial-post restrictions apply to replies and entry reads, and the adapter never posts automatically to unlock them. Legacy text-only edits refuse existing attachments; the separate preservation action below handles its bounded case. Ordinary entry objects do not include update/delete permission fields; the adapter verifies ownership, honors any returned denial, and leaves Canvas's permission enforcement intact. [Discussion participation and exact scope templates](https://developerdocs.instructure.com/services/canvas/resources/discussion_topics).

## Shared group files and folders

`canvas_manage_group_files` handles reviewed shared file/folder changes;
`canvas_upload_group_file` uploads immutable bytes to an exact group destination.
Preparation requires accepted self-membership and explicit native `participate`,
`read_files`, and the operation's `manage_files_add`, `manage_files_edit` or
`manage_files_delete` permission. It does not substitute uploader ownership for
collaborative group permissions. Additional/reused exact scopes are:

```text
url:GET|/api/v1/groups/:group_id/permissions
url:GET|/api/v1/groups/:group_id/folders
url:GET|/api/v1/groups/:group_id/folders/:id
url:GET|/api/v1/groups/:group_id/files/:id
url:GET|/api/v1/folders/:id/folders
url:GET|/api/v1/folders/:id/files
url:GET|/api/v1/courses/:course_id/settings
url:POST|/api/v1/groups/:group_id/folders
url:POST|/api/v1/groups/:group_id/files
url:PUT|/api/v1/folders/:id
url:DELETE|/api/v1/folders/:id
url:PUT|/api/v1/files/:id
url:DELETE|/api/v1/files/:id
```

The existing profile, exact group and self-membership reads are reused. Explicit
folder ancestry follows group-scoped numeric parent IDs to a root, bounded to
32 levels. Native-default discovery uses the passive group folder collection,
bounded to 1,000 folders. The literal root GET is not used: native Canvas can
create storage during that read. Missing/default parent on creation/upload means native
unfiled storage, whose root/default initialization occurs only inside execution.
Multiple ambiguous default candidates require choosing an explicit parent.

Folder rename/move inventories a bounded active subtree; native deleted-history
path changes are separately disclosed. No forced recursive delete, overwrite,
URL upload, cross-group move or arbitrary resource URL is exposed. File PUT
recalculates `locked` from usage-rights policy, and moves can change inherited
`hidden`; review binds known predictions or explicitly unknown native outcomes.
Readback verifies the saved target and actual visibility without claiming an
unknown outcome was pre-predicted. Group uploads use the existing bounded,
anonymous storage/validated finalization transport and independent group-scoped
file reads plus downloaded-byte hashes. Existing finalization caveats still apply.

Sources: [native folder controller](https://github.com/instructure/canvas-lms/blob/1c9f0bb8013ed69c4f2efe11fd483025469b7e6c/app/controllers/folders_controller.rb),
[folder model](https://github.com/instructure/canvas-lms/blob/1c9f0bb8013ed69c4f2efe11fd483025469b7e6c/app/models/folder.rb),
[group policy](https://github.com/instructure/canvas-lms/blob/1c9f0bb8013ed69c4f2efe11fd483025469b7e6c/app/models/group.rb),
[Files API](https://developerdocs.instructure.com/services/canvas/resources/files).

## Course invitations

`canvas_course_invitations` reads the authenticated student's invitations, including future/restricted course invitations. `canvas_respond_to_invitation` accepts an invited or previously declined enrollment, or declines an invited enrollment. It uses the existing profile scope and these additional scopes:

```text
url:GET|/api/v1/users/:user_id/enrollments
url:POST|/api/v1/courses/:course_id/enrollments/:id/accept
url:POST|/api/v1/courses/:course_id/enrollments/:id/reject
```

The user-scoped GET is called with `self` and verifies the exact enrollment, course, student role, and resulting state. Course date restrictions still apply after acceptance. These actions do not create enrollments, remove active enrollments, or use enrollment-administration scopes. [Enrollments and invitation responses](https://developerdocs.instructure.com/services/canvas/resources/enrollments).

## Personal bookmarks

`canvas_bookmarks` reads the current user's bookmarks. `canvas_manage_bookmarks` creates, updates, moves, or deletes a reviewed bookmark using the current-user profile scope and these routes:

```text
url:GET|/api/v1/users/self/bookmarks
url:GET|/api/v1/users/self/bookmarks/:id
url:POST|/api/v1/users/self/bookmarks
url:PUT|/api/v1/users/self/bookmarks/:id
url:DELETE|/api/v1/users/self/bookmarks/:id
```

Bookmark destinations are stored links and are not automatically opened or fetched. Position changes can shift neighboring bookmarks, so ordering verification also uses the list GET. The literal `self` in these scope templates is documented by Canvas. [Bookmarks](https://developerdocs.instructure.com/services/canvas/resources/bookmarks).

## Student What-if scores

`canvas_what_if_grades` reads saved hypothetical scores without invoking a mutation or grade calculation. `canvas_update_what_if_grades` sets/resets one own assignment's hypothetical score or resets the current student's course What-if scores. These operations reuse the profile, course, enrollment, course-permission, assignment, and single-submission GET scopes above, plus:

```text
url:GET|/api/v1/courses/:course_id/students/submissions
url:PUT|/api/v1/submissions/:id/what_if_grades
url:PUT|/api/v1/courses/:course_id/what_if_grades/reset
```

The submission-list GET filters to the current user. Writes send only a hypothetical `student_entered_score` or the dedicated reset request, then independently read saved scores and confirm unchanged recorded grades. Course reset verification covers Canvas-visible submissions; its response identifies that coverage limit. Hypothetical aggregate grades come from the mutation response and are not independently recalculated by a read. These endpoints add no teacher grading scope and do not use the generic submission-update PUT. [What-if grades](https://developerdocs.instructure.com/services/canvas/resources/what_if_grades), [submission reads](https://developerdocs.instructure.com/services/canvas/resources/submissions), [course permissions](https://developerdocs.instructure.com/services/canvas/resources/courses).

## Mastery Path selection

```text
url:POST|/api/v1/courses/:course_id/modules/:module_id/items/:id/select_mastery_path
```

The adapter uses existing course/enrollment/permission, module-item list and module-item-sequence reads to bind the current student's offered sets. It sends only `assignment_set_id`, with no student override. A subsequent sequence read must confirm selection; assignment access is checked separately and may lag background processing. It supports initial choices only, without claiming the API cannot change an eligible existing choice. [Modules](https://developerdocs.instructure.com/services/canvas/resources/modules)

## Course-group self membership

```text
url:POST|/api/v1/groups/:group_id/memberships
url:DELETE|/api/v1/groups/:group_id/users/:user_id
url:GET|/api/v1/group_categories/:group_category_id
```

Existing course-group and own-group lists, exact self-membership and group-permission reads establish the target and consequences. The adapter cannot silently remove a membership in another exclusive group or reassign leadership. A pending request is not accepted membership. It does not use management-only membership PUT as a substitute for an ordinary invitation response. [Groups](https://developerdocs.instructure.com/services/canvas/resources/groups), [group categories](https://developerdocs.instructure.com/services/canvas/resources/group_categories)

Discussion attachments reuse the course/group entry and reply POST scopes already listed. They send actual multipart bytes and verify the own file with existing self-file metadata/download reads. No arbitrary multipart path, other-user file or attachment-ID shortcut is exposed.

Existing attached-entry editing uses `discussion_attachment_edit` and the separate
`canvas_edit_discussion_attachment` group. It reuses exact course/group entry-list
GET, entry PUT, profile/context access and generic `files/:id` metadata scopes.
The fixed top-level JSON payload is `{message: reviewed_text, attachment: {}}`;
it neither removes nor uploads a file. This preserves the existing association
in the inspected native controller, whose omitted/null attachment parameter would
clear it. Preparation and readback bind the exact owned entry, association,
metadata and bounded bytes; original storage may be user, course or group.
Course-backed edits also read `url:GET|/api/v1/courses/:course_id/settings`
to verify `allow_student_discussion_editing`; ordinary entry JSON does not
provide a positive update-permission field. The mutation still enforces native
entry permissions and current availability.
Normal edit history, module participation, mentions and eligible LTI processing
still apply. [Native controller](https://github.com/instructure/canvas-lms/blob/1c9f0bb8013ed69c4f2efe11fd483025469b7e6c/app/controllers/discussion_entries_controller.rb#L95-L131).

## Feedback read markers

```text
url:PUT|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/read/:item
url:DELETE|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/read
url:GET|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/rubric_assessments/read
url:PUT|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/rubric_assessments/read
url:GET|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/document_annotations/read
url:PUT|/api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id/document_annotations/read
url:POST|/api/graphql
```

The optional GraphQL scope is **broad upstream access**, not a read-only Canvas scope: it can authorize queries and mutations within the user's existing permissions. Omit it if that scope is unacceptable; general feedback-state reads/changes then remain unavailable. Rubric/annotation markers use the dedicated REST routes and do not require GraphQL. This connector exposes only one fixed, side-effect-free GraphQL query for an independently verified own submission's identity and `readState`; there is no arbitrary query or mutation tool.

The REST submission include for read state can itself mark feedback read, so it is never used for preparation or verification. Marking aggregate feedback read explicitly reviews and performs three non-atomic item writes (grade, comment, rubric); comment marking covers all visible comments across attempts. Failure stops the sequence and leaves the receipt uncertain. The separate rubric-assessment/annotation preference flags do not establish aggregate read state. Feedback changes use their own write execution group and cannot grade, submit coursework or change another student's state. [Submissions](https://developerdocs.instructure.com/services/canvas/resources/submissions), [GraphQL scope](https://canvas.instructure.com/doc/api/graph_ql.html), [read-state resolver](https://github.com/instructure/canvas-lms/blob/master/app/graphql/types/submission_type.rb)

## Own profile, personal settings and dashboard ordering

These routes are fixed to the authenticated user. The profile reader compares profile and user-detail IDs and excludes private calendar feeds, SIS/LTI identifiers and raw avatar URLs. Names require Canvas's returned `can_update_name=true`; all three names are explicitly reviewed. The update uses Canvas's native `override_sis_stickiness=true` behavior, so an authorized user can change previously edited names. This flag does not bypass institution name-edit permissions. Settings use a small explicit boolean allowlist and never request `include[]=mobile_settings`.

```text
url:GET|/api/v1/users/:id
url:PUT|/api/v1/users/:id
url:GET|/api/v1/users/:id/settings
url:PUT|/api/v1/users/:id/settings
url:GET|/api/v1/dashboard/dashboard_cards
url:GET|/api/v1/users/:id/dashboard_positions
url:PUT|/api/v1/users/:id/dashboard_positions
```

The native cards route is defined in Canvas source but absent from the public Users API description. The adapter uses it rather than approximating the native card collection from favorites. Card ordering sends a complete permutation of the returned supported card set; the positions setter merges that patch, and readback checks preserved positions and saved native order, without establishing the selected dashboard view. A denied or unsupported native-card response requires a Canvas handoff. [Users API](https://developerdocs.instructure.com/services/canvas/resources/users), [dashboard route](https://github.com/instructure/canvas-lms/blob/master/config/routes.rb), [scope generation](https://github.com/instructure/canvas-lms/blob/master/lib/token_scopes.rb), [controller](https://github.com/instructure/canvas-lms/blob/master/app/controllers/users_controller.rb), [dashboard sorting](https://github.com/instructure/canvas-lms/blob/master/app/helpers/dashboard_helper.rb).

## Student appointment reservations

The appointment workflow reuses existing current-profile, course/enrollment, calendar-event read/delete and group-membership scopes. Additional routes are:

```text
url:GET|/api/v1/appointment_groups
url:GET|/api/v1/appointment_groups/:id
url:POST|/api/v1/calendar_events/:id/reservations
```

Discovery requests reservable appointment groups. Complete slot inventory uses the source-supported `appointment_group_ID` calendar context with explicit date bounds, rather than `all_events=true`. Reservation writes bind the exact student or verified candidate group; cancellation targets only a verified reservation child. Reserve preserves existing bookings. Reschedule uses `cancel_existing=true`, which replaces all bookings for that participant in the appointment group, so the adapter requires exactly one observed booking at preflight. Canvas provides no atomic expected-count guard: concurrent bookings may also be cancelled. Reschedule readback therefore reports incomplete coverage for the complete cancelled set, even when the known old deletion, new booking and final inventory are verified. No appointment-group creation, slot modification or arbitrary-participant route is exposed. [Appointment Groups API](https://developerdocs.instructure.com/services/canvas/resources/appointment_groups), [Calendar Events API](https://developerdocs.instructure.com/services/canvas/resources/calendar_events), [calendar controller](https://github.com/instructure/canvas-lms/blob/master/app/controllers/calendar_events_api_controller.rb), [reservation implementation](https://github.com/instructure/canvas-lms/blob/master/app/models/calendar_event.rb).


## Advanced student Inbox workflows

These additions reuse the current-profile, conversation list/show/create/add-message/update, course/group detail/enrollment and personal file upload/read scopes above. New scopes are:

```text
url:GET|/api/v1/search/recipients
url:POST|/api/v1/conversations/:id/remove_messages
url:DELETE|/api/v1/conversations/:id
```

Every thread read sets `auto_mark_as_read=false`. Star/subscription affect the current user's conversation participant; subscription can change cached read state and latest-message time. Deletion removes the current user's view, not recipients' copies. Empty-thread verification combines the native zero-count acknowledgement with independent non-marking absence and complete bounded own-list checks; a bare 404 is insufficient.

Native forwarding uses `forwarded_message_ids` on conversation creation, with exact reviewed source content and recipients. Selective replies use `recipients`, `included_messages` and verified `attachment_ids`; they never invoke `add_recipients`, which can share unreviewed deleted history. Included history may restore its original author's copy. Sender-side reads cannot prove each recipient's active historical visibility, so that coverage stays incomplete. Exact recipient searches and student-role checks prevent native audience expansion in unsupported instructor/account contexts.

Conversation uploads use the ordinary user upload route with a fixed verified folder ID, or the fixed `conversation attachments` path when absent. Complete own-folder discovery rejects duplicate native folder names. Byte verification and owner-bound receipts precede sending; upload target/finalization requests do not automatically retry or follow redirects. No arbitrary personal-file ID, admin deletion, sent-message edit or whole-history addition is exposed. [Conversations API](https://developerdocs.instructure.com/services/canvas/resources/conversations), [Search API](https://developerdocs.instructure.com/services/canvas/resources/search), [Files](https://developerdocs.instructure.com/services/canvas/resources/files), [native message/history behavior](https://github.com/instructure/canvas-lms/blob/master/app/models/conversation.rb), [participant behavior](https://github.com/instructure/canvas-lms/blob/master/app/models/conversation_participant.rb).
