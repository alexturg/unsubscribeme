import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rssbot import bot
from rssbot import scheduler as scheduler_mod
from rssbot.db import Delivery, Feed, Item, User, init_engine, session_scope
from rssbot.scheduler import BotScheduler


def test_digest_without_feeds_deletes_command(monkeypatch, tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    message = SimpleNamespace(text="/digest", answer=AsyncMock(), delete=AsyncMock())
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: 1)

    asyncio.run(bot.cmd_digest(message))

    message.answer.assert_awaited_once_with(
        "Нет активных лент в режиме digest или on_demand."
    )
    message.delete.assert_awaited_once()


@pytest.mark.parametrize("command", ["/digest", "/digest all", "/digest@mybot  "])
@pytest.mark.parametrize("sent", [0, 2])
def test_digest_all_only_uses_subscription_sources(monkeypatch, tmp_path, command, sent):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as s:
        user = User(chat_id=123, tz="UTC")
        s.add(user)
        s.flush()
        user_id = user.id
        event_feed_id = None
        for source_type, mode in (
            ("youtube", "digest"),
            ("youtube", "on_demand"),
            ("youtube", "immediate"),
            ("event_ics", "digest"),
            ("event_json", "digest"),
            ("event_manual", "digest"),
        ):
            feed = Feed(user_id=user_id, url=f"source:{source_type}:{mode}", type=source_type, mode=mode)
            s.add(feed)
            s.flush()
            if source_type == "event_ics":
                event_feed_id = feed.id

    send = AsyncMock(return_value=sent)
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: user_id)
    monkeypatch.setattr(bot, "DEPS", SimpleNamespace(scheduler=SimpleNamespace(_send_digest_for_feed=send)))
    message = SimpleNamespace(text=command, answer=AsyncMock(), delete=AsyncMock())

    asyncio.run(bot.cmd_digest(message))

    assert send.await_count == 2
    assert all(call.kwargs == {"update_last_digest_at": False} for call in send.await_args_list)
    message.answer.assert_not_awaited()
    message.delete.assert_awaited_once()

    message.text = f"/digest {event_feed_id}"
    message.answer.reset_mock()
    asyncio.run(bot.cmd_digest(message))
    message.answer.assert_awaited_once_with("Для источников событий дайджест недоступен.")
    assert send.await_count == 2
    message.delete.assert_awaited_once()


def test_digest_deletion_failure_does_not_fail_command(monkeypatch, tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: 1)
    message = SimpleNamespace(
        text="/digest", answer=AsyncMock(),
        delete=AsyncMock(side_effect=RuntimeError("Cannot delete message")),
    )
    asyncio.run(bot.cmd_digest(message))
    message.answer.assert_awaited_once_with("Нет активных лент в режиме digest или on_demand.")
    message.delete.assert_awaited_once()


def test_digest_send_failure_keeps_command(monkeypatch, tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as s:
        user = User(chat_id=123, tz="UTC")
        s.add(user)
        s.flush()
        user_id = user.id
        feed = Feed(user_id=user_id, url="source:youtube", type="youtube", mode="digest")
        s.add(feed)
    send = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: user_id)
    monkeypatch.setattr(bot, "DEPS", SimpleNamespace(scheduler=SimpleNamespace(_send_digest_for_feed=send)))
    message = SimpleNamespace(text="/digest", answer=AsyncMock(), delete=AsyncMock())
    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        asyncio.run(bot.cmd_digest(message))
    message.delete.assert_not_awaited()


def test_digest_only_sends_items_published_after_subscription(monkeypatch, tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    subscribed_at = datetime.now(timezone.utc) - timedelta(days=2)
    with session_scope() as s:
        user = User(chat_id=123, tz="UTC")
        s.add(user)
        s.flush()
        feed = Feed(
            user_id=user.id,
            url="https://example.com/rss",
            type="youtube",
            mode="digest",
            created_at=subscribed_at,
        )
        s.add(feed)
        s.flush()
        feed_id = feed.id
        for external_id, published_at in (
            ("old", subscribed_at - timedelta(days=30)),
            ("undated", None),
            ("new", subscribed_at + timedelta(days=1)),
        ):
            s.add(Item(feed_id=feed_id, external_id=external_id, title=external_id,
                       link=f"https://example.com/{external_id}", published_at=published_at))

    monkeypatch.setattr(scheduler_mod, "Settings", lambda: SimpleNamespace(HIDE_FUTURE_VIDEOS=False))
    scheduler = BotScheduler(bot=SimpleNamespace())
    send = AsyncMock(return_value=("ok", None))
    monkeypatch.setattr(scheduler, "_send_video_message", send)

    assert asyncio.run(scheduler._send_digest_for_feed(feed_id, update_last_digest_at=False)) == 1
    assert send.await_count == 1
    assert send.await_args.args[1] == "new"
    assert asyncio.run(scheduler._send_digest_for_feed(feed_id, update_last_digest_at=False)) == 0
    with session_scope() as s:
        deliveries = s.query(Delivery).filter(Delivery.feed_id == feed_id).all()
        assert len(deliveries) == 1
