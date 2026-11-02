"""Native page views through real HTTP/store/MCP boundaries; synthetic only."""
import asyncio
import copy
import json

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from canvas_connector import page_views as p
from canvas_connector.actions import ActionStore
from canvas_connector.tools import register_tools


COURSE = {"kind": "page_view", "context": {"context_type": "course", "course_id": 11}, "page_id": 12}
GROUP = {"kind": "page_view", "context": {"context_type": "group", "group_id": 9}, "page_id": 12}
STAMP = "2026-01-02T12:00:00Z"


class API:
    def __init__(self):
        self.calls = []
        self.actor = 7
        self.course = {"id": 11, "name": "Synthetic course", "workflow_state": "available", "horizon_course": False}
        self.enrollments = [{"id": 22, "user_id": 7, "course_id": 11, "type": "StudentEnrollment", "enrollment_state": "active"}]
        self.group = {"id": 9, "course_id": 11, "context_type": "Course", "name": "Synthetic group", "workflow_state": "available"}
        self.member = {"id": 33, "user_id": 7, "group_id": 9, "workflow_state": "accepted"}
        self.metadata = {"page_id": 12, "url": "7", "title": "Current page", "updated_at": STAMP,
                         "published": True, "front_page": False, "locked_for_user": False,
                         "lock_info": None, "editing_roles": "teachers", "editor": "rce"}
        self.pages = [self.metadata]
        self.summary = {"revision_id": 3, "updated_at": STAMP, "latest": True, "edited_by": {"id": 8}}
        self.page = {**self.metadata, "body": "<p>Useful native content</p>"}
        self.list_status = None
        self.summary_status = None
        self.native_status = None
        self.native_error = False
        self.page_link = None
        self.enrollment_link = None
        self.enrollment_next_rows = None
        self.after_view = lambda: None

    @property
    def views(self):
        return [request for request in self.calls if request.url.path.endswith("/pages/page_id:12")]

    def handle(self, request):
        self.calls.append(request)
        assert request.method == "GET", "A native view must not edit page content"
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        path = request.url.path
        if path == "/api/v1/users/self/profile":
            return httpx.Response(200, json={"id": self.actor, "calendar": {"ics": "PRIVATE_FEED"}})
        if path == "/api/v1/courses/11":
            assert request.url.params.get_list("include[]") == ["horizon_course"]
            return httpx.Response(200, json=self.course)
        if path == "/api/v1/courses/11/enrollments":
            assert request.url.params["user_id"] == str(self.actor)
            assert request.url.params.get_list("type[]") == ["StudentEnrollment"]
            assert request.url.params.get_list("state[]") == ["active", "completed"]
            if "page" in request.url.params and self.enrollment_next_rows is not None:
                return httpx.Response(200, json=self.enrollment_next_rows)
            headers = {"link": self.enrollment_link} if self.enrollment_link else {}
            return httpx.Response(200, json=self.enrollments, headers=headers)
        if path == "/api/v1/groups/9":
            return httpx.Response(200, json=self.group)
        if path == "/api/v1/groups/9/users/self":
            return httpx.Response(200, json=self.member)
        if path in {"/api/v1/courses/11/pages", "/api/v1/groups/9/pages"}:
            assert "include[]" not in request.url.params
            assert dict(request.url.params) == {"sort": "title", "order": "asc", "per_page": "100"}
            if self.list_status:
                return httpx.Response(self.list_status, json={"message": "Pages collection unavailable"})
            headers = {"link": self.page_link} if self.page_link else {}
            return httpx.Response(200, json=copy.deepcopy(self.pages), headers=headers)
        if path.endswith("/pages/page_id:12/revisions/latest"):
            assert dict(request.url.params) == {"summary": "true"}
            if self.summary_status:
                return httpx.Response(self.summary_status, json={"message": "Revision witness unavailable"})
            return httpx.Response(200, json=copy.deepcopy(self.summary))
        if path.endswith("/pages/page_id:12"):
            assert not request.url.query and not request.content
            if self.native_error:
                raise httpx.ReadError("synthetic transport interruption", request=request)
            if self.native_status:
                return httpx.Response(self.native_status, json={"message": "native view failed"},
                                      headers={"location": str(request.url), "retry-after": "0"})
            value = copy.deepcopy(self.page)
            self.after_view()
            return httpx.Response(200, json=value)
        raise AssertionError(f"Unexpected page request: {path}")


