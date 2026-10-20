import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from rssbot import bot
from rssbot.db import Feed, User, init_engine, session_scope


COMMANDS = (
    "list", "youtube", "channel", "playlist", "addfeed", "addeventsource",
    "addics", "addevents", "remove", "setmode", "setfilter", "mute", "unmute",
)


@pytest.mark.parametrize("command", COMMANDS)
def test_management_commands_delete_request_after_reply(tmp_path, monkeypatch, command):
    init_engine(tmp_path / "bot.sqlite")
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: 1)
    events = []

    async def answer(*_a, **_k):
        events.append("reply")

    async def delete():
        events.append("delete")

    message = SimpleNamespace(text=f"/{command}", answer=AsyncMock(side_effect=answer), delete=AsyncMock(side_effect=delete))
    asyncio.run(getattr(bot, f"cmd_{command}")(message))
    assert events == ["reply", "delete"]


@pytest.mark.parametrize("command", ["list", "mute", "unmute", "remove", "setmode", "setfilter"])
def test_successful_management_commands_delete_request(tmp_path, monkeypatch, command):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        user_id = user.id
        feed = Feed(user_id=user_id, url="https://example.com/feed", enabled=command != "unmute")
        session.add(feed)
        session.flush()
        feed_id = feed.id
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: user_id)
    monkeypatch.setattr(bot, "DEPS", SimpleNamespace(scheduler=SimpleNamespace(
        unschedule_feed_poll=Mock(), schedule_feed_poll=Mock(),
    )))
    args = {
        "list": "", "mute": f" {feed_id}", "unmute": f" {feed_id}",
        "remove": f" {feed_id}", "setmode": f" {feed_id} immediate",
        "setfilter": f' {feed_id} {{"exclude_shorts": true}}',
    }
    message = SimpleNamespace(text=f"/{command}" + args[command], answer=AsyncMock(), delete=AsyncMock())
    asyncio.run(getattr(bot, f"cmd_{command}")(message))
    message.answer.assert_awaited_once()
    message.delete.assert_awaited_once()


def test_failed_processing_keeps_command(tmp_path, monkeypatch):
    init_engine(tmp_path / "bot.sqlite")
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: 1)
    message = SimpleNamespace(text="/list", answer=AsyncMock(side_effect=RuntimeError("send failed")), delete=AsyncMock())
    with pytest.raises(RuntimeError, match="send failed"):
        asyncio.run(bot.cmd_list(message))
    message.delete.assert_not_awaited()


def test_deletion_failure_does_not_fail_processing(tmp_path, monkeypatch):
    init_engine(tmp_path / "bot.sqlite")
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: 1)
    message = SimpleNamespace(text="/list", answer=AsyncMock(), delete=AsyncMock(side_effect=RuntimeError("cannot delete")))
    asyncio.run(bot.cmd_list(message))
    message.answer.assert_awaited_once()
    message.delete.assert_awaited_once()
