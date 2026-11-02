"""Bound newline framing before the SDK parses messages; no protocol translation."""
from __future__ import annotations

import io
import os
from functools import partial
from typing import BinaryIO

import anyio

# OpenAI's tunnel limit is 10 MiB for the complete serialized envelope. Leave
# one MiB for its wrapping. This bound is independent of the core's upload cap.
MAX_MESSAGE_BYTES = 9 * 1024 * 1024


class MessageTooLarge(ValueError):
    """The connection is closed without dispatching an oversized frame."""


class BoundedInput:
    def __init__(self, stream: BinaryIO, maximum: int = MAX_MESSAGE_BYTES):
        self.stream, self.maximum = stream, maximum
        self.buffer = b""
        try:
            self.fd = stream.fileno() if os.name == "posix" else None
        except (AttributeError, io.UnsupportedOperation):
            self.fd = None

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        # Read bytes, not characters: multibyte JSON must obey the same bound.
        if self.fd is None:
            line = await anyio.to_thread.run_sync(
                partial(self.stream.readline, self.maximum + 2), abandon_on_cancel=True
            )
        else:
            # A blocked stdin readline thread can keep Python's IO finalizer
            # locked after child failure. POSIX readiness is cancellable without
            # leaving a thread or waiting for the caller to close stdin.
            while b"\n" not in self.buffer:
                if len(self.buffer) > self.maximum:
                    raise MessageTooLarge("Tunnel message exceeds the 9 MiB serialized limit")
                await anyio.wait_readable(self.fd)
                chunk = os.read(self.fd, min(65536, self.maximum + 2 - len(self.buffer)))
                if not chunk:
                    line, self.buffer = self.buffer, b""
                    break
                self.buffer += chunk
            else:
                line, self.buffer = self.buffer.split(b"\n", 1)
                line += b"\n"
        if not line:
            raise StopAsyncIteration
        body = line[:-1] if line.endswith(b"\n") else line
        if len(body) > self.maximum:
            raise MessageTooLarge("Tunnel message exceeds the 9 MiB serialized limit")
        return line.decode("utf-8", errors="strict")


class BoundedOutput:
    def __init__(self, stream: BinaryIO, maximum: int = MAX_MESSAGE_BYTES):
        self.stream, self.maximum = stream, maximum
        self.blocking = None
        try:
            self.fd = stream.fileno() if os.name == "posix" else None
        except (AttributeError, io.UnsupportedOperation):
            self.fd = None
        if self.fd is not None:
            self.blocking = os.get_blocking(self.fd)
            os.set_blocking(self.fd, False)

    def restore(self) -> None:
        if self.fd is not None and self.blocking is not None:
            os.set_blocking(self.fd, self.blocking)
            self.blocking = None

    async def write(self, value: str) -> None:
        raw = value.encode("utf-8")
        body = raw[:-1] if raw.endswith(b"\n") else raw
        if len(body) > self.maximum:
            raise MessageTooLarge("Tunnel response exceeds the 9 MiB serialized limit")
        if self.fd is None:
            await anyio.to_thread.run_sync(self.stream.write, raw)
        else:
            # Readiness alone is insufficient for a large blocking pipe write.
            # Nonblocking writes and partial-send tracking let child failure
            # interrupt a caller that stopped consuming stdout.
            remaining = memoryview(raw)
            while remaining:
                await anyio.wait_writable(self.fd)
                try:
                    count = os.write(self.fd, remaining)
                except BlockingIOError:
                    continue
                remaining = remaining[count:]

    async def flush(self) -> None:
        if self.fd is None:
            await anyio.to_thread.run_sync(self.stream.flush)

