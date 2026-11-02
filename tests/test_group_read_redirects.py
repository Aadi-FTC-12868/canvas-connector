"""Passive group reads never follow a redirect into a state-changing view."""
import httpx
import pytest
from canvas_py import Canvas
from canvas_py._errors import CanvasError
from test_group_content import MockCanvas

from canvas_connector import group_content


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", [
    "/api/v1/users/self/profile", "/api/v1/groups/2",
    "/api/v1/groups/2/users/self", "/api/v1/groups/2/files",
])
def test_group_access_and_collection_redirects_never_dispatch_native_view(status, target):
    state = MockCanvas()
    native_view = "/api/v1/groups/2/pages/page_id:20"
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if request.url.path == target:
            return httpx.Response(status, headers={
                "Location": "https://school.example" + native_view})
        if request.url.path == native_view:
            raise AssertionError("A passive group read dispatched a native view")
        return state.handle(request)

    try:
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            with Canvas(state.canvas.config, http_client=http) as canvas:
                with pytest.raises(CanvasError):
                    if target.endswith("/files"):
                        group_content.list_group_files(canvas, 2)
                    else:
                        group_content.get_group(canvas, 2)
        assert seen.count(target) == 1
        assert native_view not in seen
        assert not state.writes
    finally:
        state.close()
