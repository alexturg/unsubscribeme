import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web as aiohttp_web

from rssbot import bot, web
from rssbot.db import (
    CardEntry, Delivery, Feed, FeedBaseline, FeedRule, IcsContentState,
    Item, User, init_engine, session_scope,
)


def setup_feeds(tmp_path, monkeypatch):
    engine = init_engine(tmp_path / "bot.sqlite")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with session_scope() as session:
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        user_id = user.id
        feed_ids = []
        for number in range(2):
            feed = Feed(user_id=user_id, url=f"https://example.com/{number}", type="event_ics")
            session.add(feed)
            session.flush()
            feed_ids.append(feed.id)
            item = Item(feed_id=feed.id, external_id="video", title="Saved video")
            session.add(item)
            session.flush()
            session.add_all([
                FeedRule(feed_id=feed.id, exclude_shorts=True),
                FeedBaseline(feed_id=feed.id, baseline_item_external_id="video"),
                IcsContentState(feed_id=feed.id, content_sha256="abc", source_url=feed.url, timezone_name="UTC"),
                Delivery(feed_id=feed.id, item_id=item.id, user_id=user_id, channel="digest", status="ok"),
                CardEntry(user_id=user_id, source_key=f"item:{item.id}", kind="feed", title=item.title, status="someday"),
            ])
    scheduler = SimpleNamespace(unschedule_feed_poll=Mock())
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: user_id)
    monkeypatch.setattr(bot, "DEPS", SimpleNamespace(scheduler=scheduler))
    monkeypatch.setattr(web, "DEPS", SimpleNamespace(scheduler=scheduler))
    return feed_ids, scheduler


def assert_only_target_deleted(feed_ids):
    with session_scope() as session:
        assert session.get(Feed, feed_ids[0]) is None
        assert session.get(Feed, feed_ids[1]) is not None
        for model in (Delivery, Item, FeedRule, FeedBaseline, IcsContentState):
            assert session.query(model).filter(model.feed_id == feed_ids[0]).count() == 0
            assert session.query(model).filter(model.feed_id == feed_ids[1]).count() == 1
        assert session.query(CardEntry).filter(CardEntry.status == "someday").count() == 2


def test_remove_deletes_related_rows_and_confirms(tmp_path, monkeypatch):
    feed_ids, scheduler = setup_feeds(tmp_path, monkeypatch)
    message = SimpleNamespace(text=f"/remove {feed_ids[0]}", answer=AsyncMock())
    asyncio.run(bot.cmd_remove(message))
    message.answer.assert_awaited_once_with(f"Лента {feed_ids[0]} удалена.")
    scheduler.unschedule_feed_poll.assert_called_once_with(feed_ids[0])
    assert_only_target_deleted(feed_ids)


def test_remove_rejects_another_users_feed(tmp_path, monkeypatch):
    feed_ids, scheduler = setup_feeds(tmp_path, monkeypatch)
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: 999)
    message = SimpleNamespace(text=f"/remove {feed_ids[0]}", answer=AsyncMock())
    asyncio.run(bot.cmd_remove(message))
    message.answer.assert_awaited_once_with("Лента не найдена.")
    scheduler.unschedule_feed_poll.assert_not_called()
    with session_scope() as session:
        assert session.query(Feed).count() == 2


def test_web_remove_deletes_all_related_rows(tmp_path, monkeypatch):
    feed_ids, scheduler = setup_feeds(tmp_path, monkeypatch)
    request = SimpleNamespace(match_info={"chat_id": "77", "feed_id": str(feed_ids[0])})
    with pytest.raises(aiohttp_web.HTTPFound):
        asyncio.run(web.remove_feed(request))
    scheduler.unschedule_feed_poll.assert_called_once_with(feed_ids[0])
    assert_only_target_deleted(feed_ids)
