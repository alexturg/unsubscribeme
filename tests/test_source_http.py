import asyncio
import gzip
from types import SimpleNamespace
import zlib

import pytest

from rssbot import source_http as http


@pytest.mark.parametrize("address", [
    "127.0.0.1", "0.0.0.0", "169.254.169.254", "10.66.66.1", "100.100.100.200",
    "192.168.1.1", "172.16.1.1", "224.0.0.1", "::1", "fe80::1", "fc00::1",
    "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::", "fe80::1%eth0",
])
def test_forbidden_addresses(address):
    with pytest.raises(http.SourceFetchError):
        http.check_address(address)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/feed", "http://user:pass@example.com/",
    "http://127.0.0.1/feed", "http://[::ffff:127.0.0.1]/", "http://2130706433/",
])
def test_rejects_unsafe_urls(url):
    # Noncanonical numeric hosts must still be rejected by the connecting resolver.
    if "2130706433" in url:
        async def scenario():
            resolver = http.SourceResolver(frozenset())
            try:
                with pytest.raises(http.SourceFetchError):
                    await resolver.resolve("2130706433", 80)
            finally:
                await resolver.close()
        asyncio.run(scenario())
    else:
        with pytest.raises(http.SourceFetchError):
            http.source_url(url, frozenset())


def test_private_exceptions_are_exact_and_never_allow_metadata_or_loopback():
    allowed = http.private_hosts("rss.internal, 10.66.66.1")
    assert str(http.source_url("http://10.66.66.1/feed", allowed)).endswith("/feed")
    with pytest.raises(http.SourceFetchError):
        http.source_url("http://10.66.66.2/feed", allowed)
    for address in ("127.0.0.1", "169.254.169.254", "::1"):
        with pytest.raises(http.SourceFetchError):
            http.check_address(address, allow_private=True)
    with pytest.raises(http.SourceFetchError):
        http.private_hosts("*.internal")


def test_resolver_checks_all_addresses_and_returns_validated_records(monkeypatch):
    async def scenario():
        resolver = http.SourceResolver(frozenset({"rss.internal"}))
        records = [{"host": "93.184.216.34"}]

        async def resolve(*args):
            return records

        monkeypatch.setattr(resolver.delegate, "resolve", resolve)
        try:
            assert await resolver.resolve("example.com") is records
            records.append({"host": "10.66.66.1"})
            with pytest.raises(http.SourceFetchError):
                await resolver.resolve("example.com")
            assert await resolver.resolve("rss.internal") is records
            with pytest.raises(http.SourceFetchError):
                await resolver.resolve("evil.rss.internal")
        finally:
            await resolver.close()
    asyncio.run(scenario())


def response(payload, encoding="identity", status=200, **headers):
    async def chunks(size):
        for i in range(0, len(payload), min(size, 37)):
            yield payload[i:i + min(size, 37)]
    return SimpleNamespace(
        headers={"Content-Encoding": encoding, **headers}, status=status,
        content=SimpleNamespace(iter_chunked=chunks), close=lambda: None,
    )


@pytest.mark.parametrize("encoding,compress", [
    ("identity", lambda data: data), ("gzip", gzip.compress), ("deflate", zlib.compress),
])
def test_bounded_stream_rejects_wire_and_decoded_overflow(encoding, compress):
    async def scenario():
        payload = b"a" * 1024
        assert await http.read_bounded(response(compress(payload), encoding), 1024) == payload
        with pytest.raises(http.SourceFetchError, match="limit"):
            await http.read_bounded(response(compress(payload * 1024), encoding), 1024)
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [gzip.compress(b"hello")[:-1], gzip.compress(b"hello") * 2])
def test_rejects_incomplete_or_concatenated_compression(payload):
    with pytest.raises(http.SourceFetchError):
        asyncio.run(http.read_bounded(response(payload, "gzip"), 1024))


def fake_session(monkeypatch, responses):
    calls = []

    class Context:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *args):
            pass

    class Session(Context):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["auto_decompress"] is False
            # The fake session still owns and closes the real connector.
            self.connector = kwargs["connector"]
            super().__init__(self)

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            calls.append((str(url), kwargs["headers"].copy()))
            return Context(next(responses))

    monkeypatch.setattr(http.aiohttp, "ClientSession", Session)
    return calls


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/admin", "https://[::1]/", "file:///etc/passwd",
    "http://example.com/insecure", "https://user:pass@example.com/",
])
def test_redirect_is_rejected_before_next_request(monkeypatch, location):
    calls = fake_session(monkeypatch, iter([response(b"", status=302, Location=location)]))
    with pytest.raises(http.SourceFetchError):
        asyncio.run(http.fetch_source(
            "https://example.com/feed", headers={}, max_bytes=1024, timeout_seconds=1,
        ))
    assert len(calls) == 1


def test_redirect_strips_conditional_headers_and_304_is_preserved(monkeypatch):
    calls = fake_session(monkeypatch, iter([
        response(b"", status=302, Location="https://other.example/feed"),
        response(b"", status=304, ETag="new"),
    ]))
    result = asyncio.run(http.fetch_source(
        "https://example.com/feed", headers={"If-None-Match": "old"},
        max_bytes=1024, timeout_seconds=1,
    ))
    assert result == (304, "new", None, None)
    assert calls[0][1]["If-None-Match"] == "old"
    assert calls[1][1] == {"Accept-Encoding": "identity"}


def test_redirect_limit(monkeypatch):
    calls = fake_session(monkeypatch, iter(
        [response(b"", status=302, Location="/again")] * 5,
    ))
    with pytest.raises(http.SourceFetchError, match="redirects"):
        asyncio.run(http.fetch_source(
            "https://example.com/feed", headers={}, max_bytes=1024, timeout_seconds=1,
        ))
    assert len(calls) == 5


def test_request_slots_bound_queue_and_recover_after_cancellation():
    async def scenario():
        slots = http.FetchSlots(active=1, queued=1, wait_seconds=1)
        async def queued():
            async with slots.acquire():
                pass
        async with slots.acquire():
            waiting = asyncio.create_task(queued())
            await asyncio.sleep(0)
            with pytest.raises(http.SourceFetchError, match="full"):
                async with slots.acquire():
                    pass
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        async with slots.acquire():
            pass
    asyncio.run(scenario())


def test_one_deadline_includes_slow_response_body(monkeypatch):
    async def slow_chunks(_):
        await asyncio.Event().wait()
        yield b"unreachable"
    slow = response(b"")
    slow.content.iter_chunked = slow_chunks
    fake_session(monkeypatch, iter([slow]))
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(http.fetch_source(
            "https://example.com/feed", headers={}, max_bytes=1024, timeout_seconds=0.02,
        ))
