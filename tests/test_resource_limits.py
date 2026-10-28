import asyncio
import threading

import pytest

from rssbot import resource_limits
from rssbot.public_http import public_connection


def test_cancelled_caller_does_not_release_a_running_worker(monkeypatch):
    monkeypatch.setattr(resource_limits, "_WORKERS", threading.BoundedSemaphore(1))
    started = threading.Event()
    finish = threading.Event()

    @resource_limits.bounded_work(ValueError)
    def work():
        started.set()
        finish.wait(5)

    async def scenario():
        worker = asyncio.create_task(asyncio.to_thread(work))
        await asyncio.to_thread(started.wait, 2)
        assert started.is_set()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        try:
            with pytest.raises(ValueError, match="занят"):
                work()
        finally:
            finish.set()

    asyncio.run(scenario())
    work()


def test_nested_work_reuses_slot_and_exception_releases_it(monkeypatch):
    monkeypatch.setattr(resource_limits, "_WORKERS", threading.BoundedSemaphore(1))

    @resource_limits.bounded_work(ValueError)
    def inner():
        return 42

    @resource_limits.bounded_work(ValueError)
    def outer(fail=False):
        assert inner() == 42
        if fail:
            raise RuntimeError("failed")
        return 42

    with pytest.raises(RuntimeError):
        outer(True)
    assert outer() == 42


def test_public_connection_uses_the_checked_numeric_address(monkeypatch):
    from rssbot import public_http
    calls = []
    records = [(2, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(public_http.socket, "getaddrinfo", lambda *a, **k: records)

    class Socket:
        def __init__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, value):
            calls.append(value)

    monkeypatch.setattr(public_http.socket, "socket", Socket)
    public_connection(("example.com", 443), timeout=1)
    assert calls == [("93.184.216.34", 443)]
    records.append((2, 1, 6, "", ("127.0.0.1", 443)))
    with pytest.raises(OSError, match="forbidden"):
        public_connection(("example.com", 443))
    assert len(calls) == 1
