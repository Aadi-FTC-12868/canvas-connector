"""Native Canvas HTTP mock coverage; no school account or real Canvas writes."""
import copy
import json

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import ValidationError

from canvas_connector import group_content as content

GROUP = {"context_type": "group", "group_id": 2}
COURSE = {"context_type": "course", "course_id": 1}
ACTION = {"kind": "topic_create", "context": GROUP, "title": "Study together?", "message": "<p>Let's review chapter 2.</p>"}


class MockCanvas:
    def __init__(self):
        self.seen = []
        self.group = {"id": 2, "name": "Study group", "context_type": "Course", "course_id": 1,
                      "permissions": {"create_discussion_topic": True}, "is_public": False}
        self.member = {"id": 15, "group_id": 2, "user_id": 7, "workflow_state": "accepted"}
        self.course = {"id": 1, "name": "Course", "workflow_state": "available",
                       "enrollments": [{"user_id": 7, "type": "student", "enrollment_state": "active"}],
                       "permissions": {"create_discussion_topic": True}}
        self.profile = {"id": 7}
        self.permissions = {"create_discussion_topic": True}
        self.topic = {"id": 10, "author": {"id": 7}, "title": ACTION["title"], "message": ACTION["message"],
                      "published": True, "locked_for_user": False, "user_can_see_posts": True,
                      "require_initial_post": False, "read_state": "unread", "assignment_id": None,
                      "is_announcement": False}
        self.page = {"page_id": 20, "url": "study-guide", "title": "Study guide", "body": "<p>Guide</p>",
                     "locked_for_user": False, "published": True, "updated_at": "2026-01-01T00:00:00Z"}
        self.file = {"id": 30, "folder_id": 40, "display_name": "guide.pdf", "locked_for_user": False}
        self.folder = {"id": 40, "name": "Group files", "context_type": "Group", "context_id": 2}
        self.rows = [{"id": i, "message": f"Entry {i}", "read_state": "unread"} for i in range(50, 56)]
        self.errors = {}
        self.after_write = None
        self.create_result = None
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.canvas = Canvas(CanvasConfig("https://school.example", "synthetic-token"), http_client=self.http)

    @property
    def writes(self):
        return [r for r in self.seen if r.method != "GET"]

    def close(self):
        self.canvas.close()
        self.http.close()

    def handle(self, request):
        self.seen.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.url.host == "school.example"
        assert "as_user_id" not in request.url.params and "student_id" not in request.url.params
        path = request.url.path.removeprefix("/api/v1/")
        if request.method == "GET":
            assert request.content == b""
        if (request.method, path) in self.errors:
            status, body = self.errors[request.method, path]
            return httpx.Response(status, json=body)
        if request.method == "POST":
            assert path in {"groups/2/discussion_topics", "courses/1/discussion_topics"}
            assert request.headers["content-type"] == "application/json"
            body = json.loads(request.content)
            assert set(body) == {"title", "message"}
            self.topic.update(body)
            result = copy.deepcopy(self.create_result if self.create_result is not None else self.topic)
            if self.after_write:
                self.after_write(self)
            return httpx.Response(201, json=result)
        if path == "users/self/profile":
            body = self.profile
        elif path == "groups/2":
            assert request.url.params.get_list("include[]") == ["permissions"]
            body = self.group
        elif path == "groups/2/users/self":
            body = self.member
        elif path == "courses/1":
            assert request.url.params.get_list("include[]") == ["permissions"]
            body = self.course
        elif path in {"groups/2/permissions", "courses/1/permissions"}:
            assert request.url.params.get_list("permissions[]") == ["create_discussion_topic"]
            body = self.permissions
        elif path in {"groups/2/discussion_topics/10", "courses/1/discussion_topics/10"}:
            body = self.topic
        elif path == "groups/2/pages/page_id:20/revisions/latest":
            body = {key: self.page[key] for key in ("url", "title", "body", "updated_at")}
            body.update(revision_id=1, latest=True)
        elif path == "groups/2/pages" and request.url.params.get("per_page") == "100":
            body = [{key: value for key, value in self.page.items() if key != "body"}]
        elif path == "groups/2/files/30":
            body = self.file
        elif path == "groups/2/folders/40":
            body = self.folder
        elif path in {"groups/2/users", "groups/2/pages", "groups/2/discussion_topics", "groups/2/files", "groups/2/folders",
                      "groups/2/discussion_topics/10/entries", "groups/2/discussion_topics/10/entries/50/replies",
                      "courses/1/discussion_topics/10/entries"}:
            index = int(request.url.params.get("page", "1"))
            size = int(request.url.params["per_page"])
            body = self.rows[(index - 1) * size:index * size]
            headers = {}
            if index * size < len(self.rows):
                headers["Link"] = f'<{request.url.copy_set_param("page", index + 1)}>; rel="next"'
            return httpx.Response(200, json=body, headers=headers)
        else:
            raise AssertionError((request.method, str(request.url)))
        return httpx.Response(200, json=body)


