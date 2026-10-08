import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from rssbot import bot
from rssbot.db import CardEntry, CardMessage, User, init_engine, session_scope


def _setup(tmp_path, monkeypatch):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        user_id = user.id
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: user_id)
    monkeypatch.setattr(bot, "_is_allowed", lambda _chat_id: True)
    monkeypatch.setattr(
        bot,
        "fetch_webpage_content",
        lambda *_args, **_kwargs: SimpleNamespace(title="Page <Title> & more"),
    )
    return user_id


def _message(text, *, send_fails=False):
    sent = SimpleNamespace(message_id=200)
    answer = AsyncMock(side_effect=RuntimeError("send failed") if send_fails else None)
    if not send_fails:
        answer.return_value = sent
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=77),
        message_id=100,
        answer=answer,
        delete=AsyncMock(),
    )


def test_plain_youtube_link_becomes_card_and_deletes_original(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    message = _message(" https://youtu.be/dQw4w9WgXcQ ")
    asyncio.run(bot.cmd_plain_link(message))
    message.delete.assert_awaited_once()
    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert '<a href="https://youtu.be/dQw4w9WgXcQ">Page &lt;Title&gt; &amp; more</a>' in text
    assert [button.text for button in markup.inline_keyboard[0]] == ["Сделать /ai"]
    assert [button.text for button in markup.inline_keyboard[1]] == ["✓", "Skip", "Someday"]
    with session_scope() as session:
        card = session.query(CardEntry).one()
        assert card.status is None
        assert card.link == "https://youtu.be/dQw4w9WgXcQ"
        assert card.title == "Page <Title> & more"
        assert session.query(CardMessage).one().message_id == 200
        assert markup.inline_keyboard[0][0].callback_data == f"ai:link:{card.id}"


def test_plain_web_link_has_ai_button_and_linked_title(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    message = _message("https://example.com/article?a=1&b=2")
    asyncio.run(bot.cmd_plain_link(message))
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert [button.text for button in markup.inline_keyboard[0]] == ["Сделать /ai"]
    assert "&amp;" in message.answer.await_args.args[0]
    message.delete.assert_awaited_once()


def test_non_link_or_failed_send_keeps_original(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    for text in ("hello", "Read https://example.com", "https://example.com one more"):
        message = _message(text)
        asyncio.run(bot.cmd_plain_link(message))
        message.answer.assert_not_awaited()
        message.delete.assert_not_awaited()
    failed = _message("https://example.com", send_fails=True)
    asyncio.run(bot.cmd_plain_link(failed))
    failed.delete.assert_not_awaited()


def test_ai_button_uses_saved_link_for_its_owner(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    message = _message("https://example.com/article")
    asyncio.run(bot.cmd_plain_link(message))
    with session_scope() as session:
        card_id = session.query(CardEntry.id).scalar()
    run_ai = AsyncMock()
    monkeypatch.setattr(bot, "_run_ai_summary", run_ai)
    callback = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=77)),
        data=f"ai:link:{card_id}",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    asyncio.run(bot.cb_ai_link(callback))
    assert run_ai.await_args.args[:2] == (77, "https://example.com/article")

    callback.message.chat.id = 88
    asyncio.run(bot.cb_ai_link(callback))
    assert run_ai.await_count == 1


def test_title_lookup_failure_falls_back_to_host(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(bot, "fetch_webpage_content", unavailable)
    message = _message("https://example.com/article")
    asyncio.run(bot.cmd_plain_link(message))
    assert '>example.com</a>' in message.answer.await_args.args[0]
    message.delete.assert_awaited_once()
