import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rssbot import bot
from rssbot.db import Feed, FeedRule, User, init_engine, session_scope


def test_migrates_existing_filter_table_and_preserves_rules(tmp_path):
    path = tmp_path / "old.sqlite"
    # Reproduce the pre-upgrade schema, including an existing rule.
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE feed_rules (
            id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL UNIQUE,
            include_keywords JSON, exclude_keywords JSON, include_regex JSON,
            exclude_regex JSON, require_all BOOLEAN NOT NULL,
            case_sensitive BOOLEAN NOT NULL, categories JSON,
            min_duration_sec INTEGER, max_duration_sec INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
        )""")
        connection.execute("""INSERT INTO feed_rules
            (feed_id, exclude_keywords, require_all, case_sensitive)
            VALUES (1, '["keep"]', 0, 0)""")
    init_engine(path)
    init_engine(path)
    with session_scope() as session:
        rules = session.query(FeedRule).one()
        assert rules.exclude_shorts is False
        assert rules.exclude_keywords == ["keep"]


@pytest.mark.parametrize("target", ["all", "single"])
def test_setfilter_shorts_preserves_filters_and_user_boundary(tmp_path, monkeypatch, target):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        session.add_all([User(id=1, chat_id=123), User(id=2, chat_id=456)])
        session.flush()
        for fid, uid in [(1, 1), (2, 1), (3, 2)]:
            session.add(Feed(id=fid, user_id=uid, url=f"source:{fid}"))
        session.add(FeedRule(feed_id=1, exclude_keywords=["keep"]))
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _: 1)
    target = "1" if target == "single" else "all"
    message = SimpleNamespace(
        text=f'/setfilter {target} {{"exclude_shorts":true}}', answer=AsyncMock()
    )
    asyncio.run(bot.cmd_setfilter(message))
    with session_scope() as session:
        assert session.get(Feed, 1).rules.exclude_shorts is True
        assert session.get(Feed, 1).rules.exclude_keywords == ["keep"]
        assert bool(session.get(Feed, 2).rules) == (target == "all")
        assert session.get(Feed, 3).rules is None
    message.text = f'/setfilter {target} {{"exclude_shorts":false}}'
    asyncio.run(bot.cmd_setfilter(message))
    with session_scope() as session:
        assert session.get(Feed, 1).rules.exclude_shorts is False


@pytest.mark.parametrize("payload", ['[]', '{"exclude_shorts":"false"}'])
def test_setfilter_rejects_invalid_shorts_setting(tmp_path, monkeypatch, payload):
    init_engine(tmp_path / "bot.sqlite")
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _: 1)
    message = SimpleNamespace(text=f"/setfilter all {payload}", answer=AsyncMock())
    asyncio.run(bot.cmd_setfilter(message))
    assert message.answer.await_args.args[0] != "Фильтры обновлены."
