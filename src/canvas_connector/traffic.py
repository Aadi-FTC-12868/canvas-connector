"""Bound simultaneous upstream requests for the single-process personal server.

All assistant grants use the same Canvas account. Keep their HTTP requests behind
one gate, including streaming responses and anonymous file transfers. This does
not change Canvas quotas, cache results, or retry any operation.
"""
from __future__ import annotations

import threading
import httpx


class RequestGate:
    def __init__(self, concurrency: int = 4, wait_seconds: float = 30):
        if not 1 <= concurrency <= 16 or not 0 < wait_seconds <= 60:
            raise ValueError("Invalid upstream request limits")
        self.slots = threading.BoundedSemaphore(concurrency)
        self.wait_seconds = wait_seconds

    def wrap(self, transport: httpx.BaseTransport) -> httpx.BaseTransport:
        return _GatedTransport(transport, self)


class _GatedStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, gate: RequestGate):
        self.stream, self.gate = stream, gate
        self.closed = False
        self.lock = threading.Lock()

    def __iter__(self):
        try:
            yield from self.stream
        finally:
            self.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.stream.close()
        finally:
            self.gate.slots.release()


class _GatedTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport, gate: RequestGate):
        self.transport, self.gate = transport, gate

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not self.gate.slots.acquire(timeout=self.gate.wait_seconds):
            raise httpx.PoolTimeout("Canvas requests are busy; try again shortly", request=request)
        try:
            response = self.transport.handle_request(request)
            # A buffering transport may have consumed and closed the body
            # already. HTTPX will not close a replacement stream in that case.
            if response.is_closed:
                self.gate.slots.release()
                return response
            response.stream = _GatedStream(response.stream, self.gate)
            return response
        except BaseException:
            self.gate.slots.release()
            raise

    def close(self):
        self.transport.close()
