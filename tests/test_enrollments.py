"""Course invitations use current-self reads and exact single-student writes."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from canvas_connector.actions import ActionStore
from canvas_connector import enrollments as e
from canvas_connector.tools import register_tools


def row(**changes):
    return {'id': 10, 'user_id': 7, 'course_id': 4, 'course_section_id': 6, 'root_account_id': 1,
            'type': 'StudentEnrollment', 'enrollment_state': 'invited', 'role': 'StudentEnrollment',
            'role_id': 3, 'start_at': None, 'end_at': None, 'updated_at': '2026-01-01T00:00:00Z',
            'associated_user_id': None, **changes}


def action(kind='course_invitation_accept', **changes):
    return {'kind': kind, 'course_id': 4, 'enrollment_id': 10, **changes}


class API:
    def __init__(self):
        self.rows = [row()]
        self.actor = 7
        self.calls = []
        self.post_status = 200
        self.success = True
        self.post_changes = {}
        self.post_actor = None
        self.skip_update = False
        self.hide_after = False
        self.paginate = False
        self.bad_list = None

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        assert request.url.host == 'school.example'
        if request.method == 'GET' and path == '/api/v1/users/self/profile':
            return httpx.Response(200, json={'id': self.actor})
        if request.method == 'GET' and path == '/api/v1/users/self/enrollments':
            assert request.url.params.get_list('type[]') == ['StudentEnrollment']
            assert set(request.url.params.get_list('state[]')) == {'current_future_and_restricted', 'completed', 'rejected'}
            assert request.url.params['per_page'] == '100'
            if self.bad_list is not None:
                return httpx.Response(200, json=self.bad_list)
            if self.hide_after and self.writes:
                return httpx.Response(403, json={'error': 'denied'})
            rows = copy.deepcopy(self.rows)
            if self.paginate and 'page' not in request.url.params:
                return httpx.Response(200, json=rows[:1], headers={'Link': f'<{request.url}&page=2>; rel="next"'})
            return httpx.Response(200, json=rows[1:] if self.paginate else rows)
        if request.method == 'POST' and path in {'/api/v1/courses/4/enrollments/10/accept', '/api/v1/courses/4/enrollments/10/reject'}:
            assert not request.content
            if self.post_status != 200:
                return httpx.Response(self.post_status, json={'error': 'denied'})
            if not self.skip_update:
                self.rows[0]['enrollment_state'] = 'active' if path.endswith('/accept') else 'rejected'
            self.rows[0].update(self.post_changes)
            if self.post_actor is not None:
                self.actor = self.post_actor
            return httpx.Response(200, json={'success': self.success})
        raise AssertionError(f'Unexpected request {request.method} {path}')

    @property
    def writes(self):
        return [r for r in self.calls if r.method != 'GET']


@pytest.fixture
def api():
    api = API()
    with Canvas(CanvasConfig('https://school.example', 'synthetic-token'),
                http_client=httpx.Client(transport=httpx.MockTransport(api.handle))) as canvas:
        api.canvas = canvas
        yield api


def test_discovery_covers_future_invitations_and_follows_pagination(api):
    api.rows = [row(id=11, enrollment_state='active'), row(), row(id=12, course_id=8), row(id=13, enrollment_state='creation_pending')]
    api.paginate = True
    result = e.list_invitations(api.canvas, limit=1)
    assert result['data']['invitations'][0]['id'] == 10
    assert result['coverage']['complete'] is False and result['coverage']['next_offset'] == 1
    assert e.list_invitations(api.canvas, offset=1)['data']['invitations'][0]['id'] == 12
    assert e.list_invitations(api.canvas, state='creation_pending')['data']['invitations'][0]['id'] == 13
    assert not api.writes


@pytest.mark.parametrize('change', [{'id': True}, {'user_id': 9}, {'user_id': True}, {'course_id': '4'}, {'type': 'TeacherEnrollment'}, {'enrollment_state': None}])
def test_discovery_rejects_wrong_identity_or_schema(api, change):
    api.rows = [row(**change)]
    with pytest.raises(ValueError): e.list_invitations(api.canvas)
    assert not api.writes


def test_discovery_rejects_duplicates_and_overflow(api):
    api.rows = [row(), row()]
    with pytest.raises(ValueError, match='duplicate'): e.list_invitations(api.canvas)
    api.rows = [row(id=i+1) for i in range(1001)]
    with pytest.raises(ValueError, match='1000'): e.list_invitations(api.canvas)
    assert not api.writes


@pytest.mark.parametrize('arguments', [{'state': 'deleted'}, {'offset': -1}, {'limit': 101}])
def test_invalid_discovery_never_fetches(api, arguments):
    with pytest.raises(ValueError): e.list_invitations(api.canvas, **arguments)
    assert not api.calls


@pytest.mark.parametrize('change', [{'course_id': True}, {'enrollment_id': '10'}, {'enrollment_id': 0}, {'user_id': 8}, {'kind': 'enrollment_delete'}])
def test_action_model_prevents_broad_or_ambiguous_targets(change):
    with pytest.raises(ValidationError): e._ADAPTER.validate_python(action(**change))


@pytest.mark.parametrize('kind,before,after', [('course_invitation_accept', 'invited', 'active'),
    ('course_invitation_accept', 'rejected', 'active'), ('course_invitation_decline', 'invited', 'rejected')])
def test_prepared_action_changes_exact_invitation_and_reuses_receipt(api, kind, before, after):
    api.rows[0]['enrollment_state'] = before
    store = ActionStore()
    prepared = store.prepare(api.canvas, 'owner', action(kind))
    assert prepared['execute']['tool'] == 'canvas_respond_to_invitation'
    assert not api.writes
    result = store.execute(api.canvas, 'owner', prepared['action_id'], lambda: None)
    assert result['state'] == 'succeeded'
    assert result['result']['enrollment_state'] == after and result['result']['verified'] is True
    assert result['result']['submitted'] is False
    assert len(api.writes) == 1
    assert store.execute(api.canvas, 'owner', prepared['action_id'], lambda: None)['state'] == 'succeeded'
    assert len(api.writes) == 1


@pytest.mark.parametrize('change', [{'enrollment_state': 'active'}, {'enrollment_state': 'creation_pending'},
    {'enrollment_state': 'inactive'}, {'enrollment_state': 'deleted'}, {'self_enrolled': True},
    {'temporary_enrollment_pairing_id': 12}, {'associated_user_id': 8}, {'permissions': {'accept': False}},
    {'course_id': 8}, {'id': 11}])
def test_ineligible_invitation_never_prepares_or_writes(api, change):
    api.rows[0].update(change)
    with pytest.raises(ValueError): ActionStore().prepare(api.canvas, 'owner', action())
    assert not api.writes


@pytest.mark.parametrize('change', [{'enrollment_state': 'active'}, {'course_section_id': 8}, {'role_id': 9}, {'end_at': '2026-06-01'}, {'permissions': {'accept': False}}])
def test_prepared_target_changes_block_dispatch(api, change):
    store = ActionStore()
    prepared = store.prepare(api.canvas, 'owner', action())
    api.rows[0].update(change)
    result = store.execute(api.canvas, 'owner', prepared['action_id'], lambda: None)
    assert result['state'] == 'rejected' and not api.writes


@pytest.mark.parametrize('setting,value', [('success', False), ('success', 1), ('skip_update', True), ('hide_after', True),
    ('post_actor', 8), ('post_changes', {'course_id': 9}), ('post_changes', {'user_id': 8}), ('post_changes', {'role_id': 8}), ('post_status', 403)])
def test_unconfirmed_dispatch_stays_uncertain_without_retry(api, setting, value):
    store = ActionStore()
    prepared = store.prepare(api.canvas, 'owner', action())
    setattr(api, setting, value)
    result = store.execute(api.canvas, 'owner', prepared['action_id'], lambda: None)
    assert result['state'] == 'uncertain'
    assert store.execute(api.canvas, 'owner', prepared['action_id'], lambda: None)['state'] == 'uncertain'
    assert len(api.writes) == 1


def test_mcp_discovery_and_executor_are_separate_permission_group(api):
    mcp = FastMCP('invitations')
    register_tools(mcp, lambda: api.canvas, lambda: None, owner=lambda: 'owner')
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    def call(name, **kwargs): return asyncio.run(tools[name].fn(**kwargs))
    assert tools['canvas_course_invitations'].annotations.readOnlyHint is True
    assert tools['canvas_respond_to_invitation'].annotations.destructiveHint is True
    assert call('canvas_course_invitations')['data']['invitations'][0]['id'] == 10
    receipt = call('canvas_prepare_action', action=action())['data']
    assert call('canvas_update_preferences', action_id=receipt['action_id'])['status'] == 'invalid_request'
    assert not api.writes
    result = call('canvas_respond_to_invitation', action_id=receipt['action_id'])
    assert result['status'] == 'succeeded' and result['coverage']['verification'] is True
    assert len(api.writes) == 1
    assert 'synthetic-token' not in json.dumps(result)
