import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from rssbot.db import init_engine, session_scope, User, Feed, Item
from rssbot.rss import (
    _extract_video_id,
    _normalized_event_rows,
    _normalized_ics_event_rows,
    compute_available_at,
    fetch_and_store_event_source,
    fetch_and_store_latest_item,
)


def test_extract_video_id_variants():
    entry1 = {"id": "yt:video:VIDEO123"}
    assert _extract_video_id(entry1) == "VIDEO123"

    entry2 = {"link": "https://www.youtube.com/watch?v=ABCDEF12345&feature=youtu.be"}
    assert _extract_video_id(entry2) == "ABCDEF12345"


def test_fetch_and_store_latest_item(monkeypatch, tmp_path):
    # Initialize isolated DB
    db_path = tmp_path / "bot.sqlite"
    init_engine(db_path)

    # Create user+feed
    with session_scope() as s:
        user = User(chat_id=123, tz="UTC")
        s.add(user)
        s.flush()
        feed = Feed(user_id=user.id, url="https://example/youtube/rss", enabled=True, mode="immediate")
        s.add(feed)
        s.flush()
        feed_id = feed.id

    # Provide a minimal YouTube-like Atom feed with two entries
    xml = (
        """<?xml version='1.0' encoding='UTF-8'?>
        <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>yt:video:VID2</id>
            <link rel="alternate" href="https://www.youtube.com/watch?v=VID2"/>
            <title>Second</title>
            <published>2024-01-02T00:00:00+00:00</published>
            <author><name>Channel</name></author>
          </entry>
          <entry>
            <id>yt:video:VID1</id>
            <link rel="alternate" href="https://www.youtube.com/watch?v=VID1"/>
            <title>First</title>
            <published>2024-01-01T00:00:00+00:00</published>
            <author><name>Channel</name></author>
          </entry>
        </feed>
        """
    ).encode("utf-8")

    async def fake_fetch_http(feed):
        return 200, "etag123", "Tue, 01 Jan 2024 00:00:00 GMT", xml

    from rssbot import rss as rss_mod

    monkeypatch.setattr(rss_mod, "fetch_feed_http", fake_fetch_http)

    # Run the coroutine without needing pytest-asyncio
    item_id = asyncio.run(fetch_and_store_latest_item(feed_id))
    assert isinstance(item_id, int)

    # Verify only one item stored and it's the latest (VID2)
    with session_scope() as s:
        items = s.query(Item).all()
        assert len(items) == 1
        assert items[0].external_id == "VID2"

    # Second call should not duplicate
    item_id2 = asyncio.run(fetch_and_store_latest_item(feed_id))
    assert item_id2 is None
    with session_scope() as s:
        assert s.query(Item).count() == 1


def test_normalized_event_rows_accepts_array_and_object():
    tz = ZoneInfo("Europe/Moscow")
    payload_obj = {
        "events": [
            {
                "id": "evt-1",
                "title": "Event One",
                "link": "https://example.com/1",
                "start_at": "2026-02-10T19:30:00+03:00",
            }
        ]
    }
    rows_obj = _normalized_event_rows(payload_obj, tz)
    assert len(rows_obj) == 1
    assert rows_obj[0]["external_id"] == "evt-1"
    assert rows_obj[0]["title"] == "Event One"
    assert rows_obj[0]["link"] == "https://example.com/1"
    assert rows_obj[0]["published_at"] == datetime(2026, 2, 10, 16, 30, tzinfo=timezone.utc)

    payload_arr = [
        {
            "title": "Event Two",
            "url": "https://example.com/2",
            "start_at": "2026-02-11T20:00:00+03:00",
        }
    ]
    rows_arr = _normalized_event_rows(payload_arr, tz)
    assert len(rows_arr) == 1
    assert rows_arr[0]["title"] == "Event Two"
    assert rows_arr[0]["link"] == "https://example.com/2"
    assert rows_arr[0]["external_id"]


