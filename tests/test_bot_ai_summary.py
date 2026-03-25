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

    asyncio.run(
        bot_module._run_ai_summary(
            chat_id=123,
            video_url=video_url,
            custom_prompt=None,
            send_text=send_text,
        )
    )

    assert len(sent) == 2
    assert sent[0][0].startswith("Запускаю суммаризацию")
    assert sent[0][2].deleted is True
    assert "Суммаризация готова." in sent[1][0]
    assert f"Источник: {video_url}" in sent[1][0]


def test_run_ai_summary_error_mentions_video_url_and_deletes_progress(monkeypatch):
    monkeypatch.setattr(bot_module, "DEPS", SimpleNamespace(settings=SimpleNamespace()))

    async def _fake_summarize_video(*_args, **_kwargs):
        raise AiSummarizerError("No transcript found")

    monkeypatch.setattr(bot_module, "summarize_video", _fake_summarize_video)
    sent, send_text = _make_send_text()
    video_url = "https://www.youtube.com/watch?v=abcdefghijk"

    asyncio.run(
        bot_module._run_ai_summary(
            chat_id=123,
            video_url=video_url,
            custom_prompt=None,
            send_text=send_text,
        )
    )

    assert len(sent) == 2
    assert sent[0][2].deleted is True
    assert "Не удалось сделать суммаризацию." in sent[1][0]
    assert f"Источник: {video_url}" in sent[1][0]
    assert "Ошибка: No transcript found" in sent[1][0]
