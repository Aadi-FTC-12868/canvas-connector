"""One initialized legacy child behind the SDK's dual-era stdio server.

The SDK correlates independent upstream/downstream request IDs. Cancellation
stops the caller's wait; already-issued legacy calls are drained. No call is retried, including timeout,
HeaderMismatch, cancellation, malformed results, or transport failure.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from mcp.types import (
    CONNECTION_CLOSED,
    INVALID_PARAMS,
    REQUEST_TIMEOUT,
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
)

from . import __version__
from .transport import MAX_MESSAGE_BYTES, BoundedInput, BoundedOutput

PERSONAL_INSTRUCTIONS = (
    "This is a personal single-user Canvas connection. Every call uses the local "
    "owner's Canvas identity; never share this tunnel with other users. "
    "The tunnel facade limits each serialized request/response to 9 MiB; use at "
    "most 6 MiB of raw upload bytes to leave room for base64 and JSON. The core "
    "schemas describe the core limits, not this narrower transport limit. "
    "Cancellation, timeout, or disconnect does not prove a write was rolled back. "
    "Inspect the action receipt and fresh Canvas state before any new write. "
    "Receipts and continuation handles are process-local and lost on restart."
)

# Deliberately allowlist. No OPENAI_*, MCP_*, TUNNEL_*, OTEL_*, PYTHONPATH,
# arbitrary proxy credential, or hosted CONNECTOR_* value reaches the child.
CHILD_ENV_KEYS = frozenset({
    "HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER", "LANG", "LC_ALL",
    "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "APPDATA", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATHEXT",
    "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT", "USERNAME", "USERPROFILE",
    "CANVAS_BASE_URL", "CANVAS_API_TOKEN", "CANVAS_ENV_PATH", "CANVAS_TOKEN_EXPIRES",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
})


def child_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    result = {k: v for k, v in source.items() if k in CHILD_ENV_KEYS and not v.startswith("()")}
    result.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    return result


def connector_parameters(python: str, *, allow_writes: bool = False) -> StdioServerParameters:
    path = Path(python)
    # Do not resolve venv Python symlinks: that changes its site-packages.
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("--connector-python must be an absolute executable Python path")
    args = ["-m", "canvas_connector", "stdio"]
    if allow_writes:
        args.append("--allow-writes")
    return StdioServerParameters(command=str(path), args=args, env=child_environment())


class ChildUnavailable(RuntimeError):
    """Terminating the facade is safer than replaying a request in a new child."""


class PersonalServer(Server):
    """Preserve the child's experimental capability container in both eras."""
    child_experimental: dict | None = None

    def get_capabilities(self, *args, **kwargs):
        capabilities = super().get_capabilities(*args, **kwargs)
        if self.child_experimental is not None:
            capabilities.experimental = copy.deepcopy(self.child_experimental)
        return capabilities


def serialized_size(value: Any) -> int:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class Bridge:
    def __init__(self, session: ClientSession, listing: ListToolsResult, tasks, *, timeout=105):
        self.session, self.listing = session, listing
        self.tools = {tool.name: tool for tool in listing.tools}
        self.tasks, self.timeout = tasks, timeout
        self.pending = anyio.CapacityLimiter(16)

    async def _dispatch(self, params, outcome, completed, borrower):
        try:
            await anyio.lowlevel.checkpoint()
            if outcome.get("abandoned"):
                return
            # Caller cancellation must not reach MCP 1.27: its response writer
            # can double-answer a cancelled sync tool and corrupt that session.
            # A detached, capacity-bounded task drains the one issued call and
            # preserves its process-local ActionStore receipt. There is no retry.
            outcome["issued"] = True
            outcome["result"] = await self.session.send_request(
                CallToolRequest(params=CallToolRequestParams(
                    name=params.name, arguments=params.arguments,
                )), CallToolResult, request_read_timeout_seconds=300,
            )
        except MCPError as exc:
            outcome["error"] = exc
            if exc.code == REQUEST_TIMEOUT:
                # A truly stuck legacy child is terminated after this hard cap;
                # do not continue a potentially corrupted cancellation session.
                raise ChildUnavailable("Canvas child exceeded its hard response deadline") from None
        except Exception:  # noqa: BLE001 - never expose upstream validation values
            outcome["error"] = MCPError(-32603,
                "Canvas returned an unavailable response. A write may have completed; "
                "inspect its receipt and current Canvas state. No request was retried.")
        finally:
            self.pending.release_on_behalf_of(borrower)
            completed.set()

    async def list_tools(self, ctx, params):
        if params is not None and params.cursor is not None:
            raise MCPError(INVALID_PARAMS, "This connector tool catalog has no cursor")
        return self.listing.model_copy(deep=True)

    async def call_tool(self, ctx, params: CallToolRequestParams) -> CallToolResult:
        if params.name not in self.tools:
            raise MCPError(INVALID_PARAMS, "Unknown Canvas tool")
        if (params.input_responses is not None or params.request_state is not None
                or params.task is not None):
            raise MCPError(INVALID_PARAMS, "This connector does not support multi-round tool input")
        # The byte-framing cap protects production before JSON parsing. This
        # second check protects callers using the bridge directly in-process.
        if serialized_size(ctx.params or {}) > MAX_MESSAGE_BYTES - 1024:
            raise MCPError(INVALID_PARAMS, "Tunnel request exceeds the serialized message limit")
        await anyio.lowlevel.checkpoint_if_cancelled()
        borrower = object()
        try:
            self.pending.acquire_on_behalf_of_nowait(borrower)
        except anyio.WouldBlock:
            raise MCPError(-32000, "The personal connector is busy; this call was not dispatched") from None
        outcome, completed = {}, anyio.Event()
        self.tasks.start_soon(self._dispatch, params, outcome, completed, borrower)
        try:
            # Deliberately bypass high-level Client.call_tool (which can retry
            # HeaderMismatch) and ClientSession.call_tool's schema-refetch path.
            # Never forward caller metadata as child identity or credentials.
            with anyio.move_on_after(self.timeout) as deadline:
                await completed.wait()
            if deadline.cancelled_caught:
                outcome["abandoned"] = True
                raise MCPError(REQUEST_TIMEOUT,
                    "Canvas response was not confirmed. A write may have completed; "
                    "inspect its receipt and current Canvas state. No request was retried.")
            if "error" in outcome:
                raise outcome["error"]
            result = outcome["result"]
        except anyio.get_cancelled_exc_class():
            outcome["abandoned"] = True
            raise
        except MCPError as exc:
            if exc.code in {REQUEST_TIMEOUT, CONNECTION_CLOSED}:
                raise MCPError(exc.code,
                    "Canvas response was not confirmed. A write may have completed; "
                    "inspect its receipt and current Canvas state. No request was retried.") from None
            raise
        except Exception:  # noqa: BLE001 - transport boundary reports a sanitized outcome
            raise MCPError(-32603,
                "Canvas returned an unavailable response. A write may have completed; "
                "inspect its receipt and current Canvas state. No request was retried.") from None
        if serialized_size(result) > MAX_MESSAGE_BYTES - 1024:
            raise MCPError(-32603,
                "Canvas response exceeds the tunnel limit. A write may have completed; "
                "inspect its receipt and current Canvas state. No request was retried.")
        return result