@pytest.fixture
def api():
    value = API()
    # A caller's permissive httpx redirect default must not override the action.
    with Canvas(CanvasConfig("https://school.example", "synthetic-token"),
                http_client=httpx.Client(transport=httpx.MockTransport(value.handle), follow_redirects=True)) as canvas:
        value.canvas = canvas
        yield value


def execute(api, action=COURSE):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", action)
    result = store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
    return store, prepared, result


@pytest.mark.parametrize("action", [COURSE, GROUP])
def test_passive_prepare_and_single_exact_id_native_view_with_receipt(api, action):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", action)
    assert prepared["execute"]["tool"] == "canvas_view_page"
    target = prepared["review"]["target"]
    assert "module" in target["effect"] and target["progress_verified"] is False
    assert target["revision_summary"]["revision_id"] == 3
    assert not api.views
    assert "body" not in json.dumps(target)
    result = store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "succeeded"
    data = result["result"]
    assert data["content"]["body"] == "<p>Useful native content</p>"
    assert data["content_format"] == "html" and data["content_complete"] is True
    assert data["progress_verified"] is False and data["coverage"]["rendered_page_complete"] is False
    assert data["verification_scope"] == "native_response_page_identity"
    assert len(api.views) == 1
    calls = len(api.calls)
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None) == result
    assert len(api.calls) == calls
    assert api.views[0].url.path == "/api/v1/" + p.context_path(action["context"]) + "/pages/page_id:12"


@pytest.mark.parametrize("change", [{"page_id": True}, {"page_id": "12"}, {"page_id": 0}, {"page_id": 2**63},
                                     {"body": "unexpected edit"}, {"context": {"context_type": "course", "course_id": 11, "group_id": 9}},
                                     {"context": {"context_type": "group", "group_id": False}}])
def test_strict_view_model_rejects_ambiguous_context_or_edit_fields(api, change):
    with pytest.raises(ValidationError):
        p.snapshot(api.canvas, {**COURSE, **change})
    assert not api.calls


@pytest.mark.parametrize("status", [403, 404])
def test_unavailable_pages_tab_does_not_falsely_deny_exact_native_view(api, status):
    api.list_status = status
    _, prepared, result = execute(api)
    assert prepared["review"]["target"]["metadata"]["status"] == "unavailable"
    assert result["state"] == "succeeded" and len(api.views) == 1


@pytest.mark.parametrize("rows,status", [([], "not_listed"), ([{"page_id": 13, "url": "other"}], "not_listed"),
                                         ([{"page_id": 13, "url": "other"}] * 1001, "incomplete")])
def test_summary_permission_witness_can_cover_unlisted_or_bounded_out_metadata(api, rows, status):
    api.pages = rows
    _, prepared, result = execute(api)
    assert prepared["review"]["target"]["metadata"]["status"] == status
    assert result["state"] == "succeeded" and len(api.views) == 1


@pytest.mark.parametrize("status", [403, 404, 500])
def test_missing_revision_witness_does_not_fall_back_to_native_show(api, status):
    api.summary_status = status
    with pytest.raises(Exception) as error:
        p.snapshot(api.canvas, COURSE)
    if status == 404:
        assert "native page access may still work" in str(error.value)
    assert not api.views


@pytest.mark.parametrize("changes", [{"latest": False}, {"latest": 1}, {"revision_id": True}, {"updated_at": "bad"},
                                      {"updated_at": "2026-01-02"}, {"edited_by": {"id": "8"}}])
def test_invalid_revision_summary_is_not_permission_evidence(api, changes):
    api.summary.update(changes)
    with pytest.raises(ValueError):
        p.snapshot(api.canvas, COURSE)
    assert not api.views


def test_imported_revision_without_author_is_accepted_and_unrequested_body_never_reviewed(api):
    api.summary.pop("edited_by")
    api.summary["body"] = "PRIVATE_UNREQUESTED_REVISION_BODY"
    target = p.snapshot(api.canvas, COURSE)
    assert target["revision_summary"]["edited_by"] is None
    assert "PRIVATE_UNREQUESTED" not in json.dumps(target) and not api.views


@pytest.mark.parametrize("changes", [{"user_id": 8}, {"course_id": 99}, {"type": "TeacherEnrollment"},
                                      {"enrollment_state": "invited"}, {"id": True}])
def test_course_view_requires_actual_current_student_enrollment(api, changes):
    api.enrollments[0].update(changes)
    with pytest.raises(ValueError):
        p.snapshot(api.canvas, COURSE)
    assert not api.views


def test_completed_student_can_view_when_native_permission_witness_allows(api):
    api.enrollments[0]["enrollment_state"] = "completed"
    assert execute(api)[2]["state"] == "succeeded"


