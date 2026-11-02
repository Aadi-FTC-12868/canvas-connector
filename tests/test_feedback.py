"""Feedback markers use own fixed GraphQL reads, never mutation-bearing GET includes."""
import asyncio
import copy
import json

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from canvas_connector import feedback as f
from canvas_connector.actions import ActionStore
from canvas_connector.tools import register_tools


class API:
    def __init__(self):
        self.calls = []
        self.actor = 7
        self.enrollments = [{'id': 9, 'user_id': 7, 'course_id': 4, 'type': 'StudentEnrollment', 'enrollment_state': 'active'}]
        self.assignment = {'id': 12, 'course_id': 4, 'name': 'Paper', 'published': True}
        self.submission = {'id': 100, 'user_id': 7, 'assignment_id': 12, 'attempt': 1, 'score': 8, 'grade': '8',
            'workflow_state': 'graded', 'assignment_visible': True, 'updated_at': '2026-01-01T00:00:00Z',
            'submission_comments': [{'id': 2, 'author_id': 10, 'comment': 'Review this.', 'created_at': '2026-01-01'}]}
        self.states = {'submission': 'unread', 'rubric': False, 'annotations': False}
        self.graphql_changes = {}
        self.graphql_errors = None
        self.graphql_status = 200
        self.mutation_status = None
        self.skip_update = False
        self.after_changes = {}
        self.hide_after = False
        self.graphql_null = False
        self.marker_override = None
        self.other_unread = set()
        self.fail_write_number = None

    @property
    def writes(self):
        return [r for r in self.calls if r.method in {'PUT', 'DELETE'}]

    def handle(self, request):
        self.calls.append(request)
        assert request.url.host == 'school.example'
        assert request.headers['authorization'] == 'Bearer synthetic-token'
        assert not {'read_state', 'read_status'} & set(request.url.params.get_list('include[]'))
        path = request.url.path
        if request.method == 'GET':
            if path == '/api/v1/users/self/profile':
                return httpx.Response(200, json={'id': self.actor})
            if path.endswith('/enrollments'):
                assert request.url.params['user_id'] == str(self.actor)
                assert request.url.params.get_list('type[]') == ['StudentEnrollment']
                return httpx.Response(200, json=copy.deepcopy(self.enrollments))
            if path == '/api/v1/courses/4/assignments/12':
                return httpx.Response(200, json=copy.deepcopy(self.assignment))
            if path.endswith('/submissions/self'):
                if self.hide_after and self.writes:
                    return httpx.Response(403, json={'error': 'denied'})
                return httpx.Response(200, json=copy.deepcopy(self.submission))
            for part, suffix in f._PATH.items():
                if path.endswith('/' + suffix):
                    return httpx.Response(200, json={'read': self.states[part] if self.marker_override is None else self.marker_override})
        if path == '/api/graphql':
            assert request.method == 'POST'
            body = json.loads(request.content)
            assert body == {'operationName': 'CanvasConnectorFeedback', 'query': f._QUERY, 'variables': {'id': '100'}}
            assert 'mutation' not in body['query'] and body['query'].startswith('query ')
            row = {'_id': '100', 'userId': '7', 'assignmentId': '12', 'readState': 'unread' if self.other_unread else self.states['submission'], **self.graphql_changes}
            return httpx.Response(self.graphql_status, json={'data': {'submission': None if self.graphql_null else row}, 'errors': self.graphql_errors})
        if request.method in {'PUT', 'DELETE'}:
            assert path.startswith('/api/v1/courses/4/assignments/12/submissions/self/')
            assert not request.content
            part = next((k for k, v in f._PATH.items() if path.endswith('/'+v)), 'submission')
            if self.fail_write_number == len(self.writes):
                return httpx.Response(500, json={'error':'failed partway'})
            if self.mutation_status is not None:
                return httpx.Response(self.mutation_status, json={'error': 'rate limit' if self.mutation_status in {403,429} else 'failed'},
                                      headers={'location': 'https://school.example/replay', 'retry-after': '0'})
            if not self.skip_update:
                if '/read/' in path:
                    item = path.rsplit('/', 1)[-1]
                    assert item in {'grade','comment','rubric'} and request.method == 'PUT'
                    if item == 'grade': self.states['submission'] = 'read'
                    else: self.other_unread.discard(item)
                else:
                    self.states[part] = ('read' if request.method == 'PUT' else 'unread') if part == 'submission' else True
            self.submission.update(self.after_changes)
            return httpx.Response(204) if part == 'submission' else httpx.Response(200, json={'read': True})
        raise AssertionError(f'Unexpected {request.method} {path}')


@pytest.fixture
def api():
    a = API()
    with Canvas(CanvasConfig('https://school.example', 'synthetic-token'),
                http_client=httpx.Client(transport=httpx.MockTransport(a.handle))) as canvas:
        a.canvas = canvas
        yield a


def action(kind='submission_feedback_read', **changes):
    return {'kind': kind, 'course_id': 4, 'assignment_id': 12, **({'read': True} if kind == 'submission_feedback_read' else {}), **changes}


