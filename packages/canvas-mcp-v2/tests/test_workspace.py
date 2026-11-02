from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from canvas_mcp_v2.workspace import register_workspace_tools


def test_file_window_is_bounded_and_omits_signed_links():
    visited = []
    closed = []

    def paginate(request):
        try:
            for index in range(1000):
                visited.append(index)
                yield {
                    "id": index + 1,
                    "filename": "note.txt",
                    "url": "https://storage.example/?secret=private",
                }
        finally:
            closed.append(True)

    canvas = SimpleNamespace(
        config=SimpleNamespace(base_url="https://school.instructure.com"), paginate=paginate
    )
    mcp = FastMCP("workspace")
    register_workspace_tools(mcp, lambda: canvas)
    result = mcp._tool_manager.get_tool("canvas_browse_files").fn(offset=2, limit=3)
    assert [item["id"] for item in result["items"]] == [3, 4, 5]
    assert result["next_offset"] == 5 and result["has_more"]
    assert len(visited) == 6 and closed == [True]
    assert "secret" not in str(result)


def test_remote_sync_refused_before_provider_or_filesystem(monkeypatch):
    monkeypatch.setenv("CANVAS_MCP_TRANSPORT", "http")
    mcp = FastMCP("workspace")

    def unavailable():
        pytest.fail("Remote sync must not obtain a Canvas client")

    register_workspace_tools(mcp, unavailable)
    with pytest.raises(ValueError, match="Local course folders"):
        mcp._tool_manager.get_tool("canvas_sync_courses").fn(
            output_root="/private/school", all_active=True, apply=True
        )


def test_sync_defaults_to_dry_run_with_explicit_bounded_selection(monkeypatch):
    monkeypatch.setenv("CANVAS_MCP_TRANSPORT", "stdio")
    calls = []

    def sync(canvas, root, **kwargs):
        calls.append((root, kwargs))
        return {"mode": "dry_run"}

    monkeypatch.setattr("canvas_mcp_v2.workspace.run_sync", sync)
    mcp = FastMCP("workspace")
    register_workspace_tools(mcp, lambda: object())
    mcp._tool_manager.get_tool("canvas_sync_courses").fn(
        output_root="/private/school", course_ids=[1]
    )
    assert calls[0][1]["apply"] is False and calls[0][1]["course_ids"] == [1]
    assert calls[0][1]["limits"].max_files == 100
