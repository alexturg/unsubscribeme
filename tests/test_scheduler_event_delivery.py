import asyncio
from datetime import datetime, timedelta

from rssbot.db import Delivery, Feed, FeedBaseline, Item, User, init_engine, session_scope
from rssbot.scheduler import BotScheduler


class DummyBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        self.messages.append((chat_id, text))
        return {"ok": True}


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
