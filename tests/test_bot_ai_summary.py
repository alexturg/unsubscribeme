import asyncio
from types import SimpleNamespace

import rssbot.bot as bot_module
from rssbot.ai_summarizer import AiSummarizerError, AiSummaryResult


class DummyMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class DummyCallback:
    def __init__(self, message) -> None:
        self.message = message
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
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
    assert summary_kb.inline_keyboard[0][0].callback_data == "msg:viewed"
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
    assert summary_kb.inline_keyboard[1][0].callback_data == "msg:viewed"


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
