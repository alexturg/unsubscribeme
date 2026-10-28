"""Bounded HTTP reads for untrusted RSS, JSON and calendar sources."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import socket
from weakref import WeakKeyDictionary
import zlib

import aiohttp
from yarl import URL


class SourceFetchError(ValueError):
    """The source violates a network or resource limit."""


_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))
_TRANSLATION_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32",
))


def check_address(address: str, *, allow_private: bool = False) -> None:
    # Scoped addresses and IPv6 transition mechanisms must not bypass IPv4 rules.
    if "%" in address:
        raise SourceFetchError("Scoped IP addresses are not allowed")
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return check_address(str(ip.ipv4_mapped), allow_private=allow_private)
        if any(ip in network for network in _TRANSLATION_NETWORKS):
            raise SourceFetchError("IPv6 transition addresses are not allowed")
    if ip.is_global and not ip.is_multicast:
        return
    if allow_private and any(ip in network for network in _PRIVATE_NETWORKS):
        return
    raise SourceFetchError("Source address is not a permitted public IP")


def private_hosts(value: str) -> frozenset[str]:
    hosts = set()
    for item in value.split(","):
        host = item.strip().rstrip(".").lower()
        if not host:
            continue
        if any(c in host for c in "/@*%"):
            raise SourceFetchError("Private source exceptions must be exact hostnames or IPs")
        try:
            # Match the same IDNA normalization the HTTP connector uses.
            hosts.add(URL.build(scheme="http", host=host).raw_host)
        except (ValueError, UnicodeError) as exc:
            raise SourceFetchError("Invalid private source hostname") from exc
    return frozenset(hosts)


def source_url(value: str, allowed: frozenset[str]) -> URL:
    try:
        url = URL(value)
        host = url.raw_host
        if url.scheme not in {"http", "https"} or not host or url.user is not None:
            raise ValueError
        if not url.port or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise SourceFetchError("Source must be an HTTP(S) URL without credentials") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "%" in host:
            raise SourceFetchError("Scoped hosts are not allowed")
    else:
        check_address(host, allow_private=host.rstrip(".").lower() in allowed)
    return url.with_fragment(None)


class SourceResolver(aiohttp.abc.AbstractResolver):
    """Validate the numeric addresses the connector will actually connect to.

    Unlike a separate preflight lookup, the connector consumes these validated
    records directly, so DNS cannot change between validation and connection.
    """

    def __init__(self, allowed: frozenset[str]) -> None:
        self.delegate = aiohttp.resolver.DefaultResolver()
        self.allowed = allowed

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        records = await self.delegate.resolve(host, port, family)
        if not records:
            raise SourceFetchError("Source hostname has no addresses")
        for record in records:
            check_address(
                record["host"], allow_private=host.rstrip(".").lower() in self.allowed,
            )
        return records

    async def close(self) -> None:
        await self.delegate.close()


class FetchSlots:
    """Limit active requests and queued callers, separately for each event loop."""

    def __init__(self, active: int = 4, queued: int = 16, wait_seconds: float = 5) -> None:
        self.active = active
        self.capacity = active + queued
        self.wait_seconds = wait_seconds
        self.states = WeakKeyDictionary()

    @asynccontextmanager
    async def acquire(self):
        loop = asyncio.get_running_loop()
        state = self.states.setdefault(loop, [asyncio.Semaphore(self.active), 0])
        if state[1] >= self.capacity:
            raise SourceFetchError("Source download queue is full; try again later")
        state[1] += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(state[0].acquire(), self.wait_seconds)
            except asyncio.TimeoutError as exc:
                raise SourceFetchError("Source download queue timed out") from exc
            acquired = True
            yield
        finally:
            if acquired:
                state[0].release()
            state[1] -= 1


_SLOTS = FetchSlots()


async def read_bounded(response, max_bytes: int) -> bytes:
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise SourceFetchError("Unsupported source content encoding")
    decoder = None if encoding == "identity" else zlib.decompressobj(
        16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS,
    )
    result = bytearray()
    wire_size = 0
    try:
        async for chunk in response.content.iter_chunked(64 * 1024):
            wire_size += len(chunk)
            if wire_size > max_bytes:
                raise SourceFetchError("Source download exceeds the size limit")
            data = decoder.decompress(chunk, max_bytes - len(result) + 1) if decoder else chunk
            result.extend(data)
            if len(result) > max_bytes:
                raise SourceFetchError("Decoded source exceeds the size limit")
            if decoder and decoder.unused_data:
                raise SourceFetchError("Trailing compressed source data is not allowed")
    except zlib.error as exc:
        raise SourceFetchError("Invalid compressed source") from exc
    if decoder and not decoder.eof:
        raise SourceFetchError("Incomplete compressed source")
    return bytes(result)


async def fetch_source(
    url: str, *, headers: dict[str, str], max_bytes: int,
    timeout_seconds: int, allowed_private_hosts: str = "",
) -> tuple[int, str | None, str | None, bytes | None]:
    allowed = private_hosts(allowed_private_hosts)
    current = source_url(url, allowed)

    async def fetch():
        resolver = SourceResolver(allowed)
        try:
            connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, limit=1)
            async with aiohttp.ClientSession(
                connector=connector, trust_env=False, auto_decompress=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=timeout_seconds, sock_connect=5),
            ) as session:
                target = current
                request_headers = {**headers, "Accept-Encoding": "identity"}
                for hop in range(5):
                    async with session.get(
                        target, headers=request_headers, allow_redirects=False,
                    ) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location or hop == 4:
                                raise SourceFetchError("Invalid or excessive source redirects")
                            next_url = source_url(str(target.join(URL(location))), allowed)
                            if target.scheme == "https" and next_url.scheme != "https":
                                raise SourceFetchError("HTTPS source cannot redirect to HTTP")
                            if target.origin() != next_url.origin():
                                request_headers = {"Accept-Encoding": "identity"}
                            target = next_url
                            # Do not drain/decompress untrusted redirect bodies.
                            response.close()
                            continue
                        content = None if response.status == 304 else await read_bounded(
                            response, max_bytes,
                        )
                        return (
                            response.status, response.headers.get("ETag"),
                            response.headers.get("Last-Modified"), content,
                        )
        finally:
            await resolver.close()

    async with _SLOTS.acquire():
        # One deadline across DNS, headers, streaming and every redirect.
        return await asyncio.wait_for(fetch(), timeout=timeout_seconds)
