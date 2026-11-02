"""The actual MCP schema exposes bounded content reading in both transports."""
from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from canvas_mcp_v2.documents import register_document_tools


@pytest.mark.asyncio
async def test_document_schema_registers_without_loading_credentials():
    server = FastMCP("document-test")
    def provider():
        raise AssertionError("Schema listing must not load credentials")
    register_document_tools(server, provider)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == {"canvas_read_file", "canvas_document_capabilities"}
    properties = tools["canvas_read_file"].inputSchema["properties"]
    assert {"file_id", "offset", "char_offset", "max_units", "max_chars", "expected_sha256", "ocr"} == set(properties)
    result = await server.call_tool("canvas_document_capabilities", {})
    assert "docx" in str(result)


@pytest.mark.asyncio
async def test_document_tool_passes_each_call_to_current_provider(monkeypatch):
    server = FastMCP("document-test")
    accounts = iter(["first", "second"])
    register_document_tools(server, lambda: next(accounts))
    monkeypatch.setattr("canvas_mcp_v2.documents.read_canvas_file", lambda canvas, file_id, **kwargs: {"account": canvas, "file_id": file_id, **kwargs})
    first = await server.call_tool("canvas_read_file", {"file_id": 42, "ocr": True})
    second = await server.call_tool("canvas_read_file", {"file_id": 42, "offset": 5, "char_offset": 100})
    assert "first" in str(first) and "second" in str(second)
    assert "100" in str(second)