@pytest.mark.parametrize("rows", [[], [{"id": 22, "user_id": 7, "course_id": 11, "type": "StudentEnrollment", "enrollment_state": "active"}] * 2])
def test_absent_or_duplicate_enrollment_is_not_access_evidence(api, rows):
    api.enrollments = rows
    with pytest.raises(ValueError):
        p.snapshot(api.canvas, COURSE)
    assert not api.views


@pytest.mark.parametrize("changes", [{"user_id": 8}, {"group_id": 99}, {"workflow_state": "invited"}, {"workflow_state": "requested"}])
def test_group_view_requires_exact_accepted_self_membership(api, changes):
    api.member.update(changes)
    with pytest.raises(ValueError):
        p.snapshot(api.canvas, GROUP)
    assert not api.views


@pytest.mark.parametrize("changes", [{"non_collaborative": True}, {"workflow_state": "deleted"}, {"id": 99}])
def test_noncollaborative_or_wrong_group_rejected(api, changes):
    api.group.update(changes)
    with pytest.raises(ValueError):
        p.snapshot(api.canvas, GROUP)
    assert not api.views


@pytest.mark.parametrize("where,change", [("summary", {"revision_id": 4}), ("metadata", {"title": "Changed title"}),
                                         ("metadata", {"locked_for_user": True}), ("course", {"workflow_state": "completed"})])
def test_stale_review_rejects_before_native_show(api, where, change):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", COURSE)
    getattr(api, where).update(change)
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "rejected"
    assert not api.views


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 403, 429, 500])
def test_side_effecting_native_get_never_redirects_or_retries(api, status):
    api.native_status = status
    store, prepared, result = execute(api)
    assert result["state"] == "uncertain" and len(api.views) == 1
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert len(api.views) == 1


def test_native_transport_interruption_is_terminal_uncertain(api):
    api.native_error = True
    store, prepared, result = execute(api)
    assert result["state"] == "uncertain" and len(api.views) == 1
    store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
    assert len(api.views) == 1


@pytest.mark.parametrize("change", [{"page_id": 99}, {"page_id": True}, {"group_id": 9}, {"course_id": 99},
                                     {"context_type": "Group"}, {"context_id": 99},
                                     {"title": None}, {"url": None}, {"lock_info": False}, {"body": ["wrong type"]}])
def test_native_response_identity_or_schema_mismatch_is_uncertain(api, change):
    api.page.update(change)
    assert execute(api)[2]["state"] == "uncertain"
    assert len(api.views) == 1


def test_current_actor_change_after_native_view_is_uncertain_without_second_show(api):
    def change():
        api.actor = 8
        api.enrollments[0]["user_id"] = 8
    api.after_view = change
    assert execute(api)[2]["state"] == "uncertain" and len(api.views) == 1


@pytest.mark.parametrize("lock,info", [(True, None), (False, {}), (True, {"unlock_at": "2027-01-01T00:00:00Z"})])
def test_locked_native_form_suppresses_all_content_and_reports_incomplete(api, lock, info):
    api.page.update(locked_for_user=lock, lock_info=info, body="SHOULD_NOT_EXPOSE_LOCKED_CONTENT",
                    block_editor_data={"secret": "HIDDEN"})
    result = execute(api)[2]
    assert result["state"] == "succeeded"
    data = result["result"]
    assert data["access"]["status"] == "locked" and data["content"] == {}
    assert data["content_complete"] is False and data["coverage"]["complete"] is False
    assert "SHOULD_NOT" not in json.dumps(data) and len(api.views) == 1


def test_missing_native_lock_state_does_not_expose_assumed_accessible_body(api):
    api.page.pop("locked_for_user")
    data = execute(api)[2]["result"]
    assert data["access"]["status"] == "unavailable" and data["content"] == {}
    assert data["content_complete"] is False


@pytest.mark.parametrize("body,complete", [("", True), (None, False)])
def test_empty_html_and_null_body_are_distinguished(api, body, complete):
    api.page["body"] = body
    data = execute(api)[2]["result"]
    assert data["content"]["body"] == body and data["content_complete"] is complete


@pytest.mark.parametrize("key", ["block_editor_data", "block_editor_attributes"])
def test_null_optional_block_field_does_not_suppress_real_html(api, key):
    api.page[key] = None
    data = execute(api)[2]["result"]
    assert data["content_format"] == "html" and data["content_complete"] is True