@pytest.fixture
def upstream():
    result = MockCanvas()
    yield result
    result.close()


@pytest.mark.parametrize("context", [COURSE, GROUP])
def test_create_uses_exact_context_body_and_fresh_author_content_readback(upstream, context):
    payload = {**ACTION, "context": context}
    target = content.snapshot(upstream.canvas, payload)
    assert target["user_id"] == 7 and target["context"] == context
    assert not upstream.writes
    result = content.perform(upstream.canvas, payload, target)
    assert result["verified"] is True and result["topic_id"] == 10
    assert result["title"] == payload["title"] and result["message"] == payload["message"]
    assert len(upstream.writes) == 1
    assert upstream.writes[0].url.path == f"/api/v1/{content.context_path(context)}/discussion_topics"
    assert upstream.seen[-1].method == "GET"
    assert upstream.seen[-1].url.path.endswith("/discussion_topics/10")
    assert content.ROUTES[payload["kind"]] == "canvas_create_discussion"


@pytest.mark.parametrize("context", [COURSE, GROUP])
def test_missing_embedded_permission_uses_actual_permissions_endpoint(upstream, context):
    upstream.group.pop("permissions")
    upstream.course.pop("permissions")
    target = content.snapshot(upstream.canvas, {**ACTION, "context": context})
    assert target["permissions"] == {"create_discussion_topic": True}
    assert upstream.seen[-1].url.path.endswith("/permissions")
    assert not upstream.writes


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_missing_or_denied_permission_is_not_allowed(upstream, value):
    upstream.group.pop("permissions")
    upstream.permissions = {"create_discussion_topic": value}
    with pytest.raises(ValueError, match="permit|unknown"):
        content.snapshot(upstream.canvas, ACTION)
    assert not upstream.writes


def test_explicit_embedded_denial_does_not_fallback(upstream):
    upstream.group["permissions"]["create_discussion_topic"] = False
    with pytest.raises(ValueError, match="does not permit"):
        content.snapshot(upstream.canvas, ACTION)
    assert not any(r.url.path.endswith("/permissions") for r in upstream.seen)


@pytest.mark.parametrize("field,value", [("user_id", 9), ("user_id", True), ("group_id", 9),
                                         ("workflow_state", "invited"), ("workflow_state", "requested"),
                                         ("workflow_state", None)])
def test_group_access_requires_exact_accepted_self_membership(upstream, field, value):
    upstream.member[field] = value
    with pytest.raises(ValueError):
        content.snapshot(upstream.canvas, ACTION)
    with pytest.raises(ValueError):
        content.list_group_members(upstream.canvas, 2)
    assert not upstream.writes
    assert not any(r.url.path.endswith("/groups/2/users") for r in upstream.seen)


@pytest.mark.parametrize("enrollments", [None, [], [{"user_id": 9, "type": "student", "enrollment_state": "active"}],
    [{"user_id": 7, "type": "teacher", "enrollment_state": "active"}],
    [{"user_id": 7, "type": "student", "enrollment_state": "invited"}],
    [{"user_id": 7, "type": "student", "enrollment_state": "completed"}],
    [{"type": "student", "enrollment_state": "active"}]])
def test_course_creation_requires_active_current_student(upstream, enrollments):
    upstream.course["enrollments"] = enrollments
    with pytest.raises(ValueError, match="active student enrollment"):
        content.snapshot(upstream.canvas, {**ACTION, "context": COURSE})
    assert not upstream.writes


