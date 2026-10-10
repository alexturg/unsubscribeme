import json
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
import time

import pytest

from rssbot.web_summarize import (
    _REDDIT_CONTENT_CACHE,
    _reddit_content_cache_key,
    _write_reddit_disk_cache,
    _extract_text_from_reddit_json,
    _extract_text_from_xml_feed,
    _looks_like_reddit_access_block,
    _next_reddit_fallback_url,
    _open_web_request,
    WebSummarizationError,
    WebPageContent,
    DEFAULT_USER_AGENT,
    REDDIT_RSS_USER_AGENT,
    extract_readable_text,
    fetch_webpage_content,
    normalize_web_url,
    validate_web_url_for_fetch,
)


@pytest.fixture(autouse=True)
def clear_reddit_content_cache():
    _REDDIT_CONTENT_CACHE.clear()
    yield
    _REDDIT_CONTENT_CACHE.clear()


def test_normalize_web_url_adds_https_and_removes_fragment():
    assert normalize_web_url("example.com/path#section") == "https://example.com/path"


def test_normalize_web_url_encodes_unicode_path():
    url = "https://ru.wikipedia.org/wiki/Драммонд,_Маргарет"
    normalized = normalize_web_url(url)
    assert normalized.startswith("https://ru.wikipedia.org/wiki/")
    assert "Драммонд" not in normalized
    assert "%D0%94%D1%80%D0%B0%D0%BC%D0%BC%D0%BE%D0%BD%D0%B4" in normalized


def test_validate_web_url_for_fetch_blocks_loopback_ip():
    with pytest.raises(WebSummarizationError):
        validate_web_url_for_fetch("http://127.0.0.1/private")


