import json
import socket
import urllib.error

import pytest

from rssbot.web_summarize import (
    _extract_text_from_reddit_json,
    _next_reddit_fallback_url,
    WebSummarizationError,
    extract_readable_text,
    fetch_webpage_content,
    normalize_web_url,
    validate_web_url_for_fetch,
)


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
        "https://www.reddit.com/r/Ingress/comments/1rp5w63/pausing_opr_and_retiring_overclock/"
    )

    assert calls[0].startswith("https://www.reddit.com/")
    assert calls[1].startswith("https://old.reddit.com/")
    assert calls[2].endswith("pausing_opr_and_retiring_overclock.json?raw_json=1")
    assert page.source_url.endswith("pausing_opr_and_retiring_overclock.json?raw_json=1")
    assert "Subreddit: r/Ingress" in page.cleaned_text
    assert "Comment 1: This will affect medal progress for many players." in page.cleaned_text