@pytest.mark.parametrize("context", [COURSE, GROUP])
def test_denied_upstream_create_stays_denied_without_retry(upstream, context):
    payload = {**ACTION, "context": context}
    target = content.snapshot(upstream.canvas, payload)
    path = content.context_path(context) + "/discussion_topics"
    upstream.errors["POST", path] = (403, {"message": "not authorized"})
    with pytest.raises(CanvasError) as error:
        content.perform(upstream.canvas, payload, target)
    assert error.value.status == 403 and len(upstream.writes) == 1


@pytest.mark.parametrize("change", [{"id": 99}, {"author": {"id": 9}}, {"author": {"id": "7"}}, {"author": None},
    {"title": "different"}, {"message": "different"}, {"group_id": 8}, {"is_announcement": True}, {"assignment_id": 55}])
def test_readback_must_match_identity_author_and_exact_content(upstream, change):
    target = content.snapshot(upstream.canvas, ACTION)
    upstream.after_write = lambda state: state.topic.update(change)
    with pytest.raises(ValueError):
        content.perform(upstream.canvas, ACTION, target)
    assert len(upstream.writes) == 1


def test_failed_readback_does_not_repeat_creation(upstream):
    target = content.snapshot(upstream.canvas, ACTION)
    upstream.after_write = lambda state: state.errors.update({("GET", "groups/2/discussion_topics/10"): (403, {"message": "forbidden"})})
    with pytest.raises(CanvasError):
        content.perform(upstream.canvas, ACTION, target)
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("context", [
    {"context_type": "user", "user_id": 7}, {"context_type": "group", "group_id": 2, "course_id": 1},
    {"context_type": "course", "course_id": 1, "group_id": 2}, {"group_id": 2},
    {"context_type": "group", "group_id": True}, {"context_type": "group", "group_id": "2"},
    {"context_type": "group", "group_id": 0}, {"context_type": "group", "group_id": 2**63},
])
def test_creation_context_is_strict_and_unambiguous(context):
    with pytest.raises(ValidationError):
        content.TopicCreate.model_validate({**ACTION, "context": context})


@pytest.mark.parametrize("extra", [{"is_announcement": True}, {"assignment": {"points_possible": 10}},
    {"published": False}, {"delayed_post_at": "2026-11-01T00:00:00Z"}, {"pinned": True},
    {"user_id": 9}, {"as_user_id": 9}, {"attachment": "anything"}])
def test_student_creation_has_only_reviewed_title_message_and_context(extra):
    with pytest.raises(ValidationError):
        content.TopicCreate.model_validate({**ACTION, **extra})


@pytest.mark.parametrize("field,value", [("title", ""), ("message", " \n"), ("title", "t" * 256), ("message", "m" * 100001)])
def test_creation_content_is_bounded_nonblank(field, value):
    with pytest.raises(ValidationError):
        content.TopicCreate.model_validate({**ACTION, field: value})


@pytest.mark.parametrize("helper", [content.list_group_members, content.list_group_pages, content.list_group_discussions,
                                    content.list_group_files, content.list_group_folders])
def test_group_lists_follow_bounded_pagination_without_writes(upstream, helper):
    result = helper(upstream.canvas, 2, offset=0, limit=2)
    assert [row["id"] for row in result["data"]] == [50, 51]
    assert result["coverage"]["complete"] is False and result["coverage"]["next_offset"] == 2
    assert result["coverage"]["membership_state"] == "accepted"
    assert len(upstream.seen) == 5  # identity/group/member + first page + one lookahead page
    assert not upstream.writes
    result = helper(upstream.canvas, 2, offset=4, limit=2)
    assert [row["id"] for row in result["data"]] == [54, 55]
    assert result["coverage"]["next_offset"] is None and result["coverage"]["complete"] is False


@pytest.mark.parametrize("offset,limit", [(-1, 5), (10001, 5), (0, 0), (0, 101), (True, 1)])
def test_invalid_window_has_no_network_requests(upstream, offset, limit):
    with pytest.raises(ValueError):
        content.list_group_members(upstream.canvas, 2, offset=offset, limit=limit)
    assert not upstream.seen


@pytest.mark.parametrize("slug", ["study-guide", "20", "page_id:20"])
def test_group_page_has_exact_identity_and_refuses_locked_content(upstream, slug):
    upstream.page.update(locked_for_user=True, lock_explanation="Locked until tomorrow")
    with pytest.raises(CanvasError, match="locked"):
        content.get_group_page(upstream.canvas, 2, slug)
    assert not upstream.writes