def test_normalized_ics_event_rows_supports_url_description_and_tzid():
    tz = ZoneInfo("UTC")
    ics_payload = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:event-1@example.com\r\n"
        "DTSTART:20260210T163000Z\r\n"
        "SUMMARY:Event One\r\n"
        "URL:https://example.com/one\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:event-2@example.com\r\n"
        "DTSTART;TZID=Europe/Moscow:20260210T193000\r\n"
        "SUMMARY:Event Two\r\n"
        "DESCRIPTION:Join here https://example.com/two\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode("utf-8")

    rows = _normalized_ics_event_rows(ics_payload, tz, fallback_link="https://example.com/fallback")
    assert len(rows) == 2
    assert rows[0]["external_id"] == "event-1@example.com"
    assert rows[0]["title"] == "Event One"
    assert rows[0]["link"] == "https://example.com/one"
    assert rows[0]["published_at"] == datetime(2026, 2, 10, 16, 30, tzinfo=timezone.utc)
    assert rows[1]["external_id"] == "event-2@example.com"
    assert rows[1]["title"] == "Event Two"
    assert rows[1]["link"] == "https://example.com/two"
    assert rows[1]["published_at"] == datetime(2026, 2, 10, 16, 30, tzinfo=timezone.utc)


def test_fetch_and_store_event_source(monkeypatch, tmp_path):
    db_path = tmp_path / "bot.sqlite"
    init_engine(db_path)

    with session_scope() as s:
        user = User(chat_id=777, tz="UTC")
        s.add(user)
        s.flush()
        feed = Feed(
            user_id=user.id,
            url="https://example/events.json",
            type="event_json",
            enabled=True,
            mode="immediate",
            poll_interval_min=1,
        )
        s.add(feed)
        s.flush()
        feed_id = feed.id

    payload = (
        '{"events":[{"id":"evt-1","title":"Event One","link":"https://example.com/1",'
        '"start_at":"2026-02-10T19:30:00+03:00"}]}'
    ).encode("utf-8")

    async def fake_fetch_http(feed):
        return 200, "etag-e", "Wed, 10 Feb 2026 16:00:00 GMT", payload

    from rssbot import rss as rss_mod

    monkeypatch.setattr(rss_mod, "fetch_feed_http", fake_fetch_http)

    created_ids = asyncio.run(fetch_and_store_event_source(feed_id))
    assert len(created_ids) == 1

    with session_scope() as s:
        items = s.query(Item).filter(Item.feed_id == feed_id).all()
        assert len(items) == 1
        assert items[0].external_id == "evt-1"
        assert items[0].published_at == datetime(2026, 2, 10, 16, 30)


def test_fetch_and_store_event_source_ics(monkeypatch, tmp_path):
    db_path = tmp_path / "bot.sqlite"
    init_engine(db_path)

    with session_scope() as s:
        user = User(chat_id=888, tz="UTC")
        s.add(user)
        s.flush()
        feed = Feed(
            user_id=user.id,
            url="https://example/events.ics",
            type="event_ics",
            enabled=True,
            mode="immediate",
            poll_interval_min=1,
        )
        s.add(feed)
        s.flush()
        feed_id = feed.id

    payload = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:ics-evt-1\r\n"
        "DTSTART:20260210T163000Z\r\n"
        "SUMMARY:ICS Event One\r\n"
        "URL:https://example.com/ics/1\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode("utf-8")

    async def fake_fetch_http(feed):
        return 200, "etag-ics", "Wed, 10 Feb 2026 16:00:00 GMT", payload

    from rssbot import rss as rss_mod

    monkeypatch.setattr(rss_mod, "fetch_feed_http", fake_fetch_http)

    created_ids = asyncio.run(fetch_and_store_event_source(feed_id))
    assert len(created_ids) == 1

    with session_scope() as s:
        items = s.query(Item).filter(Item.feed_id == feed_id).all()
        assert len(items) == 1
        assert items[0].external_id == "ics-evt-1"
        assert items[0].title == "ICS Event One"
        assert items[0].link == "https://example.com/ics/1"
        assert items[0].published_at == datetime(2026, 2, 10, 16, 30)


def test_compute_available_at_normalizes_naive_datetime():
    naive_published = datetime(2026, 2, 10, 16, 30)
    available = compute_available_at("No date in title", naive_published)
    assert available is not None
    assert available.tzinfo is not None
    assert available.utcoffset() == timezone.utc.utcoffset(available)
