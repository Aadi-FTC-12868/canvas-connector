"""Appointments use real Canvas transports with synthetic HTTP only."""
import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import ValidationError

from canvas_connector import appointments as ap


class AppointmentHTTP:
    def __init__(self, group=False):
        self.group_mode = group
        self.profile = {"id": 7}
        self.course = {"id": 1, "workflow_state": "available"}
        self.enrollments = [{"id": 9, "user_id": 7, "course_id": 1, "course_section_id": 8,
                             "type": "StudentEnrollment", "enrollment_state": "active"}]
        self.group = {"id": 3, "title": "Office hours", "description": "Choose a slot", "location_name": "Room 2",
            "location_address": None, "start_at": "2000-01-01T10:00:00Z", "end_at": "2099-01-02T11:00:00Z",
            "workflow_state": "active", "context_codes": ["course_1"],
            "sub_context_codes": ["group_category_30"] if group else [],
            "participant_type": "Group" if group else "User", "participant_visibility": "private",
            "appointments_count": 3, "max_appointments_per_participant": 2,
            "min_appointments_per_participant": 1, "participants_per_appointment": 2}
        self.participant = 22 if group else 7
        self.owner_code = f'group_{self.participant}' if group else 'user_7'
        self.student_group = {"id": 22, "name": "Lab team", "course_id": 1, "context_type": "Course", "group_category_id": 30,
                              "concluded": False, "non_collaborative": False}
        self.own_groups = [self.student_group]
        self.member = {"id": 23, "user_id": 7, "group_id": 22, "workflow_state": "accepted"}
        self.permissions = {"read": True, "manage_calendar": True}
        self.slots = {i: {"id": i, "appointment_group_id": 3, "parent_event_id": None,
            "title": "Office hours", "location_name": "Room 2", "location_address": None,
            "start_at": date+'T10:00:00Z', "end_at": date+'T11:00:00Z',
            "participant_type": self.group["participant_type"], "context_code": "course_1"}
            for i, date in [(10, '2099-01-01'), (11, '2099-01-02'), (12, '2000-01-01')]}
        self.records, self.calls, self.writes = {}, [], []
        self.other_counts = {}
        self.capacities = {}
        self.errors = {}
        self.transforms = {}
        self.links = {}
        self.page_size = 100
        self.write_status = None
        self.ignore_write = False
        self.lose_response = False
        self.after_write = None
        self.before_write = None
        self.stale_inventory = False
        self.inventory_before = None
        self.catalog_override = None
        self.next_id = 1000
        self.canvas = Canvas(CanvasConfig('https://school.example', 'synthetic-token'),
            http_client=httpx.Client(transport=httpx.MockTransport(self.handle)))

    def reserve(self, slot_id, record_id=100):
        slot = self.slots[slot_id]
        row = {"id": record_id, "parent_event_id": slot_id, "appointment_group_id": 3, "context_code": self.owner_code,
               "participant_type": self.group['participant_type'], "workflow_state": "locked", "start_at": slot['start_at'],
               "end_at": slot['end_at'], "comments": None, "child_events_count": 0, "series_uuid": None, "rrule": None}
        self.records[record_id] = row
        return row

    def slot(self, slot_id, records=None):
        row = copy.deepcopy(self.slots[slot_id])
        records = self.records if records is None else records
        own = [r for r in records.values() if r['parent_event_id'] == slot_id and r['workflow_state'] != 'deleted']
        count = len(own) + self.other_counts.get(slot_id, 0)
        cap = self.capacities.get(slot_id, self.group['participants_per_appointment'])
        row.update(workflow_state='locked' if count else 'active', reserved=bool(own), child_events_count=count,
            reserve_url=f'https://school.example/api/v1/calendar_events/{slot_id}/reservations/{self.participant}',
            child_events=[{**r, 'own_reservation': True,
                ('group' if self.group_mode else 'user'): {'id': self.participant}} for r in own])
        if cap is not None:
            row.update(participants_per_appointment=cap, available_slots=max(cap-count, 0))
        return row

    def handle(self, req):
        self.calls.append(req)
        assert req.url.host == 'school.example'
        assert req.headers['authorization'] == 'Bearer synthetic-token'
        path = req.url.path.removeprefix('/api/v1/')
        if req.method != 'GET':
            self.writes.append(req)
            if self.write_status:
                return httpx.Response(self.write_status, json={'error': 'synthetic'}, headers={'location': str(req.url), 'retry-after': '0'})
            if self.before_write: self.before_write(self)
            self.inventory_before = copy.deepcopy(self.records)
            body = json.loads(req.content) if req.content else {}
            if req.method == 'POST':
                slot_id = int(path.split('/')[1])
                assert path == f'calendar_events/{slot_id}/reservations'
                assert body['participant_id'] == self.participant
                assert type(body['cancel_existing']) is bool
                if not self.ignore_write:
                    if body['cancel_existing']:
                        for row in self.records.values(): row['workflow_state'] = 'deleted'
                    row = self.reserve(slot_id, self.next_id)
                    row['comments'] = body.get('comments')
                else:
                    row = {'id': self.next_id, 'parent_event_id': slot_id, 'appointment_group_id': 3}
                self.next_id += 1
            else:
                assert req.method == 'DELETE'
                record_id = int(path.rsplit('/', 1)[1])
                assert record_id in self.records and record_id not in self.slots
                row = self.records[record_id]
                if not self.ignore_write: row['workflow_state'] = 'deleted'
            response = copy.deepcopy(row)
            # Normal mutation response need not contain own_reservation or participant JSON.
            response.pop('own_reservation', None)
            response['context_code'] = 'course_1'
            if self.after_write: self.after_write(self)
            if self.lose_response: raise httpx.ReadTimeout('synthetic lost response', request=req)
            return httpx.Response(200, json=response)
        assert not req.content
        if path in self.errors:
            return httpx.Response(self.errors[path], json={'error': 'private-upstream-body'})
        if path == 'users/self/profile': data = self.profile
        elif path == 'courses/1': data = self.course
        elif path == 'courses/1/enrollments':
            assert req.url.params['user_id'] == str(self.profile['id'])
            assert req.url.params.get_list('state[]') == ['active']
            data = self.enrollments
        elif path == 'appointment_groups':
            assert req.url.params['scope'] == 'reservable'
            assert req.url.params.get_list('context_codes[]') == ['course_1']
            data = self.catalog_override if self.catalog_override is not None else [self.group]
        elif path == 'appointment_groups/3':
            # Source behavior: nested slots omit past slots and have no full own child data.
            data = {**self.group, 'appointments': [self.slot(10), self.slot(11)], 'reserved_times': []}
        elif path == 'calendar_events':
            assert req.url.params.get_list('context_codes[]') == ['appointment_group_3']
            assert req.url.params['start_date'] == self.group['start_at']
            assert req.url.params['end_date'] == self.group['end_at']
            assert 'all_events' not in req.url.params
            records = self.inventory_before if self.stale_inventory and self.writes else self.records
            data = [self.slot(i, records) for i in self.slots]
        elif path == 'users/self/groups': data = self.own_groups
        elif path == 'groups/22': data = self.student_group
        elif path == 'groups/22/users/self': data = self.member
        elif path == 'groups/22/permissions': data = self.permissions
        elif path.startswith('calendar_events/'):
            target = int(path.rsplit('/', 1)[1])
            if target in self.slots:
                data = self.slot(target)
            elif target in self.records:
                assert req.url.params.get_list('excludes[]') == ['child_events']
                data = self.records[target]
            else:
                return httpx.Response(404, json={'error': 'missing'})
        else: raise AssertionError((req.method, path))
        data = copy.deepcopy(data)
        if path in self.transforms: data = self.transforms[path](data)
        if path in self.links:
            return httpx.Response(200, json=data, headers={'Link': self.links[path]})
        if isinstance(data, list):
            start = (int(req.url.params.get('page', 1))-1)*self.page_size
            if start + self.page_size < len(data):
                url = req.url.copy_set_param('page', str(start//self.page_size+2))
                return httpx.Response(200, json=data[start:start+self.page_size], headers={'Link': f'<{url}>; rel="next"'})
            data = data[start:]
        return httpx.Response(200, json=data)


def action(api, kind='appointment_reserve', **changes):
    target = {'slot_id': 10} if kind == 'appointment_reserve' else {'reservation_id': 100}
    if kind == 'appointment_reschedule': target['new_slot_id'] = 11
    return {'kind': kind, 'course_id': 1, 'appointment_group_id': 3,
        **({'participant_group_id': 22} if api.group_mode else {}), **target, **changes}


@pytest.mark.parametrize('group', [False, True])
@pytest.mark.parametrize('kind', ap.KINDS)
def test_exact_reserve_cancel_reschedule_and_independent_ownership_readback(group, kind):
    api = AppointmentHTTP(group)
    if kind != 'appointment_reserve': api.reserve(10)
    p = action(api, kind)
    target = ap.snapshot(api.canvas, p)
    assert not api.writes
    result = ap.perform(api.canvas, p, target)
    assert result['verified'] and not result['attendance_confirmed']
    assert result['coverage']['complete'] is (kind != 'appointment_reschedule')
    assert len(api.writes) == 1
    if kind != 'appointment_cancel':
        assert json.loads(api.writes[0].content) == {'participant_id': api.participant, 'cancel_existing': kind == 'appointment_reschedule'}
        assert result['reservation_id'] == 1000
    else: assert result['reservation_id'] is None
    assert result['cancelled_reservation_id'] == (100 if kind != 'appointment_reserve' else None)
    assert all(r.method == 'GET' for r in api.calls[api.calls.index(api.writes[0])+1:])


@pytest.mark.parametrize('group', [False, True])
def test_discovery_is_bounded_includes_past_reservations_and_derives_candidate(group):
    api = AppointmentHTTP(group); api.reserve(12)
    api.page_size = 1
    groups = ap.read_appointment_groups(api.canvas, 1)
    assert groups['data']['appointment_groups'][0]['id'] == 3
    result = ap.read_appointments(api.canvas, 1, 3, limit=1)
    assert len(result['data']['slots']) == 1
    assert result['coverage']['next_offset'] == 1 and not result['coverage']['complete']
    assert result['data']['own_reservations'][0]['id'] == 100
    assert result['data']['participant']['id'] == api.participant
    assert result['data']['inventory_complete'] is True
    assert not api.writes


@pytest.mark.parametrize('field', ['course_id', 'appointment_group_id', 'slot_id', 'participant_group_id'])
@pytest.mark.parametrize('value', [True, 0, -1, '1', 1.0, 2**63])
def test_strict_action_identifiers(field, value):
    api = AppointmentHTTP()
    with pytest.raises(ValidationError): ap.snapshot(api.canvas, action(api, **{field: value}))
    assert not api.calls


@pytest.mark.parametrize('change', [{'user_id': 5}, {'participant_id': 5}, {'cancel_existing': True}, {'comments': 'x'*256}, {'comments': 1}])
def test_no_actor_override_or_unbounded_body(change):
    api = AppointmentHTTP()
    with pytest.raises(ValidationError): ap.snapshot(api.canvas, action(api, **change))
    assert not api.calls


@pytest.mark.parametrize('target,change', [
    ('course', {'id': 2}), ('course', {'workflow_state': 'completed'}), ('course', {'access_restricted_by_date': True}),
    ('group', {'workflow_state': 'pending'}), ('group', {'context_codes': ['course_2']}),
    ('group', {'appointments_count': 2}), ('group', {'appointments_count': 1001}),
    ('group', {'max_appointments_per_participant': '2'}), ('group', {'participant_type': 'Teacher'}),
    ('group', {'sub_context_codes': ['course_section_999']}), ('group', {'start_at': '2000-01-01T10:00:00'})])
def test_missing_eligibility_or_complete_inventory_refuses(target, change):
    api = AppointmentHTTP(); getattr(api, target).update(change)
    with pytest.raises(ValueError): ap.snapshot(api.canvas, action(api))
    assert not api.writes


@pytest.mark.parametrize('change', [{'user_id': 8}, {'course_id': 2}, {'enrollment_state': 'completed'}, {'type': 'TeacherEnrollment'}])
def test_not_active_own_student(change):
    api = AppointmentHTTP(); api.enrollments[0].update(change)
    with pytest.raises(ValueError): ap.snapshot(api.canvas, action(api))
    assert not api.writes


@pytest.mark.parametrize('case', ['missing', 'wrong_category', 'wrong_course', 'requested', 'read_denied', 'candidate_mismatch', 'action_omits_group', 'different_action_group'])
def test_group_participant_must_be_discovered_member(case):
    api = AppointmentHTTP(True); p = action(api)
    if case == 'missing': api.own_groups = []
    elif case == 'wrong_category': api.student_group['group_category_id'] = 31
    elif case == 'wrong_course': api.student_group['course_id'] = 2
    elif case == 'requested': api.member['workflow_state'] = 'requested'
    elif case == 'read_denied': api.permissions['read'] = False
    elif case == 'candidate_mismatch': api.participant = 99
    elif case == 'action_omits_group': del p['participant_group_id']
    else: p['participant_group_id'] = 99
    with pytest.raises(ValueError): ap.snapshot(api.canvas, p)
    assert not api.writes


@pytest.mark.parametrize('kind', ['appointment_cancel', 'appointment_reschedule'])
def test_group_calendar_permission_required_for_cancellation(kind):
    api = AppointmentHTTP(True); api.reserve(10); api.permissions['manage_calendar'] = False
    with pytest.raises(ValueError): ap.snapshot(api.canvas, action(api, kind))
    assert not api.writes


def test_group_reserve_does_not_require_permission_to_cancel():
    api = AppointmentHTTP(True); api.permissions['manage_calendar'] = False
    assert ap.snapshot(api.canvas, action(api))['participant']['can_cancel'] is False


@pytest.mark.parametrize('url', ['https://evil.example/api/v1/calendar_events/10/reservations/7',
    'https://school.example/api/v1/calendar_events/11/reservations/7',
    'https://school.example/api/v1/calendar_events/10/reservations/8',
    'https://school.example/api/v1/calendar_events/10/reservations/7?access_token=secret',
    'https://school.example/api/v1/calendar_events/10/reservations/{{id}}'])
def test_reserve_url_is_identity_evidence_never_followed(url):
    api = AppointmentHTTP()
    api.transforms['calendar_events'] = lambda rows: [{**r, 'reserve_url': url} if r['id'] == 10 else r for r in rows]
    with pytest.raises(ValueError): ap.snapshot(api.canvas, action(api))
    assert all(r.url.host == 'school.example' for r in api.calls) and not api.writes


@pytest.mark.parametrize('case', ['full', 'past', 'already_reserved', 'past_limit', 'multiple_replace', 'past_replace'])
def test_capacity_deadline_and_complete_reservation_limits(case):
    api = AppointmentHTTP(); p = action(api)
    if case == 'full': api.other_counts[10] = 2
    elif case == 'past': p['slot_id'] = 12
    elif case == 'already_reserved': api.reserve(10)
    elif case == 'past_limit': api.reserve(12); api.group['max_appointments_per_participant'] = 1
    elif case == 'multiple_replace': api.reserve(10); api.reserve(12, 101); p = action(api, 'appointment_reschedule')
    else: api.reserve(12); p = action(api, 'appointment_reschedule')
    with pytest.raises(ValueError): ap.snapshot(api.canvas, p)
    assert not api.writes


def test_locked_slot_with_capacity_and_unlimited_override_are_reservable():
    api = AppointmentHTTP(); api.other_counts[10] = 1
    assert ap.snapshot(api.canvas, action(api))['slot']['workflow_state'] == 'locked'
    api.capacities[10] = None; api.other_counts[10] = 100
    assert ap.snapshot(api.canvas, action(api))['slot']['available_slots'] is None


def test_slot_end_controls_deadline_and_minimum_does_not_forbid_cancel():
    api = AppointmentHTTP()
    now = datetime.now(UTC)
    api.slots[10].update(start_at=(now-timedelta(minutes=5)).isoformat(), end_at=(now+timedelta(minutes=5)).isoformat())
    assert ap.snapshot(api.canvas, action(api))['slot']['id'] == 10
    api.reserve(10)
    assert ap.snapshot(api.canvas, action(api, 'appointment_cancel'))['below_requested_minimum_after'] is True


def test_regular_reserve_preserves_existing_booking_comments_and_exact_child_delete():
    api = AppointmentHTTP(); api.reserve(10)
    p = action(api, slot_id=11, comments='See you there')
    result = ap.perform(api.canvas, p, ap.snapshot(api.canvas, p))
    assert {r['id'] for r in result['own_reservations']} == {100, 1000}
    assert api.records[1000]['comments'] == 'See you there'
    p = action(api, 'appointment_cancel', cancel_reason='Schedule changed')
    ap.perform(api.canvas, p, ap.snapshot(api.canvas, p))
    assert json.loads(api.writes[-1].content) == {'cancel_reason': 'Schedule changed'}
    assert api.records[1000]['workflow_state'] == 'locked'


@pytest.mark.parametrize('field,value', [('id', True), ('parent_event_id', 99), ('context_code', 'user_99'),
    ('participant_type', 'Group'), ('series_uuid', 'series'), ('child_events_count', 1)])
def test_cancel_binds_child_parent_owner_and_no_recursive_or_series_delete(field, value):
    api = AppointmentHTTP(); api.reserve(10)[field] = value
    with pytest.raises(ValueError): ap.snapshot(api.canvas, action(api, 'appointment_cancel'))
    assert not api.writes


@pytest.mark.parametrize('status', [301, 302, 307, 308, 403, 429, 500, 503])
def test_mutation_failure_never_retries_or_redirects(status):
    api = AppointmentHTTP(); p = action(api); target = ap.snapshot(api.canvas, p)
    api.write_status = status
    with pytest.raises((CanvasError, ValueError)): ap.perform(api.canvas, p, target)
    assert len(api.writes) == 1


@pytest.mark.parametrize('case', ['ignored', 'lost', 'stale', 'wrong_actor', 'wrong_owner', 'unrelated_booking', 'read_denied', 'wrong_time'])
def test_independent_readback_required_and_no_retry(case):
    api = AppointmentHTTP(); p = action(api); target = ap.snapshot(api.canvas, p)
    if case == 'ignored': api.ignore_write = True
    elif case == 'lost': api.lose_response = True
    elif case == 'stale': api.stale_inventory = True
    elif case == 'wrong_actor': api.after_write = lambda a: a.profile.update(id=8)
    elif case == 'wrong_owner': api.after_write = lambda a: a.records[1000].update(context_code='user_8')
    elif case == 'unrelated_booking': api.after_write = lambda a: a.reserve(11, 1001)
    elif case == 'read_denied': api.after_write = lambda a: a.errors.update({'calendar_events/1000': 403})
    else: api.after_write = lambda a: a.records[1000].update(end_at='2099-01-01T12:00:00Z')
    with pytest.raises((CanvasError, ValueError)): ap.perform(api.canvas, p, target)
    assert len(api.writes) == 1


def test_delete_404_does_not_prove_cancelled():
    api = AppointmentHTTP(); api.reserve(10); p = action(api, 'appointment_cancel'); target = ap.snapshot(api.canvas, p)
    api.after_write = lambda a: a.errors.update({'calendar_events/100': 404})
    with pytest.raises((CanvasError, ValueError)): ap.perform(api.canvas, p, target)
    assert len(api.writes) == 1


@pytest.mark.parametrize('link', [
    '<https://school.example/api/v1/courses/1/modules/2/items/3>; rel="next"',
    '<https://school.example/api/v1/calendar_events?page=2>; rel="next"',
    '<https://school.example/api/v1/calendar_events?type=event&context_codes%5B%5D=appointment_group_99&start_date=2000-01-01T10%3A00%3A00Z&end_date=2099-01-02T11%3A00%3A00Z&page=2>; rel="next"',
    '<https://school.example/api/v1/calendar_events?page=2>; rel=next',
])
def test_collection_path_and_semantic_query_cannot_change_before_dispatch(link):
    api = AppointmentHTTP(); api.links['calendar_events'] = link
    with pytest.raises((CanvasError, ValueError)): ap.snapshot(api.canvas, action(api))
    assert len([r for r in api.calls if r.url.path == '/api/v1/calendar_events']) == 1
    assert not any('/modules/' in r.url.path or r.url.params.get('page') == '2' for r in api.calls)
    assert not api.writes


@pytest.mark.parametrize('path', ['appointment_groups', 'calendar_events', 'calendar_events/10'])
def test_read_redirect_is_rejected_before_side_effect_capable_get(path):
    api = AppointmentHTTP(); original = api.handle
    def handler(req):
        if req.url.path == '/api/v1/'+path:
            api.calls.append(req)
            return httpx.Response(302, headers={'Location': 'https://school.example/api/v1/courses/1/modules/2/items/3'})
        return original(req)
    api.canvas = Canvas(CanvasConfig('https://school.example', 'synthetic-token'),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(CanvasError): ap.snapshot(api.canvas, action(api))
    assert not any('/modules/' in r.url.path for r in api.calls) and not api.writes


@pytest.mark.parametrize('case', ['missing_own_flag', 'wrong_user', 'missing_children', 'capacity', 'bounds', 'missing_id'])
def test_unknown_or_inconsistent_slot_and_child_state_is_not_empty(case):
    api = AppointmentHTTP(); api.reserve(10)
    def transform(rows):
        first = rows[0]
        if case == 'missing_own_flag': first['child_events'][0].pop('own_reservation')
        elif case == 'wrong_user': first['child_events'][0]['user']['id'] = 8
        elif case == 'missing_children': first.pop('child_events')
        elif case == 'capacity': first['available_slots'] = 2
        elif case == 'bounds': rows[2]['start_at'] = '1999-01-01T10:00:00Z'
        else: first.pop('id')
        return rows
    api.transforms['calendar_events'] = transform
    with pytest.raises(ValueError): ap.read_appointments(api.canvas, 1, 3)
    assert not api.writes


@pytest.mark.parametrize('group', [False, True])
def test_verified_empty_group_is_readable_but_not_a_bookable_slot(group):
    api = AppointmentHTTP(group); api.slots = {}; api.group['appointments_count'] = 0
    api.group.update(start_at=None, end_at=None)
    # Group SHOW's future nested appointments have no bearing on the empty inventory.
    original = api.handle
    def handler(req):
        if req.url.path == '/api/v1/appointment_groups/3':
            api.calls.append(req)
            return httpx.Response(200, json={**api.group, 'appointments': []})
        return original(req)
    api.canvas = Canvas(CanvasConfig('https://school.example', 'synthetic-token'),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = ap.read_appointments(api.canvas, 1, 3)
    assert result['data']['slots'] == [] and result['data']['own_reservations'] == []
    assert result['coverage']['complete']
    with pytest.raises(ValueError): ap.snapshot(api.canvas, action(api))
    assert not api.writes


@pytest.mark.parametrize('provided,expected', [(None, 'Existing appointment notes'), ('', ''), ('New notes', 'New notes')])
def test_reschedule_preserves_comments_unless_explicitly_replaced(provided, expected):
    api = AppointmentHTTP(); api.reserve(10)['comments'] = 'Existing appointment notes'
    p = action(api, 'appointment_reschedule', **({'comments': provided} if provided is not None else {}))
    review = ap.snapshot(api.canvas, p)
    assert review['reservation_comments'] == expected
    result = ap.perform(api.canvas, p, review)
    assert json.loads(api.writes[0].content)['comments'] == expected
    assert result['own_reservations'][0]['comments'] == expected


@pytest.mark.parametrize('change', ['skip_page', 'drop_page', 'change_size', 'drop_size', 'restart', 'arbitrary'])
def test_native_size_catalog_cannot_skip_groups_and_falsely_report_complete(change):
    api = AppointmentHTTP()
    api.catalog_override = [{**copy.deepcopy(api.group), 'id': i, 'title': f'Group {i}'} for i in range(1, 201)]
    original = api.handle
    def handler(req):
        response = original(req)
        if req.url.path == '/api/v1/appointment_groups' and req.url.params.get('page') is None:
            next_url = req.url.copy_set_param('page', '2')
            if change == 'skip_page': next_url = next_url.copy_set_param('page', '3')
            elif change == 'drop_page': next_url = next_url.copy_remove_param('page')
            elif change == 'change_size': next_url = next_url.copy_set_param('per_page', '50')
            elif change == 'drop_size': next_url = next_url.copy_remove_param('per_page')
            elif change == 'restart': next_url = next_url.copy_set_param('page', 'first')
            else: next_url = next_url.copy_set_param('page', 'opaque-unrecognized-token')
            response.headers['Link'] = f'<{next_url}>; rel="next"'
        return response
    api.canvas = Canvas(CanvasConfig('https://school.example', 'synthetic-token'),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError): ap.read_appointment_groups(api.canvas, 1, limit=100)
    assert len([r for r in api.calls if r.url.path == '/api/v1/appointment_groups']) == 1
    assert not api.writes


def test_native_bookmark_catalog_cursor_is_preserved_and_complete():
    api = AppointmentHTTP()
    api.catalog_override = [{**copy.deepcopy(api.group), 'id': i, 'title': f'Group {i}'} for i in range(1, 201)]
    original = api.handle
    token = 'bookmark:synthetic-source-compatible-opaque-cursor'
    def handler(req):
        if req.url.path == '/api/v1/appointment_groups' and req.url.params.get('page') == token:
            api.calls.append(req)
            return httpx.Response(200, json=api.catalog_override[100:])
        response = original(req)
        if req.url.path == '/api/v1/appointment_groups':
            response.headers['Link'] = f'<{req.url.copy_set_param("page", token)}>; rel="next"'
        return response
    api.canvas = Canvas(CanvasConfig('https://school.example', 'synthetic-token'),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = ap.read_appointment_groups(api.canvas, 1, offset=100, limit=100)
    assert [r['id'] for r in result['data']['appointment_groups']] == list(range(101, 201))
    assert result['coverage']['total'] == 200
    assert result['coverage']['next_offset'] is None
    assert not result['coverage']['complete']  # This response is a window starting at100.
    assert len([r for r in api.calls if r.url.path == '/api/v1/appointment_groups']) == 2
    assert not api.writes
