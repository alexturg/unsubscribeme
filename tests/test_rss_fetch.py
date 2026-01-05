from __future__ import annotations

import asyncio
from pathlib import Path

from rssbot import rss as rss_mod
from rssbot.db import Feed, Item, User, init_engine, session_scope
from rssbot.rss import fetch_and_store_feed, fetch_and_store_recent


def _init_db_with_feed(tmp_path: Path) -> int:
    db_path = tmp_path / "bot.sqlite"
    init_engine(db_path)
    with session_scope() as s:
        user = User(chat_id=123, tz="UTC")
        s.add(user)
        s.flush()
        feed = Feed(user_id=user.id, url="https://example/rss", enabled=True, mode="immediate")
        s.add(feed)
        s.flush()
        return feed.id


def test_fetch_and_store_feed_updates_name_and_items(monkeypatch, tmp_path: Path) -> None:
    feed_id = _init_db_with_feed(tmp_path)
    xml = (
        """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>My Feed</title>
          <entry>
            <id>yt:video:VID1</id>
            <title>First</title>
            <published>2024-01-01T00:00:00+00:00</published>
            <link rel="alternate" href="https://www.youtube.com/watch?v=VID1"/>
          </entry>
          <entry>
            <id>yt:video:VID2</id>
            <title>Second</title>
            <published>2024-01-02T00:00:00+00:00</published>
            <link rel="alternate" href="https://www.youtube.com/watch?v=VID2"/>
          </entry>
        </feed>
        """
    ).encode("utf-8")

    async def fake_fetch_http(feed):
        return 200, "etag1", "lm1", xml

    monkeypatch.setattr(rss_mod, "fetch_feed_http", fake_fetch_http)

    new_ids = asyncio.run(fetch_and_store_feed(feed_id))
    assert len(new_ids) == 2

    with session_scope() as s:
        f = s.get(Feed, feed_id)
        assert f.name == "My Feed"
        assert f.http_etag == "etag1"
        assert f.http_last_modified == "lm1"
        items = s.query(Item).filter(Item.feed_id == feed_id).all()
        assert {it.external_id for it in items} == {"VID1", "VID2"}


def test_fetch_and_store_recent_respects_limit(monkeypatch, tmp_path: Path) -> None:
    feed_id = _init_db_with_feed(tmp_path)
    xml = (
        """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>Recent Feed</title>
          <entry>
            <id>yt:video:VID1</id>
            <title>First</title>
            <published>2024-01-01T00:00:00+00:00</published>
            <link rel="alternate" href="https://www.youtube.com/watch?v=VID1"/>
          </entry>
          <entry>
            <id>yt:video:VID2</id>
            <title>Second</title>
            <published>2024-01-02T00:00:00+00:00</published>
            <link rel="alternate" href="https://www.youtube.com/watch?v=VID2"/>
          </entry>
          <entry>
            <id>yt:video:VID3</id>
            <title>Third</title>
            <published>2024-01-03T00:00:00+00:00</published>
            <link rel="alternate" href="https://www.youtube.com/watch?v=VID3"/>
          </entry>
        </feed>
        """
    ).encode("utf-8")

    async def fake_fetch_http(feed):
        return 200, None, None, xml

    monkeypatch.setattr(rss_mod, "fetch_feed_http", fake_fetch_http)

    new_ids = asyncio.run(fetch_and_store_recent(feed_id, limit=2))
    assert len(new_ids) == 2

    with session_scope() as s:
        items = s.query(Item).filter(Item.feed_id == feed_id).all()
        assert {it.external_id for it in items} == {"VID2", "VID3"}

    new_ids_again = asyncio.run(fetch_and_store_recent(feed_id, limit=2))
    assert len(new_ids_again) == 1

    with session_scope() as s:
        items = s.query(Item).filter(Item.feed_id == feed_id).all()
        assert {it.external_id for it in items} == {"VID1", "VID2", "VID3"}
