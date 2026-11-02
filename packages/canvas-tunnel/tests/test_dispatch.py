import asyncio
from types import SimpleNamespace

import anyio
import anyio.lowlevel
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

from canvas_tunnel.bridge import Bridge, ChildUnavailable
from canvas_tunnel.transport import MAX_MESSAGE_BYTES

LISTING = ListToolsResult(tools=[Tool(name="canvas_test", input_schema={"type": "object"})])
PARAMS = CallToolRequestParams(name="canvas_test", arguments={"value": "unchanged"})
CTX = SimpleNamespace(params={"name": "canvas_test", "arguments": {"value": "unchanged"}})


class Child:
    def __init__(self, error=None, blocked=False, result=None):
        self.calls, self.error = [], error
        self.release = anyio.Event()
        if not blocked:
            self.release.set()
        self.result = result or CallToolResult(content=[TextContent(type="text", text="saved")],
            structured_content={"verified": True})

    async def send_request(self, request, result_type, **kwargs):
        self.calls.append(request.model_dump(by_alias=True, exclude_none=True))
        await self.release.wait()
        if self.error:
            raise self.error
        return self.result


@pytest.mark.parametrize("code", [-32020, -32602, -32000])
async def test_child_errors_are_never_replayed_even_header_mismatch(code):
    child = Child(error=MCPError(code, "synthetic child error"))
    async with anyio.create_task_group() as tasks:
        bridge = Bridge(child, LISTING, tasks)
        with pytest.raises(MCPError) as caught:
            await bridge.call_tool(CTX, PARAMS)
        assert caught.value.code == code and len(child.calls) == 1
        assert child.calls[0]["params"] == {"name": "canvas_test", "arguments": {"value": "unchanged"}}


async def test_cancellation_before_scheduled_dispatch_never_sends_later():
    class Queue:
        def __init__(self):
            self.ready = asyncio.Event()

        def start_soon(self, fn, *args):
            self.fn, self.args = fn, args
            self.ready.set()

    child, queue = Child(), Queue()
    bridge = Bridge(child, LISTING, queue)
    caller = asyncio.create_task(bridge.call_tool(CTX, PARAMS))
    await queue.ready.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await queue.fn(*queue.args)
    assert child.calls == [] and bridge.pending.borrowed_tokens == 0


async def test_overflow_rejected_without_dispatch_and_drains_are_bounded():
    child = Child(blocked=True)
    async with anyio.create_task_group() as tasks:
        bridge = Bridge(child, LISTING, tasks)
        callers = [asyncio.create_task(bridge.call_tool(CTX, PARAMS)) for _ in range(16)]
        try:
            with anyio.fail_after(2):
                while len(child.calls) < 16:
                    await anyio.lowlevel.checkpoint()
            with pytest.raises(MCPError, match="not dispatched"):
                await bridge.call_tool(CTX, PARAMS)
            assert len(child.calls) == 16 and bridge.pending.borrowed_tokens == 16
            # Cancelling callers does not send child cancellation or create capacity
            # until their already-issued requests really finish.
            for caller in callers:
                caller.cancel()
            await asyncio.gather(*callers, return_exceptions=True)
            assert bridge.pending.borrowed_tokens == 16
            child.release.set()
            with anyio.fail_after(2):
                while bridge.pending.borrowed_tokens:
                    await anyio.lowlevel.checkpoint()
            assert len(child.calls) == 16
        finally:
            child.release.set()
            for caller in callers:
                caller.cancel()
            await asyncio.gather(*callers, return_exceptions=True)


async def test_oversized_child_response_is_not_truncated_or_retried():
    child = Child(result=CallToolResult(content=[TextContent(type="text", text="x" * MAX_MESSAGE_BYTES)]))
    async with anyio.create_task_group() as tasks:
        bridge = Bridge(child, LISTING, tasks)
        with pytest.raises(MCPError, match="may have completed"):
            await bridge.call_tool(CTX, PARAMS)
        assert len(child.calls) == 1


async def test_hard_child_deadline_terminates_instead_of_replaying_or_reusing_session():
    child = Child(error=MCPError(-32001, "synthetic hard deadline"))
    with pytest.raises(ExceptionGroup) as failed:
        async with anyio.create_task_group() as tasks:
            bridge = Bridge(child, LISTING, tasks)
            await bridge.call_tool(CTX, PARAMS)
    assert any(isinstance(error, ChildUnavailable) for error in failed.value.exceptions)
    assert len(child.calls) == 1


@pytest.mark.parametrize("values", [{"task": {"ttl": 1000}}, {"requestState": "opaque"},
                                   {"inputResponses": {}}])
async def test_modern_continuation_or_task_contract_is_not_silently_dropped(values):
    child = Child()
    async with anyio.create_task_group() as tasks:
        bridge = Bridge(child, LISTING, tasks)
        params = CallToolRequestParams.model_validate({"name": "canvas_test", **values})
        with pytest.raises(MCPError, match="multi-round"):
            await bridge.call_tool(CTX, params)
        assert not child.calls