def test_validate_web_url_for_fetch_blocks_private_dns(monkeypatch):
    def fake_getaddrinfo(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]

    monkeypatch.setattr("rssbot.web_summarize.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(WebSummarizationError):
        validate_web_url_for_fetch("https://internal.example.com/report")


def test_validate_web_url_for_fetch_allows_public_dns(monkeypatch):
    def fake_getaddrinfo(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr("rssbot.web_summarize.socket.getaddrinfo", fake_getaddrinfo)

    assert (
        validate_web_url_for_fetch("https://public.example.com/report")
        == "https://public.example.com/report"
    )


def test_extract_readable_text_drops_noise_and_scripts():
    html = """
    <html>
      <head>
        <title>Quarterly Report</title>
        <meta name="description" content="Revenue and margin trends overview." />
      </head>
      <body>
        <nav>Subscribe for updates</nav>
        <article>
          <h1>Q4 Results</h1>
          <p>Revenue increased by 20 percent year-over-year due to enterprise deals.</p>
          <p>Operating margin improved after reducing infrastructure costs.</p>
        </article>
        <script>console.log("tracking")</script>
        <footer>Privacy policy</footer>
      </body>
    </html>
    """

    title, cleaned = extract_readable_text(html, max_words=140)

    assert title == "Quarterly Report"
    assert "Title: Quarterly Report" in cleaned
    assert "Revenue increased by 20 percent year-over-year" in cleaned
    assert "Operating margin improved" in cleaned
    assert "subscribe for updates" not in cleaned.lower()
    assert "privacy policy" not in cleaned.lower()
    assert "tracking" not in cleaned.lower()


def test_next_reddit_fallback_url_prefers_old_reddit_host():
    url = "https://www.reddit.com/r/Ingress/comments/1rp5w63/pausing_opr_and_retiring_overclock/"
    fallback = _next_reddit_fallback_url(url)
    assert (
        fallback
        == "https://old.reddit.com/r/Ingress/comments/1rp5w63/pausing_opr_and_retiring_overclock/"
    )


def test_next_reddit_fallback_url_does_not_rewrite_share_path():
    assert _next_reddit_fallback_url("https://www.reddit.com/r/Ingress/s/BGHThr4vvc") is None


def test_concurrent_reddit_fetches_reuse_the_first_download(monkeypatch):
    url = "https://www.reddit.com/r/PlannerAddicts/comments/1x25ecl/post/"
    first_started = threading.Event()
    second_started = threading.Event()
    calls = []
    class Response(BytesIO):
        headers = {"Content-Type": "application/atom+xml"}
        def geturl(self):
            return url + ".rss"
    class Opener:
        def open(self, request, timeout=None):
            calls.append(request.full_url)
            first_started.set()
            assert second_started.wait(2)
            time.sleep(0.05)
            return Response(b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Post</title><entry><summary>Full source text.</summary></entry></feed>')
    monkeypatch.setattr("rssbot.web_summarize.urllib.request.build_opener", lambda *_: Opener())
    monkeypatch.setattr("rssbot.web_summarize.socket.getaddrinfo",
                        lambda host, port, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    def second_fetch():
        second_started.set()
        return fetch_webpage_content(url)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(fetch_webpage_content, url)
        assert first_started.wait(2)
        second = executor.submit(second_fetch)
        assert first.result(timeout=3) == second.result(timeout=3)
    assert calls == [url + ".rss"]


def test_reddit_rate_limit_retries_same_request_once(monkeypatch):
    request = urllib.request.Request("https://www.reddit.com/r/Ingress/comments/abc/.rss")
    calls = []
    pauses = []
    response = object()

    class FakeOpener:
        def open(self, req, timeout=None):
            calls.append(req)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Limited", {"Retry-After": "3"}, None)
            return response

    monkeypatch.setattr("rssbot.web_summarize.time.sleep", pauses.append)
    assert _open_web_request(FakeOpener(), request, 15) is response
    assert calls == [request, request]
    assert pauses == [3.0]


@pytest.mark.parametrize("retry_after,expected_calls", [(None, 2), ("120", 1)])
def test_reddit_rate_limit_retry_is_bounded(monkeypatch, retry_after, expected_calls):
    request = urllib.request.Request("https://www.reddit.com/r/Ingress/comments/abc/.rss")
    calls = []

    class FakeOpener:
        def open(self, req, timeout=None):
            calls.append(req)
            headers = {"Retry-After": retry_after} if retry_after else {}
            raise urllib.error.HTTPError(req.full_url, 429, "Limited", headers, None)

    pauses = []
    monkeypatch.setattr("rssbot.web_summarize.time.sleep", pauses.append)
    with pytest.raises(urllib.error.HTTPError):
        _open_web_request(FakeOpener(), request, 15)
    assert len(calls) == expected_calls
    assert pauses == ([60] if retry_after is None else [])


@pytest.mark.parametrize("share_get_status", [301, 403, 429])
@pytest.mark.parametrize("use_share_url", [True, False])
def test_fetch_webpage_content_resolves_reddit_share_url(monkeypatch, tmp_path, share_get_status, use_share_url):
    share_url = "https://www.reddit.com/r/Ingress/s/BGHThr4vvc"
    canonical_url = (
        "https://www.reddit.com/r/Ingress/comments/1x0qg70/"
        "whats_the_deal_with_a_passed_agents_account/"
    )
    redirect_url = canonical_url + "?share_id=abc&utm_source=share"
    calls = []

    class FakeResponse:
        headers = {"Content-Type": "application/atom+xml; charset=utf-8"}

        def __init__(self, url):
            self.url = url
            self.payload = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Ingress post</title><entry><summary>Agent account discussion.</summary></entry></feed>'

        def geturl(self):
            return self.url

        def read(self, n=-1):
            payload, self.payload = self.payload[:n], self.payload[n:]
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class FakeOpener:
        def open(self, request, timeout=None):
            calls.append((request.get_method(), request.full_url))
            if request.full_url == share_url:
                if request.get_method() == "HEAD":
                    assert request.get_header("User-agent") == "Twitterbot"
                if request.get_method() == "HEAD" or share_get_status == 301:
                    raise urllib.error.HTTPError(
                        share_url, 301, "Moved", hdrs={"Location": redirect_url}, fp=None
                    )
                raise urllib.error.HTTPError(share_url, share_get_status, "Blocked", hdrs={}, fp=None)
            assert request.full_url == canonical_url + ".rss"
            assert request.get_header("User-agent") == REDDIT_RSS_USER_AGENT
            assert request.get_header("Accept") == "application/atom+xml,application/rss+xml"
            return FakeResponse(request.full_url)

    def fake_getaddrinfo(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(
        "rssbot.web_summarize.urllib.request.build_opener",
        lambda *_args, **_kwargs: FakeOpener(),
    )
    monkeypatch.setattr("rssbot.web_summarize.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("rssbot.web_summarize.time.sleep", lambda _delay: None)

    page = fetch_webpage_content(share_url if use_share_url else canonical_url, cache_dir=tmp_path)

    assert page.source_url == canonical_url + ".rss"
    assert "Agent account discussion." in page.cleaned_text
    assert calls[-1] == ("GET", canonical_url + ".rss")
    if not use_share_url:
        assert len(calls) == 1
    assert all("old.reddit.com" not in url for _, url in calls)
    request_count = len(calls)
    assert fetch_webpage_content(canonical_url) == page
    assert fetch_webpage_content(share_url) == page
    if use_share_url:
        assert len(calls) == request_count
    # A newly seen share alias still needs to resolve once, but never refetches RSS.
    assert sum(url.endswith(".rss") for _, url in calls) == 1
    fetch_webpage_content(share_url, cache_dir=tmp_path)
    request_count = len(calls)
    _REDDIT_CONTENT_CACHE.clear()
    assert fetch_webpage_content(canonical_url, cache_dir=tmp_path) == page
    if use_share_url:
        assert fetch_webpage_content(share_url, cache_dir=tmp_path) == page
        assert len(calls) == request_count


@pytest.mark.parametrize("age", [0, 600, 90000])
def test_reddit_disk_cache_survives_restart_and_handles_rate_limit(monkeypatch, tmp_path, age):
    url = "https://www.reddit.com/r/Ingress/comments/abc/post/"
    page = WebPageContent(url, "Post title", "Actual post text and comments.")
    key = _reddit_content_cache_key(url, 2_000_000, 4500, DEFAULT_USER_AGENT)
    _write_reddit_disk_cache(key, page, tmp_path)
    _REDDIT_CONTENT_CACHE.clear()
    import time
    now = time.time()
    monkeypatch.setattr("rssbot.web_summarize.time.time", lambda: now + age)
    monkeypatch.setattr("rssbot.web_summarize.time.sleep", lambda _delay: None)
    monkeypatch.setattr(
        "rssbot.web_summarize.socket.getaddrinfo",
        lambda host, port, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    calls = []

    class BlockedOpener:
        def open(self, request, timeout=None):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 429, "Limited", {}, None)

    monkeypatch.setattr("rssbot.web_summarize.urllib.request.build_opener", lambda *_: BlockedOpener())
    if age >= 86400:
        with pytest.raises(WebSummarizationError, match="429"):
            fetch_webpage_content(url, cache_dir=tmp_path)
    else:
        result = fetch_webpage_content(url, cache_dir=tmp_path)
        assert page.cleaned_text in result.cleaned_text
        assert bool(calls) == (age >= 300)
        assert ("newer comments may be missing" in result.cleaned_text) == (age >= 300)


def test_next_reddit_fallback_url_switches_old_reddit_to_json():
    url = (
        "https://old.reddit.com/r/Ingress/comments/1rp5w63/"
        "pausing_opr_and_retiring_overclock/?sort=top"
    )
    fallback = _next_reddit_fallback_url(url)
    assert (
        fallback
        == "https://old.reddit.com/r/Ingress/comments/1rp5w63/pausing_opr_and_retiring_overclock.json"
        "?sort=top&raw_json=1"
    )


def test_next_reddit_fallback_url_switches_json_to_rss():
    url = (
        "https://old.reddit.com/r/Ingress/comments/1rp5w63/"
        "pausing_opr_and_retiring_overclock.json?sort=top&raw_json=1"
    )
    fallback = _next_reddit_fallback_url(url)
    assert (
        fallback
        == "https://www.reddit.com/r/Ingress/comments/1rp5w63/pausing_opr_and_retiring_overclock/.rss"
        "?sort=top"
    )


def test_looks_like_reddit_access_block_detects_known_message():
    blocked = """
    You've been blocked by network security.
    You are unable to access reddit.com.
    """
    assert _looks_like_reddit_access_block(blocked) is True


def test_extract_text_from_reddit_json_collects_post_and_comments():
    payload = [
        {
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "title": "Pausing OPR and retiring Overclock",
                            "selftext": "Niantic announced that OPR will be paused.",
                            "subreddit": "Ingress",
                            "author": "agent42",
                        },
                    }
                ]
            }
        },
        {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "body": "This will affect medal progress for many players.",
                            "replies": {
                                "data": {
                                    "children": [
                                        {
                                            "kind": "t1",
                                            "data": {"body": "Hope this returns in a better form."},
                                        }
                                    ]
                                }
                            },
                        },
                    }
                ]
            }
        },
    ]

    title, cleaned = _extract_text_from_reddit_json(json.dumps(payload), max_words=200)

    assert title == "Pausing OPR and retiring Overclock"
    assert "Subreddit: r/Ingress" in cleaned
    assert "Author: u/agent42" in cleaned
    assert "Post: Niantic announced that OPR will be paused." in cleaned
    assert "Comment 1: This will affect medal progress for many players." in cleaned
    assert "Comment 2: Hope this returns in a better form." in cleaned


