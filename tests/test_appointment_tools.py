"""Real MCP appointment routing, review freshness and durable receipt boundaries."""
import asyncio

import pytest
from mcp.server.fastmcp import FastMCP
from test_appointments import AppointmentHTTP, action

from canvas_connector.actions import ActionStore
from canvas_connector.tools import register_tools


@pytest.mark.parametrize('group', [False, True])
@pytest.mark.parametrize('kind', ['appointment_reserve', 'appointment_cancel', 'appointment_reschedule'])
def test_actual_mcp_discovery_prepare_execute_and_no_replay(group, kind):
    api = AppointmentHTTP(group)
    if kind != 'appointment_reserve': api.reserve(10)
    mcp = FastMCP('appointment-test')
    register_tools(mcp, lambda: api.canvas, lambda: None, owner=lambda: 'alice')
    async def run():
        tools = {t.name:t for t in await mcp.list_tools()}
        for name in ('canvas_appointment_groups', 'canvas_appointments'):
            assert tools[name].annotations.readOnlyHint
        assert not tools['canvas_manage_appointments'].annotations.readOnlyHint
        assert tools['canvas_manage_appointments'].annotations.destructiveHint
        groups = (await mcp.call_tool('canvas_appointment_groups', {'course_id': 1}))[1]
        assert groups['data']['appointment_groups'][0]['id'] == 3
        read = (await mcp.call_tool('canvas_appointments', {'course_id': 1, 'appointment_group_id': 3, 'limit': 1}))[1]
        assert read['data']['participant']['id'] == api.participant
        assert not read['coverage']['complete'] and not api.writes
        prepared = (await mcp.call_tool('canvas_prepare_action', {'action': action(api, kind)}))[1]['data']
        assert prepared['execute']['tool'] == 'canvas_manage_appointments'
        wrong = (await mcp.call_tool('canvas_manage_calendar', {'action_id': prepared['action_id']}))[1]
        assert wrong['status'] == 'invalid_request' and not api.writes
        result = (await mcp.call_tool('canvas_manage_appointments', {'action_id': prepared['action_id']}))[1]
        assert result['status'] == 'succeeded'
        assert result['coverage']['complete'] is (kind != 'appointment_reschedule')
        assert result['data']['result']['verified']
        again = (await mcp.call_tool('canvas_manage_appointments', {'action_id': prepared['action_id']}))[1]
        assert again['data'] == result['data'] and len(api.writes) == 1
    asyncio.run(run())


@pytest.mark.parametrize('case', ['capacity', 'membership', 'reservation', 'time', 'limit', 'comments'])
def test_stale_review_is_rejected_without_dispatch(case):
    api = AppointmentHTTP(case == 'membership')
    api.reserve(10)
    store = ActionStore()
    prepared = store.prepare(api.canvas, 'alice', action(api, 'appointment_reschedule'))
    if case == 'capacity': api.other_counts[11] = 2
    elif case == 'membership': api.member['workflow_state'] = 'requested'
    elif case == 'reservation': api.reserve(12, 101)
    elif case == 'time': api.slots[11]['start_at'] = '2099-01-02T10:15:00Z'
    elif case == 'limit': api.group['max_appointments_per_participant'] = 0
    else: api.records[100]['comments'] = 'Changed after review'
    result = store.execute(api.canvas, 'alice', prepared['action_id'], lambda: None)
    assert result['state'] == 'rejected' and not api.writes


@pytest.mark.parametrize('case', ['lost_response', 'ignored_write', 'replica_lag', 'redirect', 'rate_limit', 'readback_denied'])
def test_dispatched_uncertain_receipt_never_replays(case):
    api = AppointmentHTTP(); store = ActionStore()
    prepared = store.prepare(api.canvas, 'alice', action(api))
    if case == 'lost_response': api.lose_response = True
    elif case == 'ignored_write': api.ignore_write = True
    elif case == 'replica_lag': api.stale_inventory = True
    elif case == 'redirect': api.write_status = 307
    elif case == 'rate_limit': api.write_status = 429
    else: api.after_write = lambda a: a.errors.update({'calendar_events/1000': 403})
    first = store.execute(api.canvas, 'alice', prepared['action_id'], lambda: None)
    assert first['state'] == 'uncertain' and len(api.writes) == 1
    assert store.execute(api.canvas, 'alice', prepared['action_id'], lambda: None) == first
    assert len(api.writes) == 1


def test_actual_mcp_denied_read_preserves_failure_and_hides_upstream_body():
    api = AppointmentHTTP(); api.errors['calendar_events'] = 403
    mcp = FastMCP('appointment-denied')
    register_tools(mcp, lambda: api.canvas, lambda: None, owner=lambda: 'alice')
    async def run():
        result = (await mcp.call_tool('canvas_appointments', {'course_id': 1, 'appointment_group_id': 3}))[1]
        assert result['status'] == 'forbidden' and not result['coverage']['complete']
        assert 'private-upstream-body' not in str(result) and not api.writes
    asyncio.run(run())


def test_executor_authorization_precedes_any_canvas_dispatch():
    api = AppointmentHTTP(); store = ActionStore()
    prepared = store.prepare(api.canvas, 'alice', action(api)); before = len(api.calls)
    def deny(): raise PermissionError('read only')
    with pytest.raises(PermissionError): store.execute(api.canvas, 'alice', prepared['action_id'], deny)
    assert len(api.calls) == before and not api.writes


def test_native_replace_all_can_cancel_concurrent_booking_and_exposes_unobservable_cancelled_set():
    api = AppointmentHTTP(True); api.reserve(10)
    store = ActionStore(); prepared = store.prepare(api.canvas, 'alice', action(api, 'appointment_reschedule'))
    # Canvas locks/changes the participant only during its transaction, after
    # our exact-one preflight. A second group member can book in that gap.
    api.before_write = lambda a: a.reserve(11, 101)
    outcome = store.execute(api.canvas, 'alice', prepared['action_id'], lambda: None)
    assert outcome['state'] == 'succeeded'
    result = outcome['result']
    assert result['verified'] and result['confirmed_cancelled_reservation_ids'] == [100]
    assert api.records[101]['workflow_state'] == 'deleted'
    assert result['replacement_effect']['observed_preflight_reservation_ids'] == [100]
    assert result['replacement_effect']['concurrent_reservations_may_also_be_cancelled'] is True
    assert result['replacement_effect']['atomic_count_condition_supported'] is False
    assert result['coverage']['cancelled_set_complete'] is False and result['coverage']['complete'] is False
    assert result['coverage']['final_inventory_complete'] is True
    assert len(api.writes) == 1
