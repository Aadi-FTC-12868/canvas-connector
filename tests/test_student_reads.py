"""Read contracts use the real Canvas client without upstream mutations."""
import asyncio

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from mcp.server.fastmcp import FastMCP

from canvas_connector.tools import register_tools


def call(handler, tool, **arguments):
    with Canvas(CanvasConfig('https://school.example', 'synthetic'),
                http_client=httpx.Client(transport=httpx.MockTransport(handler))) as canvas:
        mcp = FastMCP('read-contracts')
        register_tools(mcp, lambda: canvas, lambda: None, owner=lambda: 'synthetic-owner')
        fn = next(item.fn for item in mcp._tool_manager.list_tools() if item.name == tool)
        return asyncio.run(fn(**arguments))


@pytest.mark.parametrize('arguments,expected_path', [
    ({}, '/api/v1/users/self/files'),
    ({'course_id': 4}, '/api/v1/courses/4/files'),
    ({'folder_id': 9}, '/api/v1/folders/9/files'),
])
def test_file_filters_reach_correct_context(arguments, expected_path):
    def handler(request):
        assert request.method == 'GET' and request.url.path == expected_path
        assert request.url.params.get_list('content_types[]') == ['image', 'application/pdf']
        return httpx.Response(200, json=[{'id': 5, 'display_name': 'Worksheet.pdf'}])
    result = call(handler, 'canvas_browse_files', content_types=['image', 'application/pdf'], **arguments)
    assert result['status'] == 'ok' and result['data'][0]['id'] == 5


@pytest.mark.parametrize('arguments', [
    {'kind': 'folders', 'content_types': ['image']},
    {'content_types': ['x'] * 21}, {'content_types': ['']}, {'content_types': ['image\n']},
])
def test_invalid_file_filter_does_not_fetch(arguments):
    def handler(request):
        raise AssertionError('Invalid arguments must fail before network access')
    assert call(handler, 'canvas_browse_files', **arguments)['status'] == 'invalid_request'


@pytest.mark.parametrize('tool,arguments,path,data', [
    ('canvas_file_quota', {}, '/api/v1/users/self/files/quota', {'quota': 1000, 'quota_used': 400}),
    ('canvas_get_folder', {'folder_id': 9}, '/api/v1/folders/9', {'id': 9, 'name': 'Notes', 'context_type': 'User', 'context_id': 7}),
    ('canvas_activity_summary', {}, '/api/v1/users/self/activity_stream/summary', [{'type': 'Submission', 'count': 3, 'unread_count': 2}]),
])
def test_student_reads_use_get_without_mutation(tool, arguments, path, data):
    seen = []
    def handler(request):
        seen.append(request)
        assert request.method == 'GET' and request.url.path == path
        return httpx.Response(200, json=data)
    result = call(handler, tool, **arguments)
    assert result['status'] == 'ok'
    assert result['data'] == data
    assert len(seen) == 1


@pytest.mark.parametrize('arguments', [{}, {'course_id': 4}, {'folder_id': 9}])
def test_file_listing_removes_opaque_download_credentials(arguments):
    import json
    def handler(request):
        return httpx.Response(200, json=[{'id': 5, 'display_name': 'Worksheet.pdf',
            'url': 'https://cdn.example/opaque-download-secret/file.pdf?X-Amz-Signature=query-secret'}])
    result = call(handler, 'canvas_browse_files', **arguments)
    assert 'opaque-download-secret' not in json.dumps(result)
    assert 'query-secret' not in json.dumps(result)
    assert result['data'][0]['html_url'] == 'https://school.example/files/5'
    assert result['data'][0]['read'] == {'tool': 'canvas_read_file', 'file_id': 5}
