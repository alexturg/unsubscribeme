#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from html.parser import HTMLParser
import os
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CHANNEL_ID_RE = re.compile(r"(UC[a-zA-Z0-9_-]{22})")

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
    # Recommendations contain many unrelated channel IDs. Only page ownership
    # metadata may identify the channel being viewed.
    class PageMetadata(HTMLParser):
        canonical_id: str | None = None
        channel_id: str | None = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
                parsed = urlparse(attrs.get("href") or "")
                host = (parsed.hostname or "").lower()
                if host == "youtube.com" or host.endswith(".youtube.com"):
                    self.canonical_id = extract_from_path(parsed.path)
            if tag == "meta" and attrs.get("itemprop") == "channelId":
                value = attrs.get("content") or ""
                if CHANNEL_ID_RE.fullmatch(value):
                    self.channel_id = value

    metadata = PageMetadata()
    metadata.feed(html)
    if metadata.canonical_id:
        return metadata.canonical_id
    markers = ("var ytInitialData =", "ytInitialData =", "window['ytInitialData'] =", 'window["ytInitialData"] =')
    for marker in markers:
        pos = html.find(marker)
        if pos < 0:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(html[pos + len(marker):].lstrip())
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        page_metadata = data.get("metadata")
        renderer = page_metadata.get("channelMetadataRenderer") if isinstance(page_metadata, dict) else None
        value = renderer.get("externalId") if isinstance(renderer, dict) else None
        if isinstance(value, str) and CHANNEL_ID_RE.fullmatch(value):
            return value
    if metadata.channel_id:
        return metadata.channel_id
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
