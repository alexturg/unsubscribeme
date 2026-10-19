import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text

from rssbot import bot, scheduler as scheduler_mod, web as web_mod
from rssbot.db import Delivery, Feed, Item, User, init_engine, session_scope
from rssbot.scheduler import BotScheduler


@pytest.fixture
def feeds(tmp_path):
    path = tmp_path / 'bot.sqlite'
    init_engine(path)
    with session_scope() as session:
        session.add(User(id=1, chat_id=123, tz='Asia/Almaty'))
        session.flush()
        session.add(Feed(id=1, user_id=1, url='source:1', mode='digest',
                         digest_time_local='01:00', digest_weekday=4,
                         created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    return path


def test_weekly_backlog_waits_for_local_weekday_and_sends_once(feeds, monkeypatch):
    with session_scope() as session:
        session.add(Item(feed_id=1, external_id='announcement', title='Анонс',
                         published_at=datetime(2026, 10, 20, tzinfo=timezone.utc)))
    scheduler = BotScheduler(bot=SimpleNamespace())
    send = AsyncMock(return_value=('ok', None))
    monkeypatch.setattr(scheduler, '_send_video_message', send)
    monkeypatch.setattr(scheduler_mod, 'Settings', lambda: SimpleNamespace(HIDE_FUTURE_VIDEOS=False))

    class Clock(datetime):
        instant = datetime(2026, 10, 22, 1, tzinfo=ZoneInfo('Asia/Almaty'))

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(scheduler_mod, 'datetime', Clock)
    asyncio.run(scheduler._digest_scan_tick())
    send.assert_not_awaited()
    Clock.instant = datetime(2026, 10, 23, 0, 59, tzinfo=ZoneInfo('Asia/Almaty'))
    asyncio.run(scheduler._digest_scan_tick())
    send.assert_not_awaited()
    Clock.instant = datetime(2026, 10, 23, 1, tzinfo=ZoneInfo('Asia/Almaty'))
    asyncio.run(scheduler._digest_scan_tick())
    asyncio.run(scheduler._digest_scan_tick())
    send.assert_awaited_once()
    with session_scope() as session:
        assert session.query(Delivery).one().status == 'ok'


def test_daily_feed_has_no_weekday_restriction(feeds, monkeypatch):
    with session_scope() as session:
        session.get(Feed, 1).digest_weekday = None
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 22, 1, tzinfo=ZoneInfo('Asia/Almaty')).astimezone(tz)
    monkeypatch.setattr(scheduler_mod, 'datetime', Clock)
    scheduler = BotScheduler(bot=SimpleNamespace())
    send = AsyncMock()
    monkeypatch.setattr(scheduler, '_send_digest_for_feed', send)
    asyncio.run(scheduler._digest_scan_tick())
    send.assert_awaited_once_with(1)


@pytest.mark.parametrize('day', ['пт', 'fri', 'ПТ'])
def test_command_sets_preserves_and_resets_day(feeds, monkeypatch, day):
    monkeypatch.setattr(bot, '_ensure_user_id', lambda _: 1)
    monkeypatch.setattr(bot, 'DEPS', SimpleNamespace(
        settings=SimpleNamespace(DIGEST_DEFAULT_TIME='20:00'),
        scheduler=SimpleNamespace(schedule_feed_poll=Mock())))
    message = SimpleNamespace(text=f'/setmode 1 digest 19:00 day={day}', answer=AsyncMock())
    asyncio.run(bot.cmd_setmode(message))
    with session_scope() as session:
        feed = session.get(Feed, 1)
        assert (feed.digest_weekday, feed.digest_time_local) == (4, '19:00')
        assert 'пт' in bot._format_feed_list_line(feed)
    message.text = '/setmode 1 digest 18:00'
    asyncio.run(bot.cmd_setmode(message))
    with session_scope() as session:
        assert session.get(Feed, 1).digest_weekday == 4
    message.text = '/setmode 1 digest day=daily'
    asyncio.run(bot.cmd_setmode(message))
    with session_scope() as session:
        assert session.get(Feed, 1).digest_weekday is None
        assert session.get(Feed, 1).digest_time_local == '18:00'


@pytest.mark.parametrize('args', ['digest day=bad', 'digest day=', 'immediate day=пт',
                                 'digest day=пт day=вт'])
def test_invalid_schedule_does_not_mutate_feed(feeds, monkeypatch, args):
    monkeypatch.setattr(bot, '_ensure_user_id', lambda _: 1)
    message = SimpleNamespace(text=f'/setmode 1 {args}', answer=AsyncMock())
    asyncio.run(bot.cmd_setmode(message))
    with session_scope() as session:
        assert session.get(Feed, 1).digest_weekday == 4
        assert session.get(Feed, 1).mode == 'digest'


def test_migrates_existing_feeds_preserving_schedule(feeds):
    # Simulate an old database without the new nullable column.
    import sqlite3
    with sqlite3.connect(feeds) as connection:
        connection.execute('ALTER TABLE feeds DROP COLUMN digest_weekday')
    engine = init_engine(feeds)
    init_engine(feeds)
    with session_scope() as session:
        feed = session.get(Feed, 1)
        assert feed.digest_weekday is None
        assert feed.digest_time_local == '01:00'
    with engine.connect() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM feeds')).scalar() == 1


def test_web_update_sets_day_and_preserves_it_for_older_forms(feeds, monkeypatch):
    monkeypatch.setattr(web_mod, 'DEPS', SimpleNamespace(
        settings=SimpleNamespace(DIGEST_DEFAULT_TIME='20:00'),
        scheduler=SimpleNamespace(schedule_feed_poll=Mock())))
    form = {'mode': 'digest', 'time': '19:00', 'day': 'вс'}
    request = SimpleNamespace(match_info={'chat_id': '123', 'feed_id': '1'},
                              post=AsyncMock(return_value=form))
    for expected_day in [6, 6, None]:
        with pytest.raises(web_mod.web.HTTPFound):
            asyncio.run(web_mod.update_feed(request))
        with session_scope() as session:
            assert session.get(Feed, 1).digest_weekday == expected_day
        if 'day' in form:
            del form['day']
        else:
            form['day'] = 'daily'
