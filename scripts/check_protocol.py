"""Exercise the real stdio MCP subprocess without printing account payloads.

Default is credential-free protocol validation. --live uses the caller's explicit
CANVAS_ENV_PATH or environment and performs read-only status/dashboard calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(live: bool) -> None:
    env = os.environ.copy()
    if not live:
        env.pop("CANVAS_ENV_PATH", None)
        env.update(CANVAS_BASE_URL="https://school.example", CANVAS_API_TOKEN="synthetic-protocol-test")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "canvas_connector", "stdio"], env=env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            assert {"canvas_dashboard", "canvas_read_file", "canvas_prepare_action", "canvas_submit_assignment"} <= names
            assert "canvas_execute_action" not in names
            assert all(t.annotations is not None for t in listed.tools)
            execute = next(t for t in listed.tools if t.name == "canvas_submit_assignment")
            assert execute.annotations.readOnlyHint is False
            result = await session.call_tool("canvas_capabilities", {})
            assert not result.isError and result.structuredContent
            output = {"protocol": initialized.protocolVersion, "tool_count": len(names),
                      "structured_results": True, "write_annotations": True, "live": live}
            if live:
                for name, args in [("canvas_connection_status", {}), ("canvas_dashboard", {"refresh": True})]:
                    reply = await session.call_tool(name, args)
                    assert not reply.isError, f"{name} failed"
                    data = reply.structuredContent
                    assert data and data.get("status") == "ok", f"{name} did not return an accessible result"
                    output[name] = {"status": data["status"], "response_bytes": len(json.dumps(data).encode())}
            print(json.dumps(output, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    asyncio.run(check(parser.parse_args().live))
