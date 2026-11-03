from concurrent.futures import ThreadPoolExecutor
import threading
import time

import httpx
import pytest

from canvas_connector.traffic import RequestGate


def test_already_buffered_responses_release_the_permit():
    gate = RequestGate(1, wait_seconds=.01)
    with httpx.Client(transport=gate.wrap(httpx.MockTransport(
            lambda request: httpx.Response(200, json={"synthetic": True})))) as client:
        for _ in range(10):
            assert client.get("https://synthetic.example/read").json() == {"synthetic": True}


def test_shared_gate_covers_response_streams_across_independent_clients():
    gate, lock = RequestGate(2), threading.Lock()
    counts = {"active": 0, "peak": 0, "calls": 0}

    class Body(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            time.sleep(.02)
            yield b"synthetic"

        def close(self):
            if not self.closed:
                self.closed = True
                with lock:
                    counts["active"] -= 1

    def upstream(request):
        with lock:
            counts["active"] += 1
            counts["calls"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
        return httpx.Response(200, stream=Body())

    def call(_):
        with httpx.Client(transport=gate.wrap(httpx.MockTransport(upstream))) as client:
            assert client.get("https://synthetic.example/read").content == b"synthetic"

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(call, range(16)))
    assert counts == {"active": 0, "peak": 2, "calls": 16}


def test_wait_timeout_never_dispatches_then_slot_is_reusable_after_early_close():
    calls, gate = [], RequestGate(1, wait_seconds=.01)

    def upstream(request):
        calls.append(request.method)
        return httpx.Response(200, stream=httpx.ByteStream(b"synthetic"))

    with httpx.Client(transport=gate.wrap(httpx.MockTransport(upstream))) as client:
        with client.stream("GET", "https://synthetic.example/read"):
            with pytest.raises(httpx.PoolTimeout):
                client.post("https://synthetic.example/write")
        assert client.get("https://synthetic.example/read").status_code == 200
    assert calls == ["GET", "GET"]


@pytest.mark.parametrize("phase", ["dispatch", "body", "close"])
def test_failure_releases_slot_without_replaying(phase):
    gate, calls = RequestGate(1, wait_seconds=.01), []

    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            if phase == "body":
                raise httpx.ReadError("synthetic")
            yield b"synthetic"

        def close(self):
            if phase == "close":
                raise httpx.ReadError("synthetic")

    def upstream(request):
        calls.append(request.method)
        if len(calls) == 1:
            if phase == "dispatch":
                raise httpx.ConnectError("synthetic")
            return httpx.Response(200, stream=Broken())
        return httpx.Response(200, content=b"ok")

    with httpx.Client(transport=gate.wrap(httpx.MockTransport(upstream))) as client:
        with pytest.raises(httpx.HTTPError):
            client.post("https://synthetic.example/write")
        assert client.get("https://synthetic.example/read").content == b"ok"
    assert calls == ["POST", "GET"]