def test_extract_text_from_xml_feed_collects_entries():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Ingress discussion</title>
      <entry>
        <title>Pausing OPR and retiring Overclock</title>
        <author><name>agent42</name></author>
        <content>This will affect medal progress for many players.</content>
      </entry>
      <entry>
        <title>Second thought</title>
        <summary>Hope this returns in a better form.</summary>
      </entry>
    </feed>
    """
    title, cleaned = _extract_text_from_xml_feed(xml, max_words=200)

    assert title == "Ingress discussion"
    assert "Title: Ingress discussion" in cleaned
    assert "Entry 1: Pausing OPR and retiring Overclock - by agent42." in cleaned
    assert "This will affect medal progress for many players." in cleaned
    assert "Entry 2: Second thought." in cleaned
    assert "Hope this returns in a better form." in cleaned


def test_fetch_webpage_content_reddit_403_fallbacks_to_old_and_json(monkeypatch):
    calls: list[str] = []
    payload = json.dumps(
        [
            {
                "data": {
                    "children": [
                        {
                            "kind": "t3",
                            "data": {
                                "title": "Pausing OPR and retiring Overclock",
                                "selftext": "Niantic announced that OPR will be paused.",
                                "subreddit": "Ingress",
                                "author": "agent42",
                            },
                        }
                    ]
                }
            },
            {
                "data": {
                    "children": [
                        {
                            "kind": "t1",
                            "data": {"body": "This will affect medal progress for many players."},
                        }
                    ]
                }
            },
        ]
    ).encode("utf-8")

    class FakeResponse:
        def __init__(self, url: str) -> None:
            self._payload = payload
            self._offset = 0
            self._url = url
            self.headers = {"Content-Type": "application/json; charset=utf-8"}

        def geturl(self) -> str:
            return self._url

        def read(self, n: int = -1) -> bytes:
            if self._offset >= len(self._payload):
                return b""
            if n is None or n < 0:
                n = len(self._payload) - self._offset
            chunk = self._payload[self._offset : self._offset + n]
            self._offset += len(chunk)
            return chunk

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeOpener:
        def open(self, request, timeout=None):
            url = request.full_url
            calls.append(url)
            if len(calls) <= 2:
                raise urllib.error.HTTPError(url, 403, "Forbidden", hdrs={}, fp=None)
            return FakeResponse(url)

    def fake_getaddrinfo(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(
        "rssbot.web_summarize.urllib.request.build_opener",
        lambda *_args, **_kwargs: FakeOpener(),
    )
    monkeypatch.setattr("rssbot.web_summarize.socket.getaddrinfo", fake_getaddrinfo)

    page = fetch_webpage_content(
        "https://www.reddit.com/r/Ingress/"
    )

    assert calls[0].startswith("https://www.reddit.com/")
    assert calls[1].startswith("https://old.reddit.com/")
    assert calls[2].endswith("/Ingress.json?raw_json=1")
    assert page.source_url.endswith("/Ingress.json?raw_json=1")
    assert "Subreddit: r/Ingress" in page.cleaned_text
    assert "Comment 1: This will affect medal progress for many players." in page.cleaned_text


@pytest.mark.parametrize("blocked_status", [403, 429])
def test_fetch_webpage_content_reddit_block_fallbacks_to_rss(monkeypatch, blocked_status):
    calls: list[str] = []
    xml_payload = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Ingress discussion</title>
      <entry>
        <title>Pausing OPR and retiring Overclock</title>
        <summary>This will affect medal progress for many players.</summary>
      </entry>
    </feed>
    """

    class FakeResponse:
        def __init__(self, url: str) -> None:
            self._payload = xml_payload
            self._offset = 0
            self._url = url
            self.headers = {"Content-Type": "application/atom+xml; charset=utf-8"}

        def geturl(self) -> str:
            return self._url

        def read(self, n: int = -1) -> bytes:
            if self._offset >= len(self._payload):
                return b""
            if n is None or n < 0:
                n = len(self._payload) - self._offset
            chunk = self._payload[self._offset : self._offset + n]
            self._offset += len(chunk)
            return chunk

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeOpener:
        def open(self, request, timeout=None):
            url = request.full_url
            calls.append(url)
            if "/.rss" not in url:
                raise urllib.error.HTTPError(url, blocked_status, "Blocked", hdrs={}, fp=None)
            if "raw_json=" in url:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", hdrs={}, fp=None)
            return FakeResponse(url)

    def fake_getaddrinfo(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(
        "rssbot.web_summarize.urllib.request.build_opener",
        lambda *_args, **_kwargs: FakeOpener(),
    )
    monkeypatch.setattr("rssbot.web_summarize.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("rssbot.web_summarize.time.sleep", lambda _delay: None)

    page = fetch_webpage_content(
        "https://old.reddit.com/r/Ingress/comments/1rp5w63/pausing_opr_and_retiring_overclock.json?raw_json=1"
    )

    distinct_calls = list(dict.fromkeys(calls))
    assert len(distinct_calls) == 2
    assert distinct_calls[0].startswith("https://old.reddit.com/")
    assert ".json?raw_json=1" in distinct_calls[0]
    assert distinct_calls[1].startswith("https://www.reddit.com/")
    assert distinct_calls[1].endswith("/.rss")
    assert "Pausing OPR and retiring Overclock" in page.cleaned_text