@pytest.mark.parametrize('part', ['submission', 'rubric', 'annotations'])
def test_reads_do_not_change_any_marker(api, part):
    before = copy.deepcopy(api.states)
    result = f.read_feedback_state(api.canvas, 4, 12, part)
    assert result['data']['read_state'] == before[part]
    assert result['coverage']['complete'] is True
    assert api.states == before and not api.writes
    assert any(r.url.path == '/api/graphql' for r in api.calls) == (part == 'submission')


@pytest.mark.parametrize('p', [action(), action(read=False), action('rubric_feedback_read'), action('annotation_feedback_read')])
def test_prepare_execute_and_replay_each_exact_marker(api, p):
    api.states['submission'] = 'read' if p.get('read') is False else 'unread'
    store = ActionStore()
    prepared = store.prepare(api.canvas, 'alice', p)
    assert not api.writes
    assert prepared['execute']['tool'] == 'canvas_update_feedback_read_state'
    result = store.execute(api.canvas, 'alice', prepared['action_id'], lambda: None)
    assert result['state'] == 'succeeded' and result['result']['verified']
    assert result['result']['submitted'] is False
    assert result['result']['returned_grade_fields_unchanged']
    assert api.submission['score'] == 8 and api.submission['grade'] == '8'
    count = 3 if p.get('read') is True else 1
    assert len(api.writes) == count
    assert store.execute(api.canvas, 'alice', prepared['action_id'], lambda: None) == result
    assert len(api.writes) == count



@pytest.mark.parametrize('change', [{'course_id': True}, {'assignment_id': 0}, {'read': 1}, {'read': 'true'}, {'user_id': 99}, {'query': 'mutation{}'}, {'course_id': 2**63}])
def test_strict_payload_has_no_query_or_other_user(api, change):
    with pytest.raises(ValidationError): f._ADAPTER.validate_python(action(**change))
    assert not api.calls


@pytest.mark.parametrize('target,change', [('assignment', {'id': 14}), ('assignment', {'course_id': 5}),
    ('submission', {'user_id': 99}), ('submission', {'assignment_id': 13}), ('submission', {'id': True}),
    ('submission', {'assignment_visible': False}), ('submission', {'submission_comments': None})])
def test_wrong_identity_or_incomplete_feedback_refuses_before_write(api, target, change):
    getattr(api, target).update(change)
    with pytest.raises(ValueError): f.snapshot(api.canvas, action())
    assert not api.writes


@pytest.mark.parametrize('change', [{'user_id': 99}, {'course_id': 8}, {'type': 'TeacherEnrollment'}, {'enrollment_state': 'invited'}, {'id': True}])
def test_nonstudent_or_wrong_enrollment_refuses(api, change):
    api.enrollments[0].update(change)
    with pytest.raises(ValueError): f.snapshot(api.canvas, action())
    assert not api.writes


@pytest.mark.parametrize('rows', [[], [{}], [{'id': i+1, 'user_id':7, 'course_id':4,'type':'StudentEnrollment','enrollment_state':'active'} for i in range(1001)]])
def test_missing_or_unbounded_enrollment_refuses(api, rows):
    api.enrollments = rows
    with pytest.raises(ValueError): f.snapshot(api.canvas, action())
    assert not api.writes


@pytest.mark.parametrize('changes', [{'_id': '101'}, {'userId': '8'}, {'assignmentId': '13'}, {'userId': True}, {'_id':'0100'}, {'readState': None}, {'readState': 'maybe'}])
def test_graphql_wrong_identity_or_unknown_read_state_refuses(api, changes):
    api.graphql_changes = changes
    with pytest.raises(ValueError): f.snapshot(api.canvas, action())
    assert not api.writes and api.states['submission'] == 'unread'


@pytest.mark.parametrize('failure', ['errors', 'null', 'forbidden'])
def test_graphql_failure_never_falls_back_to_mutating_get(api, failure):
    if failure == 'errors': api.graphql_errors = [{'message':'private backend detail'}]
    elif failure == 'null': api.graphql_null = True
    else: api.graphql_status = 403
    store = ActionStore()
    with pytest.raises(Exception): store.prepare(api.canvas, 'alice', action())
    assert not api.writes
    assert all('read_state' not in r.url.params.get_list('include[]') for r in api.calls)


@pytest.mark.parametrize('value', ['true', 1, {}, []])
def test_rest_marker_requires_actual_boolean(api, value):
    api.marker_override = value
    with pytest.raises(ValueError): f.snapshot(api.canvas, action('rubric_feedback_read'))
    assert not api.writes


@pytest.mark.parametrize('change', [{'attempt': 2}, {'score': 9}, {'grade': 'A'}, {'submission_comments': []}, {'updated_at': 'new'}])
def test_stale_feedback_or_grade_rejected_before_dispatch(api, change):
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action())
    api.submission.update(change)
    result = store.execute(api.canvas, 'alice', p['action_id'], lambda: None)
    assert result['state'] == 'rejected' and not api.writes