@pytest.mark.parametrize("key,content_format,payload", [
    ("block_editor_data", "external_block_data", {"blocks": [{"type": "paragraph", "data": {"text": "Native external text"}}]}),
    ("block_editor_attributes", "block_editor_attributes", {"id": 55, "version": "0.2", "blocks": [{"type": "paragraph", "data": {"text": "Native local text"}}]})])
def test_native_block_content_is_returned_without_passive_html_or_horizon_gate(api, key, content_format, payload):
    api.course["horizon_course"] = True
    api.metadata["editor"] = "block_editor"
    api.page.pop("body")
    api.page[key] = payload
    data = execute(api)[2]["result"]
    assert data["content_format"] == content_format and data["content"][key] == payload
    assert data["content_complete"] is True and data["coverage"]["rendered_page_complete"] is False
    assert len(api.views) == 1


def test_unpublished_group_page_with_native_read_rights_is_not_invented_denial(api):
    api.metadata["published"] = api.page["published"] = False
    result = execute(api, GROUP)[2]
    assert result["state"] == "succeeded" and result["result"]["content_complete"] is True


def test_recursive_block_html_links_and_storage_capabilities_are_handled(api):
    api.page.pop("body")
    api.page["block_editor_data"] = {"blocks": [
        {"type": "paragraph", "data": {"text": '<script>BAD_SCRIPT</script><a onclick="BAD_EVENT" href="https://docs.google.com/document/d/doc/edit?token=PRIVATE_QUERY">Read Google</a>'}},
        {"type": "link", "data": {"url": "https://school.sharepoint.com/sites/class/lesson?access_token=PRIVATE_MS"}},
        {"type": "image", "data": {"url": "https://opaque.example/PRIVATE_CDN_PATH", "signature": "PRIVATE_SIGNATURE"}},
        {"data": {"text": 'Read https://example.org/lesson and <a href="/courses/11/files/45/download?verifier=PRIVATE_FILE">Canvas file</a>'}},
    ]}
    data = execute(api)[2]["result"]
    encoded = json.dumps(data)
    assert not any(value in encoded for value in ["PRIVATE_", "BAD_SCRIPT", "BAD_EVENT"])
    assert "Read Google" in encoded and "https://example.org/lesson" in encoded
    assert {link["provider"] for link in data["links"]} >= {"google_drive", "microsoft", "canvas", "external"}
    assert "https://school.example/courses/11/files/45" in encoded
    assert data["content_redacted"] is True and data["coverage"]["complete"] is False


def test_prepare_metadata_is_sanitized_without_native_view(api):
    api.metadata["lock_explanation"] = '<a href="https://cdn.example/PRIVATE_PATH?token=PRIVATE_TOKEN">Locked item</a>'
    target = p.snapshot(api.canvas, COURSE)
    assert "PRIVATE_" not in json.dumps(target) and target["metadata_redacted"] is True
    assert not api.views


@pytest.mark.parametrize("payload", [
    {"text": '<a href="https://example.org/resource#access_token=PRIVATE_SENTINEL">Resource</a>'},
    {"type": "image", "value": "/files/1/download?verifier=PRIVATE_SENTINEL"},
    {"url": "data:text/plain;base64,PRIVATE_SENTINEL"},
    {"type": "image", "value": "data:text/plain;base64,PRIVATE_SENTINEL"},
    {"text": '<div data-options=\'{"access_token":"PRIVATE_SENTINEL"}\'>Useful text</div>'},
    {"blocks": '{"credential":"PRIVATE_SENTINEL","text":"Useful serialized content"}'},
    {"type": {"unexpected": True}, "refresh_token": "PRIVATE_SENTINEL", "text": "Other content"},
])
def test_arbitrary_native_block_fields_cannot_bypass_secret_projection(api, payload):
    api.page.pop("body")
    api.page["block_editor_data"] = payload
    data = execute(api)[2]["result"]
    assert "PRIVATE_SENTINEL" not in json.dumps(data)
    assert data["content_redacted"] is True and data["content_complete"] is False


@pytest.mark.parametrize("key", ["refresh_token", "id_token", "client_secret", "Authorization", "X-Amz-Credential"])
@pytest.mark.parametrize("separator", ["?", "#"])
def test_secret_query_and_fragment_names_share_recursive_field_policy(api, key, separator):
    api.page["block_editor_data"] = {"url": f"https://docs.google.com/document/d/example/edit{separator}{key}=PRIVATE_SENTINEL"}
    data = execute(api)[2]["result"]
    assert "PRIVATE_SENTINEL" not in json.dumps(data)
    assert any(link["provider"] == "google_drive" for link in data["links"])


