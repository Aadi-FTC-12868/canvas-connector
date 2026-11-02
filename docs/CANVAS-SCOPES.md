# Canvas developer-key scope manifest

This is the reviewed endpoint inventory for the new adapter in `src/canvas_connector/tools.py` and `actions.py`, plus the navigation and document-reader calls they use. It covers the implemented student surface, not every endpoint in the bundled library or every Canvas feature. Reconcile this inventory whenever those call sites change.

Canvas developer-key scopes use the exact route-template spelling below. For example, an API call containing `self` can correspond to a scope containing `:user_id`; keep Canvas's documented placeholder names rather than substituting real IDs. These upstream scopes are separate from this connector's `canvas:read` and `canvas:write` grants. An institutional administrator enables the upstream scopes. See [Canvas developer-key configuration](https://developerdocs.instructure.com/services/canvas/oauth2/file.developer_keys).

The current hosted authorization request does not send a narrower upstream `scope` parameter; it inherits the configured developer key's allowed scope set. The connector separately enforces its read/write grant. For an institution that wants upstream read-only credentials, configure a developer key with only the applicable read scopes below.

## Identity, courses, and navigation

```text
url:GET|/api/v1/users/:user_id/profile
url:GET|/api/v1/courses
url:GET|/api/v1/courses/:id
url:GET|/api/v1/courses/:course_id/tabs
url:GET|/api/v1/courses/:course_id/modules
url:GET|/api/v1/courses/:course_id/modules/:module_id/items
url:GET|/api/v1/courses/:course_id/modules/:module_id/items/:id
url:GET|/api/v1/courses/:course_id/module_item_sequence
url:GET|/api/v1/courses/:course_id/pages
url:GET|/api/v1/courses/:course_id/pages/:url_or_id
url:GET|/api/v1/courses/:course_id/front_page
url:GET|/api/v1/courses/:course_id/quizzes
url:GET|/api/v1/courses/:course_id/quizzes/:id
```

Sources: [users](https://developerdocs.instructure.com/services/canvas/resources/users), [courses](https://developerdocs.instructure.com/services/canvas/resources/courses), [tabs](https://developerdocs.instructure.com/services/canvas/resources/tabs), [modules](https://developerdocs.instructure.com/services/canvas/resources/modules), [pages](https://developerdocs.instructure.com/services/canvas/resources/pages), [quiz metadata](https://developerdocs.instructure.com/services/canvas/resources/quizzes).

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
```

Sources: [personal file uploads](https://developerdocs.instructure.com/services/canvas/resources/users), [submission uploads and comments](https://developerdocs.instructure.com/services/canvas/resources/submissions), [discussion posts and readback](https://developerdocs.instructure.com/services/canvas/resources/discussion_topics), [messages](https://developerdocs.instructure.com/services/canvas/resources/conversations), [planner actions](https://developerdocs.instructure.com/services/canvas/resources/planner).

Upload finalization follows Canvas's validated returned confirmation URL. Some installations use an older `create_success` confirmation route. The static list above does not claim to enumerate every installation-specific finalization permission; exercise a small upload in a test course with the exact developer key before enabling uploads publicly. Do not broaden the key to unrelated administration endpoints to hide a denied call.

The submission-update scope also exists for grading, but this connector's action only sends its supported comment payload; Canvas still enforces the student's underlying permissions. No course/assignment creation, grade editing, quiz attempt, user administration, or arbitrary API-call tool is enabled by this manifest.
