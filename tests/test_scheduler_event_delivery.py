import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

from aiogram.types import InlineKeyboardMarkup
from rssbot.db import Delivery, Feed, FeedBaseline, Item, User, init_engine, session_scope
from rssbot.scheduler import BotScheduler


class DummyBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return {"ok": True}


def test_ics_fetches_hourly_while_start_delivery_runs_each_minute(tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as s:
        user = User(chat_id=12345, tz="UTC")
        s.add(user)
        s.flush()
        feed = Feed(
            user_id=user.id,
            url="https://example.com/calendar.ics",
            type="event_ics",
            enabled=True,
            poll_interval_min=1,
        )
        s.add(feed)
        s.flush()
        feed_id = feed.id

    scheduler = BotScheduler(bot=DummyBot())
    scheduler.scheduler = Mock()
    scheduler.schedule_feed_poll(feed_id, 1)

    jobs = {call.kwargs["id"]: call.kwargs for call in scheduler.scheduler.add_job.call_args_list}
    assert jobs[f"poll:{feed_id}"]["minutes"] == 60
    assert jobs[f"event-start:{feed_id}"]["minutes"] == 1
    with session_scope() as s:
        assert s.get(Feed, feed_id).poll_interval_min == 60

    scheduler.unschedule_feed_poll(feed_id)
    removed = {call.args[0] for call in scheduler.scheduler.remove_job.call_args_list}
    assert removed == {f"poll:{feed_id}", f"event-start:{feed_id}"}


def test_ics_poll_does_not_run_start_delivery(monkeypatch, tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as s:
        user = User(chat_id=12347, tz="UTC")
        s.add(user)
        s.flush()
        feed = Feed(user_id=user.id, url="https://example.com/calendar.ics", type="event_ics")
        s.add(feed)
        s.flush()
        feed_id = feed.id

    from rssbot import scheduler as scheduler_mod

    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(scheduler_mod, "fetch_and_store_event_source", fetch)
    scheduler = BotScheduler(bot=DummyBot())
    deliver = AsyncMock(return_value=0)
    monkeypatch.setattr(scheduler, "_deliver_due_event_starts", deliver)

    asyncio.run(scheduler._poll_feed_job(feed_id))
    fetch.assert_awaited_once_with(feed_id)
    deliver.assert_not_awaited()


def test_deliver_due_event_starts_sets_baseline_and_skips_historical_on_first_run(tmp_path):
    db_path = tmp_path / "bot.sqlite"
    init_engine(db_path)

    with session_scope() as s:
        user = User(chat_id=12345, tz="UTC")
        s.add(user)
        s.flush()

        feed = Feed(
            user_id=user.id,
            url="https://example.com/calendar.ics",
            type="event_ics",
            mode="immediate",
            enabled=True,
            poll_interval_min=1,
        )
        s.add(feed)
        s.flush()

        # Intentionally naive datetime to emulate SQLite timezone-less reads.
        item = Item(
            feed_id=feed.id,
            external_id="evt-1",
            title="Event One",
            link="https://example.com/event/1",
            published_at=datetime.utcnow() - timedelta(minutes=10),
        )
        s.add(item)
        s.flush()
        feed_id = feed.id

    scheduler = BotScheduler(bot=DummyBot())
    sent = asyncio.run(scheduler._deliver_due_event_starts(feed_id))
    assert sent == 0

    with session_scope() as s:
        deliveries = s.query(Delivery).filter(Delivery.feed_id == feed_id).all()
        assert len(deliveries) == 0
        baseline = s.get(FeedBaseline, feed_id)
        assert baseline is not None
        assert baseline.baseline_published_at is not None


def test_deliver_due_event_starts_accepts_naive_item_datetime_and_no_repeat(tmp_path):
    db_path = tmp_path / "bot.sqlite"
    init_engine(db_path)

    with session_scope() as s:
        user = User(chat_id=12345, tz="UTC")
        s.add(user)
        s.flush()

        feed = Feed(
            user_id=user.id,
            url="https://example.com/calendar.ics",
            type="event_ics",
            mode="immediate",
            enabled=True,
            poll_interval_min=1,
        )
        s.add(feed)
        s.flush()
        feed_id = feed.id

        # Force baseline to an old moment to allow current due delivery.
        s.add(
            FeedBaseline(
                feed_id=feed_id,
                baseline_published_at=datetime.utcnow() - timedelta(days=2),
            )
        )

        item = Item(
            feed_id=feed_id,
            external_id="evt-1",
            title="Event One",
            link="https://example.com/event/1",
            # Intentionally naive datetime to emulate SQLite timezone-less reads.
            published_at=datetime.utcnow() - timedelta(minutes=10),
        )
        s.add(item)

    scheduler = BotScheduler(bot=DummyBot())
    first_sent = asyncio.run(scheduler._deliver_due_event_starts(feed_id))
    second_sent = asyncio.run(scheduler._deliver_due_event_starts(feed_id))
    assert first_sent == 1
    assert second_sent == 0

    with session_scope() as s:
        deliveries = s.query(Delivery).filter(Delivery.feed_id == feed_id).all()
        assert len(deliveries) == 1
        assert deliveries[0].status == "ok"


def test_deliver_due_event_starts_skips_duplicate_items_by_title_and_time(tmp_path):
    db_path = tmp_path / "bot.sqlite"
    init_engine(db_path)

    with session_scope() as s:
        user = User(chat_id=12345, tz="UTC")
        s.add(user)
        s.flush()

        feed = Feed(
            user_id=user.id,
            url="https://example.com/calendar.ics",
            type="event_ics",
            mode="immediate",
            enabled=True,
            poll_interval_min=1,
        )
        s.add(feed)
        s.flush()
        feed_id = feed.id

        s.add(
            FeedBaseline(
                feed_id=feed_id,
                baseline_published_at=datetime.utcnow() - timedelta(days=2),
            )
        )

        published_at = datetime.utcnow() - timedelta(minutes=10)
        s.add(
            Item(
                feed_id=feed_id,
                external_id="evt-1-a",
                title="Event One",
                link="https://example.com/event/1",
                published_at=published_at,
                summary_hash="hash-a",
            )
        )
        s.add(
            Item(
                feed_id=feed_id,
                external_id="evt-1-b",
                title="  Event   One  ",
                link="https://example.com/event/1?utm=2",
                published_at=published_at,
                summary_hash="hash-b",
            )
        )

    bot = DummyBot()
    scheduler = BotScheduler(bot=bot)
    sent = asyncio.run(scheduler._deliver_due_event_starts(feed_id))
    assert sent == 1
    assert len(bot.messages) == 1

    with session_scope() as s:
        deliveries = s.query(Delivery).filter(Delivery.feed_id == feed_id).all()
        assert len(deliveries) == 1
        assert deliveries[0].status == "ok"


def test_ics_delivery_ignores_old_backlog_but_sends_recent_start(tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as s:
        user = User(chat_id=12346, tz="UTC")
        s.add(user)
        s.flush()
        feed = Feed(
            user_id=user.id,
            url="https://example.com/calendar.ics",
            type="event_ics",
            mode="immediate",
            enabled=True,
        )
        s.add(feed)
        s.flush()
        feed_id = feed.id
        s.add(
            FeedBaseline(
                feed_id=feed_id,
                baseline_published_at=datetime.utcnow() - timedelta(days=10),
            )
        )
        for external_id, age in (("old", timedelta(days=2)), ("recent", timedelta(minutes=5))):
            s.add(
                Item(
                    feed_id=feed_id,
                    external_id=external_id,
                    title=external_id,
                    link=f"https://example.com/{external_id}",
                    published_at=datetime.utcnow() - age,
                )
            )

    bot = DummyBot()
    sent = asyncio.run(BotScheduler(bot=bot)._deliver_due_event_starts(feed_id))
    assert sent == 1
    assert len(bot.messages) == 1
    assert "recent" in bot.messages[0][1]


def test_send_video_message_attaches_ai_callback_for_item():
    bot = DummyBot()
    scheduler = BotScheduler(bot=bot)

    status, error = asyncio.run(
        scheduler._send_video_message(
            chat_id=12345,
            title="Video title",
            link="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            feed_name="Test feed",
            item_id=42,
        )
    )

    assert status == "ok"
    assert error is None
    assert len(bot.messages) == 1
    _, _, reply_markup = bot.messages[0]
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    assert len(reply_markup.inline_keyboard) == 2
    assert len(reply_markup.inline_keyboard[0]) == 2
    assert reply_markup.inline_keyboard[0][0].text == "Открыть"
    assert reply_markup.inline_keyboard[0][1].text == "Сделать /ai"
    assert reply_markup.inline_keyboard[0][1].callback_data == "ai:item:42"
    assert len(reply_markup.inline_keyboard[1]) == 1
    assert reply_markup.inline_keyboard[1][0].text == "✓"
    assert reply_markup.inline_keyboard[1][0].callback_data == "msg:viewed"


def test_send_video_message_without_item_id_has_only_open_button():
    bot = DummyBot()
    scheduler = BotScheduler(bot=bot)

    status, error = asyncio.run(
        scheduler._send_video_message(
            chat_id=12345,
            title="Video title",
            link="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            feed_name="Test feed",
            item_id=None,
        )
    )

    assert status == "ok"
    assert error is None
    assert len(bot.messages) == 1
    _, _, reply_markup = bot.messages[0]
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    assert len(reply_markup.inline_keyboard) == 2
    assert len(reply_markup.inline_keyboard[0]) == 1
    assert reply_markup.inline_keyboard[0][0].text == "Открыть"
    assert len(reply_markup.inline_keyboard[1]) == 1
    assert reply_markup.inline_keyboard[1][0].text == "✓"
    assert reply_markup.inline_keyboard[1][0].callback_data == "msg:viewed"


def test_send_video_message_non_youtube_has_only_open_button():
    bot = DummyBot()
    scheduler = BotScheduler(bot=bot)

    status, error = asyncio.run(
        scheduler._send_video_message(
            chat_id=12345,
            title="Article title",
            link="https://example.com/post/123",
            feed_name="Test feed",
            item_id=42,
        )
    )

    assert status == "ok"
    assert error is None
    assert len(bot.messages) == 1
    _, _, reply_markup = bot.messages[0]
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    assert len(reply_markup.inline_keyboard) == 2
    assert len(reply_markup.inline_keyboard[0]) == 1
    assert reply_markup.inline_keyboard[0][0].text == "Открыть"
    assert len(reply_markup.inline_keyboard[1]) == 1
    assert reply_markup.inline_keyboard[1][0].text == "✓"
    assert reply_markup.inline_keyboard[1][0].callback_data == "msg:viewed"