def test_ordinary_google_microsoft_and_instructional_links_remain_usable(api):
    api.page["block_editor_data"] = {"text": "Read https://docs.google.com/document/d/example/edit and https://onedrive.live.com/view.aspx?id=lesson and https://example.org/instructions"}
    data = execute(api)[2]["result"]
    assert data["content_complete"] is True
    assert {link["provider"] for link in data["links"]} == {"google_drive", "microsoft", "external"}


@pytest.mark.parametrize("link", [
    '<https://school.example/api/v1/courses/11/pages?sort=title&order=asc&per_page=100&page=2&include%5B%5D=body>; rel="next"',
    '<https://school.example/api/v1/courses/11/pages/page_id:12>; rel="next"'])
def test_metadata_pagination_cannot_dispatch_native_view_or_include_body(api, link):
    api.page_link = link
    with pytest.raises(Exception):
        p.snapshot(api.canvas, COURSE)
    assert not api.views
    assert len([request for request in api.calls if request.url.path.endswith("/pages")]) == 1


def test_enrollment_pagination_cannot_change_current_student_filter(api):
    api.enrollment_link = '<https://school.example/api/v1/courses/11/enrollments?user_id=8&type%5B%5D=StudentEnrollment&state%5B%5D=active&state%5B%5D=completed&per_page=100&page=2>; rel="next"'
    with pytest.raises(ValueError):
        p.snapshot(api.canvas, COURSE)
    assert not api.views
    assert len([request for request in api.calls if request.url.path.endswith("/enrollments")]) == 1


def test_native_bookmark_enrollment_pagination_preserves_scope(api):
    query = httpx.QueryParams([("user_id", 7), ("type[]", "StudentEnrollment"), ("state[]", "active"),
                              ("state[]", "completed"), ("per_page", 100), ("page", 'bookmark:{"id":22}')])
    api.enrollment_link = f'<https://school.example/api/v1/courses/11/enrollments?{query}>; rel="next"'
    api.enrollment_next_rows = [{**api.enrollments[0], "id": 23}]
    target = p.snapshot(api.canvas, COURSE)
    assert [row["id"] for row in target["enrollments"]] == [22, 23]
    assert not api.views


@pytest.mark.parametrize("body", ["x" * (1024 * 1024 + 1), {"blocks": list(range(10001))}])
def test_native_response_bounds_do_not_trigger_second_show(api, body):
    if isinstance(body, str):
        api.page["body"] = body
    else:
        api.page["block_editor_data"] = body
    assert execute(api)[2]["state"] == "uncertain" and len(api.views) == 1


@pytest.mark.parametrize("locked", [False, True])
def test_actual_mcp_review_permission_group_native_content_coverage_and_replay(api, locked):
    api.page["locked_for_user"] = locked
    mcp = FastMCP("native-page-test")
    auth = []
    register_tools(mcp, lambda: api.canvas, lambda: auth.append(True), owner=lambda: "alice")

    async def check():
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        assert tools["canvas_view_page"].annotations.readOnlyHint is False
        prepared = (await mcp.call_tool("canvas_prepare_action", {"action": COURSE}))[1]["data"]
        assert not api.views and not auth
        wrong = (await mcp.call_tool("canvas_update_profile", {"action_id": prepared["action_id"]}))[1]
        assert wrong["status"] == "invalid_request" and not api.views
        result = (await mcp.call_tool("canvas_view_page", {"action_id": prepared["action_id"]}))[1]
        assert result["status"] == "succeeded"
        assert result["coverage"]["complete"] is (not locked)
        assert result["coverage"]["progress_verified"] is False
        assert result["source"]["content_is_untrusted"] is True
        if not locked:
            assert result["data"]["result"]["content"]["body"] == "Useful native content"
        replay = (await mcp.call_tool("canvas_view_page", {"action_id": prepared["action_id"]}))[1]
        assert replay["data"] == result["data"] and len(api.views) == 1
    asyncio.run(check())


def test_actual_mcp_write_authorization_denies_native_show(api):
    def refuse():
        raise ValueError("Writes disabled")
    mcp = FastMCP("native-page-denied")
    register_tools(mcp, lambda: api.canvas, refuse, owner=lambda: "alice")

    async def check():
        prepared = (await mcp.call_tool("canvas_prepare_action", {"action": COURSE}))[1]["data"]
        result = (await mcp.call_tool("canvas_view_page", {"action_id": prepared["action_id"]}))[1]
        assert result["status"] == "invalid_request" and not api.views
    asyncio.run(check())
