import asyncio
from datetime import timezone

from rssbot.db import init_engine, session_scope, User, Feed, Item
from rssbot.rss import _extract_video_id, fetch_and_store_latest_item


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

