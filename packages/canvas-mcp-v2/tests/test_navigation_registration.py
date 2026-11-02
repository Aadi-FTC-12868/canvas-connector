"""Run with the MCP environment: python -m unittest discover -s tests."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP

from canvas_mcp_v2.navigation import register_navigation_tools


class NavigationSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_schemas_are_registered_and_identity_enums_are_closed(self):
        server = FastMCP("navigation-test")

        def provider():
            raise AssertionError("Listing tools must not load credentials")

        register_navigation_tools(server, provider)
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(set(tools), {"canvas_dashboard", "canvas_course_home", "canvas_browse_course", "canvas_open_item",
                                      "canvas_open_url", "canvas_module_sequence", "canvas_clear_cache"})
        schema = tools["canvas_browse_course"].inputSchema
        self.assertIn("modules", schema["properties"]["section"]["enum"])
        self.assertNotIn("url", schema["properties"])
        self.assertEqual(tools["canvas_open_item"].inputSchema["properties"]["item_type"]["enum"],
                         ["module", "module_item", "page", "assignment", "file", "quiz", "discussion"])

    async def test_provider_is_resolved_for_each_call_not_captured_at_registration(self):
        server = FastMCP("navigation-test")
        accounts = iter(["one", "two"])

        def provider():
            identity = next(accounts)
            return SimpleNamespace(navigation=SimpleNamespace(dashboard=lambda **kwargs: {"account": identity}))

        register_navigation_tools(server, provider)
        first = await server.call_tool("canvas_dashboard", {})
        second = await server.call_tool("canvas_dashboard", {})
        self.assertIn("one", str(first))
        self.assertIn("two", str(second))


if __name__ == "__main__":
    unittest.main()