@pytest.mark.parametrize("slug", ["../courses", "a/b", "a%2fb", "a?x=y", "a#x", "page_id:0", "page_id:true", "\x00"])
def test_page_slug_cannot_inject_routes(upstream, slug):
    with pytest.raises(ValueError):
        content.get_group_page(upstream.canvas, 2, slug)
    assert not upstream.seen


def test_group_detail_files_and_folder_use_scoped_gets(upstream):
    assert content.get_group(upstream.canvas, 2)["membership"]["user_id"] == 7
    assert content.get_group_file(upstream.canvas, 2, 30)["id"] == 30
    assert content.get_group_folder(upstream.canvas, 2, 40)["context_id"] == 2
    upstream.folder["context_id"] = 3
    with pytest.raises(ValueError):
        content.get_group_folder(upstream.canvas, 2, 40)
    assert not upstream.writes


@pytest.mark.parametrize("entry_id", [None, 50])
def test_group_entries_and_replies_preserve_unread_state(upstream, entry_id):
    result = content.list_entries(upstream.canvas, GROUP, 10, entry_id, limit=2)
    assert result["data"][0]["read_state"] == "unread"
    assert result["coverage"]["next_offset"] == 2
    assert upstream.topic["read_state"] == "unread" and not upstream.writes


@pytest.mark.parametrize("change", [{"id": 99}, {"group_id": 99}, {"context_id": 99}, {"context_type": "Course"}])
def test_group_topic_cannot_read_a_mismatched_returned_context(upstream, change):
    upstream.topic.update(change)
    with pytest.raises(ValueError):
        content.list_entries(upstream.canvas, GROUP, 10)
    assert not any("/entries" in r.url.path for r in upstream.seen)
    assert not upstream.writes


def test_closed_for_comments_is_still_readable(upstream):
    upstream.topic["locked"] = True
    result = content.list_entries(upstream.canvas, GROUP, 10)
    assert result["status"] == "ok" and result["data"]
    assert not upstream.writes


@pytest.mark.parametrize("change,access,status", [
    ({"user_can_see_posts": False, "require_initial_post": True}, "initial_post_required", "forbidden"),
    ({"user_can_see_posts": False}, "posts_not_visible", "forbidden"),
    ({"locked_for_user": True}, "locked_for_user", "locked"),
    ({"published": False}, "locked_for_user", "locked"),
])
def test_read_gates_do_not_fetch_posts_or_unlock_them(upstream, change, access, status):
    upstream.topic.update(change)
    result = content.list_entries(upstream.canvas, GROUP, 10)
    assert result["status"] == status and result["data"]["access"] == access
    assert result["coverage"]["complete"] is False
    assert not any("/entries" in r.url.path for r in upstream.seen)
    assert not upstream.writes


def test_api_initial_post_gate_is_preserved_when_topic_metadata_omits_it(upstream):
    upstream.topic.pop("user_can_see_posts")
    upstream.errors["GET", "groups/2/discussion_topics/10/entries"] = (403, {"error": "require_initial_post"})
    result = content.list_entries(upstream.canvas, GROUP, 10)
    assert result["status"] == "forbidden" and result["data"]["access"] == "initial_post_required"
    assert not upstream.writes


def test_unrelated_list_failure_is_not_reported_as_empty(upstream):
    upstream.errors["GET", "groups/2/files"] = (403, {"message": "not authorized"})
    with pytest.raises(CanvasError) as error:
        content.list_group_files(upstream.canvas, 2)
    assert error.value.status == 403


def test_snapshot_changes_when_reviewed_audience_or_membership_changes(upstream):
    before = content.snapshot(upstream.canvas, ACTION)
    upstream.group.update(members_count=99, updated_at="later")
    assert content.snapshot(upstream.canvas, ACTION) == before
    upstream.group["name"] = "Different group name"
    assert content.snapshot(upstream.canvas, ACTION) != before


def test_target_cannot_select_a_different_context(upstream):
    target = content.snapshot(upstream.canvas, ACTION)
    target["context"] = COURSE
    with pytest.raises(ValueError):
        content.perform(upstream.canvas, ACTION, target)
    assert not upstream.writes
