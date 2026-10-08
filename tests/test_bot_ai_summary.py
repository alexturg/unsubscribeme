import asyncio
from types import SimpleNamespace
import pytest

import rssbot.bot as bot_module
from rssbot.ai_summarizer import AiSummarizerError, AiSummaryResult
from rssbot.db import CardEntry, User, init_engine, session_scope


@pytest.fixture(autouse=True)
def card_db(tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        for chat_id in (123, 111, 222, 999):
            session.add(User(chat_id=chat_id))


class DummyMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.deleted = False
        self.chat = SimpleNamespace(id=123)
        self.message_id = 101
        self.reply_markup = None
        self.edits = []

    async def delete(self) -> None:
        self.deleted = True

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.reply_markup = kwargs.get("reply_markup")
        self.edits.append((text, kwargs))
        return self


class DummyCallback:
    def __init__(self, message) -> None:
        self.message = message
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def _make_send_text():
    sent: list[tuple[str, object, DummyMessage]] = []

    async def _send_text(text: str, reply_markup=None):
        message = DummyMessage(text)
        sent.append((text, reply_markup, message))
        return message

    return sent, _send_text


def test_run_ai_summary_deletes_progress_message_on_success(monkeypatch):
    monkeypatch.setattr(bot_module, "DEPS", SimpleNamespace(settings=SimpleNamespace()))

    async def _fake_summarize_video(*_args, **_kwargs):
        return AiSummaryResult(
            summary_text="- Summary ready",
            summary_path=None,
            transcript_path=None,
            source_type="youtube",
            summary_basis="captions",
            video_id="dQw4w9WgXcQ",
        )

    monkeypatch.setattr(bot_module, "summarize_video", _fake_summarize_video)
    sent, send_text = _make_send_text()
    video_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    source_message = DummyMessage("/ai request")

    asyncio.run(
        bot_module._run_ai_summary(
            chat_id=123,
            video_url=video_url,
            custom_prompt=None,
            send_text=send_text,
            source_request_message=source_message,
        )
    )

    assert len(sent) == 2
    assert sent[0][0].startswith("Запускаю суммаризацию")
    assert sent[0][2].deleted is True
    assert "Суммаризация готова." in sent[1][0]
    assert f"Источник: {video_url}" in sent[1][0]
    summary_kb = sent[1][1]
    assert summary_kb is not None
    assert summary_kb.inline_keyboard[0][0].text == "✓"
    assert summary_kb.inline_keyboard[0][0].callback_data.startswith("card:done:")
    assert [button.text for button in summary_kb.inline_keyboard[0]] == ["✓", "Skip", "Someday"]
    assert source_message.deleted is True


def test_run_ai_summary_error_mentions_video_url_and_deletes_progress(monkeypatch):
    monkeypatch.setattr(bot_module, "DEPS", SimpleNamespace(settings=SimpleNamespace()))

    async def _fake_summarize_video(*_args, **_kwargs):
        raise AiSummarizerError("No transcript found")

    monkeypatch.setattr(bot_module, "summarize_video", _fake_summarize_video)
    sent, send_text = _make_send_text()
    video_url = "https://www.youtube.com/watch?v=abcdefghijk"
    source_message = DummyMessage("/ai request")

    asyncio.run(
        bot_module._run_ai_summary(
            chat_id=123,
            video_url=video_url,
            custom_prompt=None,
            send_text=send_text,
            source_request_message=source_message,
        )
    )

    assert len(sent) == 2
    assert sent[0][2].deleted is True
    assert "Не удалось сделать суммаризацию." in sent[1][0]
    assert f"Источник: {video_url}" in sent[1][0]
    assert "Ошибка: No transcript found" in sent[1][0]
    assert source_message.deleted is False


def test_run_ai_summary_parallel_requests_delete_only_own_source_messages(monkeypatch):
    monkeypatch.setattr(bot_module, "DEPS", SimpleNamespace(settings=SimpleNamespace()))

    async def _fake_summarize_video(*_args, **kwargs):
        video_url = kwargs.get("video_url", "")
        await asyncio.sleep(0.02 if video_url.endswith("one") else 0.01)
        return AiSummaryResult(
            summary_text=f"- Summary for {video_url}",
            summary_path=None,
            transcript_path=None,
            source_type="youtube",
            summary_basis="captions",
            video_id="dQw4w9WgXcQ",
        )

    monkeypatch.setattr(bot_module, "summarize_video", _fake_summarize_video)
    sent_one, send_text_one = _make_send_text()
    sent_two, send_text_two = _make_send_text()
    source_one = DummyMessage("/ai one")
    source_two = DummyMessage("/ai two")

    async def _run() -> None:
        await asyncio.gather(
            bot_module._run_ai_summary(
                chat_id=111,
                video_url="https://example.com/one",
                custom_prompt=None,
                send_text=send_text_one,
                source_request_message=source_one,
            ),
            bot_module._run_ai_summary(
                chat_id=222,
                video_url="https://example.com/two",
                custom_prompt=None,
                send_text=send_text_two,
                source_request_message=source_two,
            ),
        )

    asyncio.run(_run())

    assert len(sent_one) == 2
    assert len(sent_two) == 2
    assert source_one.deleted is True
    assert source_two.deleted is True


def test_run_ai_summary_metadata_comments_keeps_whisper_and_seen_buttons(monkeypatch):
    monkeypatch.setattr(bot_module, "DEPS", SimpleNamespace(settings=SimpleNamespace()))

    async def _fake_summarize_video(*_args, **_kwargs):
        return AiSummaryResult(
            summary_text="- Preliminary summary",
            summary_path=None,
            transcript_path=None,
            source_type="youtube",
            summary_basis="metadata_comments",
            video_id="dQw4w9WgXcQ",
        )

    monkeypatch.setattr(bot_module, "summarize_video", _fake_summarize_video)
    sent, send_text = _make_send_text()
    source_message = DummyMessage("/ai metadata")

    asyncio.run(
        bot_module._run_ai_summary(
            chat_id=999,
            video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            custom_prompt=None,
            send_text=send_text,
            source_request_message=source_message,
        )
    )

    assert len(sent) == 2
    summary_kb = sent[1][1]
    assert summary_kb is not None
    assert summary_kb.inline_keyboard[0][0].text == "Сделать транскрипцию через Whisper"
    assert summary_kb.inline_keyboard[0][0].callback_data == "ai:whisper:dQw4w9WgXcQ"
    assert summary_kb.inline_keyboard[1][0].text == "✓"
    assert summary_kb.inline_keyboard[1][0].callback_data.startswith("card:done:")


def test_cb_mark_seen_deletes_message(monkeypatch):
    monkeypatch.setattr(bot_module, "_is_allowed", lambda _chat_id: True)
    callback_message = DummyMessage("hello")
    callback_message.chat = SimpleNamespace(id=123)
    callback = DummyCallback(callback_message)

    asyncio.run(bot_module.cb_mark_seen(callback))

    assert callback_message.deleted is True
    assert callback.answers == [("Удалено.", False)]


def test_cb_mark_seen_reports_failure(monkeypatch):
    monkeypatch.setattr(bot_module, "_is_allowed", lambda _chat_id: True)

    class FailingMessage(DummyMessage):
        async def delete(self) -> None:
            raise RuntimeError("cannot delete")

    callback_message = FailingMessage("hello")
    callback_message.chat = SimpleNamespace(id=123)
    callback = DummyCallback(callback_message)

    asyncio.run(bot_module.cb_mark_seen(callback))

    assert callback.answers == [("Не удалось удалить сообщение.", True)]


def test_looks_like_missing_subtitles_error_accepts_proxy_disconnect():
    exc = bot_module.TranscriptError(
        "Failed to fetch transcript. Details: ('Connection aborted.', "
        "RemoteDisconnected('Remote end closed connection without response'))"
    )
    assert bot_module._looks_like_missing_subtitles_error(exc) is True


@pytest.mark.parametrize("basis", ["captions", "metadata_comments", "whisper"])
def test_ai_button_edits_original_card_without_new_messages(monkeypatch, basis):
    monkeypatch.setattr(bot_module, "DEPS", SimpleNamespace(settings=SimpleNamespace()))
    url = "https://example.com/post"
    card_id = bot_module.ensure_link_card(123, 100, url, "Original <title>")
    message = DummyMessage("Original card")
    sent, send_text = _make_send_text()

    async def summarize(*_args, **_kwargs):
        assert len(message.edits) == 1
        assert "Запускаю" in message.text
        assert message.reply_markup is None
        return AiSummaryResult("Summary <result>", None, None, "youtube", basis, "dQw4w9WgXcQ")

    monkeypatch.setattr(bot_module, "summarize_video", summarize)
    asyncio.run(bot_module._run_ai_summary(
        123, url, None, send_text, card_message=message, existing_card_id=card_id,
    ))
    assert sent == []
    assert not message.deleted
    assert len(message.edits) == 2
    assert "Original &lt;title&gt;" in message.text
    assert "Summary &lt;result&gt;" in message.text
    buttons = [button for row in message.reply_markup.inline_keyboard for button in row]
    assert [b.text for b in buttons[-3:]] == ["✓", "Skip", "Someday"]
    assert buttons[-3].callback_data == f"card:done:{card_id}"
    assert any((b.callback_data or "").startswith("ai:whisper:") for b in buttons) == (basis == "metadata_comments")
    with session_scope() as session:
        assert session.query(CardEntry).count() == 1
        assert "Summary <result>" in session.get(CardEntry, card_id).body


def test_ai_card_error_keeps_card_and_retry_button(monkeypatch):
    monkeypatch.setattr(bot_module, "DEPS", SimpleNamespace(settings=SimpleNamespace()))
    url = "https://example.com/post"
    card_id = bot_module.ensure_link_card(123, 100, url, "Post")
    message = DummyMessage("Original card")
    sent, send_text = _make_send_text()

    async def summarize(*_args, **_kwargs):
        raise AiSummarizerError("HTTP 429 <blocked>")

    monkeypatch.setattr(bot_module, "summarize_video", summarize)
    asyncio.run(bot_module._run_ai_summary(
        123, url, None, send_text, card_message=message, existing_card_id=card_id,
        retry_callback=f"ai:link:{card_id}",
    ))
    assert sent == []
    assert len(message.edits) == 2
    assert not message.deleted
    assert "HTTP 429 &lt;blocked&gt;" in message.text
    assert message.reply_markup.inline_keyboard[0][0].callback_data == f"ai:link:{card_id}"
    with session_scope() as session:
        assert session.get(CardEntry, card_id).body is None


def test_long_summary_pages_edit_same_card_and_check_owner(monkeypatch):
    monkeypatch.setattr(bot_module, "DEPS", SimpleNamespace(settings=SimpleNamespace()))
    monkeypatch.setattr(bot_module, "_is_allowed", lambda _: True)
    url = "https://example.com/post"
    card_id = bot_module.ensure_link_card(123, 100, url, "Post")
    message = DummyMessage("Original card")
    sent, send_text = _make_send_text()

    async def summarize(*_args, **_kwargs):
        return AiSummaryResult("First.\n" + "😀" * 2500 + "\nLast.", None, None, "web_page", "web_page", None)

    monkeypatch.setattr(bot_module, "summarize_video", summarize)
    asyncio.run(bot_module._run_ai_summary(
        123, url, None, send_text, card_message=message, existing_card_id=card_id,
    ))
    assert not sent
    next_button = message.reply_markup.inline_keyboard[0][0]
    assert next_button.callback_data.startswith(f"ai:page:{card_id}:")
    callback = DummyCallback(message)
    callback.data = next_button.callback_data
    asyncio.run(bot_module.cb_ai_page(callback))
    assert len(message.edits) == 3
    for text, _kwargs in message.edits:
        assert len(text.encode("utf-16-le")) // 2 < 4096
    with session_scope() as session:
        assert session.get(CardEntry, card_id).body.endswith("Last.")
    message.chat.id = 222
    asyncio.run(bot_module.cb_ai_page(callback))
    assert len(message.edits) == 3
    assert callback.answers[-1] == ("Запись недоступна.", True)


def test_double_ai_click_runs_summary_once(monkeypatch):
    monkeypatch.setattr(bot_module, "DEPS", SimpleNamespace(settings=SimpleNamespace()))
    url = "https://example.com/post"
    card_id = bot_module.ensure_link_card(123, 100, url, "Post")
    message = DummyMessage("Original card")
    sent, send_text = _make_send_text()
    calls = []

    async def summarize(*_args, **_kwargs):
        calls.append(True)
        await asyncio.sleep(0.01)
        return AiSummaryResult("Summary", None, None, "web_page", "web_page", None)

    monkeypatch.setattr(bot_module, "summarize_video", summarize)
    async def run():
        await asyncio.gather(*[
            bot_module._run_ai_summary(123, url, None, send_text, card_message=message, existing_card_id=card_id)
            for _ in range(2)
        ])
    asyncio.run(run())
    assert len(calls) == 1
    assert len(message.edits) == 2
    assert not sent
