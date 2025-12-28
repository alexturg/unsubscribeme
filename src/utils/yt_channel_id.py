#!/usr/bin/env python3
from __future__ import annotations
import argparse
import os
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CHANNEL_ID_RE = re.compile(r"(UC[a-zA-Z0-9_-]{22})")

HTML_PATTERNS = [
    r'itemprop="channelId" content="(UC[a-zA-Z0-9_-]{22})"',
    r'"channelId":"(UC[a-zA-Z0-9_-]{22})"',
    r'"externalId":"(UC[a-zA-Z0-9_-]{22})"',
    r'"browseId":"(UC[a-zA-Z0-9_-]{22})"',
    r'<link rel="canonical" href="https?://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"',
]


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("empty url")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def extract_from_path(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    if parts[0] == "channel" and len(parts) > 1:
        if CHANNEL_ID_RE.fullmatch(parts[1]):
            return parts[1]
    if CHANNEL_ID_RE.fullmatch(parts[0]):
        return parts[0]
    return None


def build_ssl_context(insecure: bool, ca_bundle: str | None) -> ssl.SSLContext:
    if insecure:
        return ssl._create_unverified_context()
    if ca_bundle:
        if not os.path.isfile(ca_bundle):
            raise ValueError(f"CA bundle not found: {ca_bundle}")
        return ssl.create_default_context(cafile=ca_bundle)
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch_html(url: str, timeout: float, insecure: bool, ca_bundle: str | None) -> tuple[str, str]:
    context = build_ssl_context(insecure, ca_bundle)
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/119.0 Safari/537.36"
            )
        },
    )
    with urlopen(req, timeout=timeout, context=context) as resp:
        html = resp.read().decode("utf-8", "ignore")
        final_url = resp.geturl()
    return html, final_url


def extract_from_html(html: str) -> str | None:
    for pattern in HTML_PATTERNS:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    match = CHANNEL_ID_RE.search(html)
    if match:
        return match.group(1)
    return None


def get_channel_id(
    url: str,
    timeout: float = 10.0,
    insecure: bool = False,
    ca_bundle: str | None = None,
) -> str | None:
    url = normalize_url(url)
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("invalid url")

    channel_id = extract_from_path(parsed.path)
    if channel_id:
        return channel_id

    html, final_url = fetch_html(url, timeout, insecure, ca_bundle)
    channel_id = extract_from_path(urlparse(final_url).path)
    if channel_id:
        return channel_id

    return extract_from_html(html)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract YouTube channel_id from a channel URL or handle."
    )
    parser.add_argument("url", help="YouTube channel URL, handle, or custom link")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--ca-bundle",
        help="Path to a CA bundle PEM file for TLS verification.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (use only if needed).",
    )
    args = parser.parse_args()

    try:
        channel_id = get_channel_id(
            args.url,
            timeout=args.timeout,
            insecure=args.insecure,
            ca_bundle=args.ca_bundle,
        )
    except (ValueError, HTTPError, URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not channel_id:
        print("channel id not found", file=sys.stderr)
        return 1

    print(channel_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
