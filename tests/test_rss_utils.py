from __future__ import annotations

import hashlib
import time

import feedparser
from datetime import datetime, timezone

from rssbot.rss import _published_at, _summary_hash, compute_available_at


def test_published_at_from_struct_time() -> None:
    entry = feedparser.FeedParserDict(
        published_parsed=time.strptime("2024-01-02 03:04:05", "%Y-%m-%d %H:%M:%S")
    )
    dt = _published_at(entry)
    assert dt == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_published_at_from_iso_z() -> None:
    entry = {"published": "2024-01-02T03:04:05Z"}
    dt = _published_at(entry)
    assert dt == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_summary_hash_uses_summary_then_description() -> None:
    assert _summary_hash({"summary": ""}) is None
    assert _summary_hash({"summary": "hello"}) == hashlib.sha1(b"hello").hexdigest()
    assert _summary_hash({"summary": None, "description": "desc"}) == hashlib.sha1(b"desc").hexdigest()


def test_compute_available_at_prefers_published_when_earlier(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TZ", "UTC")
    published = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)

    title_earlier = "02.01.2025 10:00"
    assert compute_available_at(title_earlier, published) == published

    title_later = "05.01.2025 10:00"
    assert compute_available_at(title_later, published) == datetime(
        2025, 1, 5, 10, 0, tzinfo=timezone.utc
    )