@pytest.mark.parametrize('change', [{'attempt': 2}, {'score': 9}, {'user_id': 8}, {'submission_comments': []}])
def test_feedback_changed_after_dispatch_is_uncertain_without_retry(api, change):
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action())
    api.after_changes = change
    result = store.execute(api.canvas, 'alice', p['action_id'], lambda: None)
    assert result['state'] == 'uncertain'
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'uncertain'
    assert len(api.writes) == 3


@pytest.mark.parametrize('kind', f.KINDS)
def test_unconfirmed_saved_marker_is_uncertain(api, kind):
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action(kind))
    api.skip_update = True
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'uncertain'
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'uncertain'
    assert len(api.writes) == (3 if kind == 'submission_feedback_read' else 1)


@pytest.mark.parametrize('status', [302, 307, 308, 403, 429, 500])
def test_mutation_error_or_redirect_is_not_replayed(api, status):
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action())
    api.mutation_status = status
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'uncertain'
    assert len(api.writes) == 1


def test_denied_postwrite_readback_is_uncertain(api):
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action())
    api.hide_after = True
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'uncertain'
    assert len(api.writes) == 3


def test_updated_timestamp_alone_does_not_invalidate_saved_marker(api):
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action())
    api.after_changes = {'updated_at': 'new'}
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'succeeded'


def test_actual_mcp_boundary_separates_fixed_read_from_execution(api):
    mcp = FastMCP('feedback-test'); register_tools(mcp, lambda: api.canvas, lambda: None, owner=lambda: 'alice')
    async def check():
        listed = {t.name:t for t in await mcp.list_tools()}
        assert listed['canvas_feedback_read_state'].annotations.readOnlyHint
        assert not listed['canvas_update_feedback_read_state'].annotations.readOnlyHint
        result = (await mcp.call_tool('canvas_feedback_read_state', {'course_id':4,'assignment_id':12}))[1]
        assert result['data']['read_state'] == 'unread' and not api.writes
        prepared = (await mcp.call_tool('canvas_prepare_action', {'action': action()}))[1]['data']
        wrong = (await mcp.call_tool('canvas_update_what_if_grades', {'action_id':prepared['action_id']}))[1]
        assert wrong['status'] == 'invalid_request' and not api.writes
        executed = (await mcp.call_tool('canvas_update_feedback_read_state', {'action_id':prepared['action_id']}))[1]
        assert executed['status'] == 'succeeded' and executed['coverage']['complete'] is True
        assert len(api.writes) == 3
    asyncio.run(check())


@pytest.mark.parametrize('comments', [[{}], [{'id': True}], [{'id': 2}, {'id': 2}]])
def test_missing_or_duplicate_feedback_identity_refuses(api, comments):
    api.submission['submission_comments'] = comments
    with pytest.raises(ValueError): f.snapshot(api.canvas, action())
    assert not api.writes


def test_feedback_order_does_not_create_false_stale_refusal(api):
    api.submission['submission_comments'].append({'id':3,'comment':'Also check citations.'})
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action())
    api.submission['submission_comments'].reverse()
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'succeeded'


@pytest.mark.parametrize('number', [1,2,3])
def test_partial_all_item_read_stops_without_replay(api, number):
    api.other_unread = {'comment','rubric'}
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action())
    assert p['review']['target']['operations'] == ['read/grade','read/comment','read/rubric']
    assert 'all visible comments across attempts' in p['review']['target']['effect']
    api.fail_write_number = number
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'uncertain'
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'uncertain'
    assert len(api.writes) == number


def test_all_participation_read_clears_aggregate_but_not_other_preferences(api):
    api.states['submission'] = 'read'
    api.other_unread = {'comment','rubric'}
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action())
    assert p['review']['target']['read_state'] == 'unread'
    result = store.execute(api.canvas, 'alice', p['action_id'], lambda: None)
    assert result['state'] == 'succeeded' and result['result']['read_state'] == 'read'
    assert not api.other_unread
    assert api.states['rubric'] is False and api.states['annotations'] is False
    assert [r.url.path.rsplit('/',1)[-1] for r in api.writes] == ['grade','comment','rubric']


@pytest.mark.parametrize('kind', ['rubric_feedback_read','annotation_feedback_read'])
def test_preference_markers_are_independent_of_aggregate(api, kind):
    api.other_unread = {'comment','rubric'}
    store = ActionStore(); p = store.prepare(api.canvas, 'alice', action(kind))
    assert store.execute(api.canvas, 'alice', p['action_id'], lambda: None)['state'] == 'succeeded'
    assert f.read_feedback_state(api.canvas,4,12)['data']['read_state'] == 'unread'


@pytest.mark.parametrize('published', [None, False, 'true'])
def test_all_item_read_requires_published_assignment(api, published):
    api.assignment['published'] = published
    with pytest.raises(ValueError, match='published'): f.snapshot(api.canvas,action())
    assert not api.writes