@asynccontextmanager
async def connected_server(parameters: StdioServerParameters, *, timeout: float = 105):
    """Internal injection point for synthetic tests; the CLI fixes the command.

    EOF is observable even while idle, so a dead child cannot leave a seemingly
    healthy facade serving stale metadata. There is no child replacement loop.
    """
    ended = anyio.Event()
    # Do not expose child tracebacks or config values to a remote caller/log.
    with open(os.devnull, "w") as errlog:  # noqa: ASYNC230 - local null descriptor, no data I/O
        async with stdio_client(parameters, errlog=errlog) as (raw_read, child_write):
            send, child_read = anyio.create_memory_object_stream[SessionMessage | Exception](0)

            async def relay():
                try:
                    async with send:
                        async for message in raw_read:
                            await send.send(message)
                finally:
                    ended.set()

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(relay)
                async with ClientSession(child_read, child_write, read_timeout_seconds=timeout) as session:
                    initial = await session.initialize()
                    listing = await session.list_tools()
                    if (listing.next_cursor is not None or not listing.tools or len(listing.tools) > 1000
                            or len({tool.name for tool in listing.tools}) != len(listing.tools)
                            or any(not tool.name.startswith("canvas_") for tool in listing.tools)
                            or serialized_size(listing) > MAX_MESSAGE_BYTES - 1024):
                        raise ValueError("Child must expose one bounded, unique Canvas tool catalog")
                    bridge = Bridge(session, listing, tasks, timeout=timeout)
                    # Core currently advertises empty resources/prompts as well
                    # as tools. Forward those supported operations faithfully.
                    handlers = {}
                    if initial.capabilities.resources is not None:
                        async def resources(ctx, params):
                            return await session.list_resources(params=params)

                        async def templates(ctx, params):
                            return await session.list_resource_templates(params=params)

                        async def read_resource(ctx, params):
                            return await session.read_resource(params.uri)

                        handlers.update(on_list_resources=resources,
                            on_list_resource_templates=templates, on_read_resource=read_resource)
                    if initial.capabilities.prompts is not None:
                        async def prompts(ctx, params):
                            return await session.list_prompts(params=params)

                        async def get_prompt(ctx, params):
                            return await session.get_prompt(params.name, params.arguments)

                        handlers.update(on_list_prompts=prompts, on_get_prompt=get_prompt)
                    server = PersonalServer(
                        "Canvas Connector personal tunnel", version=__version__,
                        instructions=(initial.instructions or "") + "\n\n" + PERSONAL_INSTRUCTIONS,
                        on_list_tools=bridge.list_tools, on_call_tool=bridge.call_tool,
                        get_tool_input_schema=lambda name: (
                            bridge.tools[name].input_schema if name in bridge.tools else None),
                        **handlers,
                    )
                    server.child_experimental = initial.capabilities.experimental
                    try:
                        yield server, ended
                    finally:
                        tasks.cancel_scope.cancel()


async def serve(parameters: StdioServerParameters, *, timeout: float = 105) -> None:
    if os.name != "posix":
        raise RuntimeError("Cancellation-safe personal tunnel stdio requires macOS or Linux")
    output = BoundedOutput(sys.stdout.buffer)
    try:
        async with connected_server(parameters, timeout=timeout) as (server, ended), stdio_server(
            stdin=BoundedInput(sys.stdin.buffer), stdout=output,
        ) as (read, write):
            async def watch_child():
                await ended.wait()
                raise ChildUnavailable("Canvas child stopped; requests were not replayed")

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(watch_child)
                try:
                    await server.run(read, write, server.create_initialization_options())
                finally:
                    tasks.cancel_scope.cancel()
    finally:
        output.restore()

