import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from rssbot import ai_summarizer
from rssbot.ai_summarizer import AiSummarizerError, parse_ai_request_text, split_message_chunks


def _settings(tmp_path: Path, **overrides):
    values = {
        "OPENAI_API_KEY": "test-key",
        "AI_SUMMARIZER_MODE": "openai",
        "AI_SUMMARIZER_OPENAI_MODEL": "gpt-4.1-mini",
        "AI_SUMMARIZER_LANGUAGES": "ru,en",
        "AI_SUMMARIZER_MAX_SENTENCES": 7,
        "AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS": 0,
        "AI_SUMMARIZER_TIMEOUT_SEC": 60,
        "AI_SUMMARIZER_OUTPUT_DIR": tmp_path / "ai",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parse_ai_request_text_with_focus():
    req = parse_ai_request_text(
        "/ai https://www.youtube.com/watch?v=dQw4w9WgXcQ выдели ключевые решения"
    )
    assert req.video_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert req.custom_prompt == "выдели ключевые решения"


def test_parse_ai_request_text_without_focus():
    req = parse_ai_request_text("/ai dQw4w9WgXcQ")
    assert req.video_url == "dQw4w9WgXcQ"
    assert req.custom_prompt is None


def test_parse_ai_request_text_requires_url():
    with pytest.raises(ValueError):
        parse_ai_request_text("/ai")


def test_split_message_chunks_roundtrip():
    text = "A" * 30 + "\n" + "B" * 30 + "\n" + "C" * 30
    parts = split_message_chunks(text, max_len=35)
    assert len(parts) > 1
    assert "".join(parts) == text


def test_summarize_video_extractive(monkeypatch, tmp_path):
    settings = _settings(tmp_path, AI_SUMMARIZER_MODE="extractive")

    monkeypatch.setattr(ai_summarizer, "extract_video_id", lambda _: "dQw4w9WgXcQ")
    monkeypatch.setattr(
        ai_summarizer,
        "fetch_transcript",
        lambda **_: [
            ai_summarizer.TranscriptSegment(text="First sentence.", start=0.0, duration=1.0),
            ai_summarizer.TranscriptSegment(text="Second sentence.", start=4.0, duration=1.0),
        ],
    )
    monkeypatch.setattr(ai_summarizer, "summarize_text", lambda *_args, **_kwargs: "- Bullet one")

    result = asyncio.run(
        ai_summarizer.summarize_video(
            settings,
            chat_id=123,
            video_url="https://youtu.be/dQw4w9WgXcQ",
            custom_prompt=None,
        )
    )

    assert result.summary_text == "- Bullet one"
    assert result.summary_path.exists()
    assert result.transcript_path is not None and result.transcript_path.exists()


def test_summarize_video_openai_custom_prompt(monkeypatch, tmp_path):
    settings = _settings(tmp_path, AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS=333)
    calls = {}

    monkeypatch.setattr(ai_summarizer, "extract_video_id", lambda _: "dQw4w9WgXcQ")
    monkeypatch.setattr(
        ai_summarizer,
        "fetch_transcript",
        lambda **_: [ai_summarizer.TranscriptSegment(text="Transcript text.", start=0.0, duration=1.0)],
    )

    def fake_openai_summary(text, **kwargs):
        calls["text"] = text
        calls["kwargs"] = kwargs
        return "- OpenAI bullet"

    monkeypatch.setattr(ai_summarizer, "summarize_text_with_openai", fake_openai_summary)

    result = asyncio.run(
        ai_summarizer.summarize_video(
            settings,
            chat_id=1,
            video_url="dQw4w9WgXcQ",
            custom_prompt="только риски",
        )
    )

    assert result.summary_text == "- OpenAI bullet"
    assert calls["kwargs"]["custom_prompt"] == "только риски"
    assert calls["kwargs"]["max_input_words"] == 333
    assert calls["kwargs"]["api_key"] == "test-key"


def test_summarize_video_invalid_mode(tmp_path):
    settings = _settings(tmp_path, AI_SUMMARIZER_MODE="bad_mode")
    with pytest.raises(AiSummarizerError):
        asyncio.run(
            ai_summarizer.summarize_video(
                settings,
                chat_id=1,
                video_url="dQw4w9WgXcQ",
                custom_prompt=None,
            )
        )
