import socket

import pytest

from rssbot.web_summarize import (
    WebSummarizationError,
    extract_readable_text,
    normalize_web_url,
    validate_web_url_for_fetch,
)


def test_normalize_web_url_adds_https_and_removes_fragment():
    assert normalize_web_url("example.com/path#section") == "https://example.com/path"


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
