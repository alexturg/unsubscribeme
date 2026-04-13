import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from rssbot import ai_summarizer
from rssbot.ai_summarizer import AiSummarizerError, parse_ai_request_text, split_message_chunks
from rssbot.youtube_context import VideoContext
from rssbot.web_summarize import WebPageContent


def _settings(tmp_path: Path, **overrides):
    values = {
        "OPENAI_API_KEY": "test-key",
        "AI_SUMMARIZER_MODE": "openai",
        "AI_SUMMARIZER_OPENAI_MODEL": "gpt-4.1-mini",
        "AI_SUMMARIZER_LANGUAGES": "ru,en",
        "AI_SUMMARIZER_MAX_SENTENCES": 7,
        "AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS": 0,
        "AI_SUMMARIZER_SAVE_OUTPUT_FILES": False,
        "AI_SUMMARIZER_WEB_OPENAI_MAX_INPUT_WORDS": 1400,
        "AI_SUMMARIZER_WEB_FETCH_TIMEOUT_SEC": 15,
        "AI_SUMMARIZER_WEB_MAX_RESPONSE_BYTES": 2_000_000,
        "AI_SUMMARIZER_WEB_MAX_EXTRACTED_WORDS": 4500,
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
    assert result.summary_path is None
    assert result.transcript_path is None


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
    assert calls["kwargs"]["target_language"] == "Russian"


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


def test_summarize_video_openai_english_prompt_language(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    calls = {}

    monkeypatch.setattr(ai_summarizer, "extract_video_id", lambda _: "dQw4w9WgXcQ")
    monkeypatch.setattr(
        ai_summarizer,
        "fetch_transcript",
        lambda **_: [ai_summarizer.TranscriptSegment(text="Transcript text.", start=0.0, duration=1.0)],
    )

    def fake_openai_summary(text, **kwargs):
        calls["kwargs"] = kwargs
        return "- OpenAI bullet"

    monkeypatch.setattr(ai_summarizer, "summarize_text_with_openai", fake_openai_summary)

    asyncio.run(
        ai_summarizer.summarize_video(
            settings,
            chat_id=2,
            video_url="dQw4w9WgXcQ",
            custom_prompt="what are pros and cons of each model",
        )
    )
    assert calls["kwargs"]["target_language"] == "English"


def test_summarize_video_openai_defaults_to_russian_without_prompt(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    calls = {}

    monkeypatch.setattr(ai_summarizer, "extract_video_id", lambda _: "dQw4w9WgXcQ")
    monkeypatch.setattr(
        ai_summarizer,
        "fetch_transcript",
        lambda **_: [ai_summarizer.TranscriptSegment(text="Transcript text.", start=0.0, duration=1.0)],
    )

    def fake_openai_summary(text, **kwargs):
        calls["kwargs"] = kwargs
        return "- OpenAI bullet"

    monkeypatch.setattr(ai_summarizer, "summarize_text_with_openai", fake_openai_summary)

    asyncio.run(
        ai_summarizer.summarize_video(
            settings,
            chat_id=10,
            video_url="dQw4w9WgXcQ",
            custom_prompt=None,
        )
    )
    assert calls["kwargs"]["target_language"] == "Russian"


def test_summarize_video_web_page_openai(monkeypatch, tmp_path):
    settings = _settings(tmp_path, AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS=0)
    calls = {}

    monkeypatch.setattr(
        ai_summarizer,
        "fetch_webpage_content",
        lambda *args, **kwargs: WebPageContent(
            source_url="https://example.com/page",
            title="Example title",
            cleaned_text="First factual paragraph.\nSecond factual paragraph.",
        ),
    )

    def fake_openai_summary(text, **kwargs):
        calls["text"] = text
        calls["kwargs"] = kwargs
        return "- Web bullet"

    monkeypatch.setattr(ai_summarizer, "summarize_text_with_openai", fake_openai_summary)

    result = asyncio.run(
        ai_summarizer.summarize_video(
            settings,
            chat_id=5,
            video_url="https://example.com/page",
            custom_prompt="сфокусируйся на рисках",
        )
    )

    assert result.summary_text == "- Web bullet"
    assert result.summary_path is None
    assert result.transcript_path is None
    assert calls["text"].startswith("First factual paragraph.")
    assert calls["kwargs"]["max_input_words"] == 1400
    assert calls["kwargs"]["target_language"] == "Russian"


def test_summarize_video_persists_files_only_when_enabled(monkeypatch, tmp_path):
    settings = _settings(tmp_path, AI_SUMMARIZER_MODE="extractive", AI_SUMMARIZER_SAVE_OUTPUT_FILES=True)

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
            chat_id=999,
            video_url="https://youtu.be/dQw4w9WgXcQ",
            custom_prompt=None,
        )
    )

    assert result.summary_path is not None and result.summary_path.exists()
    assert result.transcript_path is not None and result.transcript_path.exists()


def test_summarize_video_fallbacks_to_description_and_comments(monkeypatch, tmp_path):
    settings = _settings(tmp_path, AI_SUMMARIZER_MODE="openai")
    calls = {}

    monkeypatch.setattr(ai_summarizer, "extract_video_id", lambda _: "dQw4w9WgXcQ")

    def fake_fetch_transcript(**_kwargs):
        raise ai_summarizer.TranscriptError("No transcripts were found for this video")

    monkeypatch.setattr(ai_summarizer, "fetch_transcript", fake_fetch_transcript)
    monkeypatch.setattr(
        ai_summarizer,
        "fetch_video_context",
        lambda **_kwargs: VideoContext(
            video_id="dQw4w9WgXcQ",
            title="Video title",
            short_description="Short description text",
            comments=["first comment", "second comment"],
            watch_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
    )

    def fake_openai_summary(text, **kwargs):
        calls["text"] = text
        calls["kwargs"] = kwargs
        return "- Fallback bullet"

    monkeypatch.setattr(ai_summarizer, "summarize_text_with_openai", fake_openai_summary)

    result = asyncio.run(
        ai_summarizer.summarize_video(
            settings,
            chat_id=77,
            video_url="https://youtu.be/dQw4w9WgXcQ",
            custom_prompt="выдели риски",
        )
    )

    assert result.summary_text == "- Fallback bullet"
    assert result.summary_basis == "metadata_comments"
    assert result.video_id == "dQw4w9WgXcQ"
    assert "Short video description:" in calls["text"]
    assert "Top viewer comments:" in calls["text"]
    assert calls["kwargs"]["max_input_words"] == 900


def test_summarize_video_fallbacks_to_context_on_request_blocked(monkeypatch, tmp_path):
    settings = _settings(
        tmp_path,
        AI_SUMMARIZER_MODE="openai",
        AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_URL="https://vakhov.github.io/fresh-proxy-list/http.txt",
        AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_MAX_TRIES=9,
    )
    calls = {}

    monkeypatch.setattr(ai_summarizer, "extract_video_id", lambda _: "dQw4w9WgXcQ")

    def fake_fetch_transcript(**kwargs):
        calls["fetch_kwargs"] = kwargs
        raise ai_summarizer.TranscriptError(
            "RequestBlocked: YouTube is blocking requests from your IP"
        )

    monkeypatch.setattr(ai_summarizer, "fetch_transcript", fake_fetch_transcript)
    monkeypatch.setattr(
        ai_summarizer,
        "fetch_video_context",
        lambda **_kwargs: VideoContext(
            video_id="dQw4w9WgXcQ",
            title="Video title",
            short_description="Short description text",
            comments=["first comment", "second comment"],
            watch_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
    )
    monkeypatch.setattr(ai_summarizer, "summarize_text_with_openai", lambda *_args, **_kwargs: "- Via fallback")

    result = asyncio.run(
        ai_summarizer.summarize_video(
            settings,
            chat_id=21,
            video_url="https://youtu.be/dQw4w9WgXcQ",
            custom_prompt=None,
        )
    )

    assert result.summary_text == "- Via fallback"
    assert result.summary_basis == "metadata_comments"
    assert calls["fetch_kwargs"]["proxy_list_url"].startswith("https://vakhov.github.io")
    assert calls["fetch_kwargs"]["proxy_max_tries"] == 9


def test_summarize_video_fallbacks_to_context_on_proxy_disconnect(monkeypatch, tmp_path):
    settings = _settings(tmp_path, AI_SUMMARIZER_MODE="openai")

    monkeypatch.setattr(ai_summarizer, "extract_video_id", lambda _: "dQw4w9WgXcQ")
    monkeypatch.setattr(
        ai_summarizer,
        "fetch_transcript",
        lambda **_kwargs: (_ for _ in ()).throw(
            ai_summarizer.TranscriptError(
                "Failed to fetch transcript. Details: ('Connection aborted.', "
                "RemoteDisconnected('Remote end closed connection without response'))"
            )
        ),
    )
    monkeypatch.setattr(
        ai_summarizer,
        "fetch_video_context",
        lambda **_kwargs: VideoContext(
            video_id="dQw4w9WgXcQ",
            title="Video title",
            short_description="Short description text",
            comments=["first comment", "second comment"],
            watch_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
    )
    monkeypatch.setattr(ai_summarizer, "summarize_text_with_openai", lambda *_args, **_kwargs: "- Via fallback")

    result = asyncio.run(
        ai_summarizer.summarize_video(
            settings,
            chat_id=22,
            video_url="https://youtu.be/dQw4w9WgXcQ",
            custom_prompt=None,
        )
    )

    assert result.summary_text == "- Via fallback"
    assert result.summary_basis == "metadata_comments"


def test_summarize_video_force_whisper(monkeypatch, tmp_path):
    settings = _settings(tmp_path, AI_SUMMARIZER_MODE="openai")
    calls = {}

    monkeypatch.setattr(ai_summarizer, "extract_video_id", lambda _: "dQw4w9WgXcQ")
    monkeypatch.setattr(
        ai_summarizer,
        "transcribe_video_with_whisper",
        lambda *_args, **_kwargs: "full whisper transcript text",
    )

    def fake_openai_summary(text, **kwargs):
        calls["text"] = text
        calls["kwargs"] = kwargs
        return "- Whisper bullet"

    monkeypatch.setattr(ai_summarizer, "summarize_text_with_openai", fake_openai_summary)
    monkeypatch.setattr(
        ai_summarizer,
        "fetch_transcript",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("captions path should not be used")),
    )

    result = asyncio.run(
        ai_summarizer.summarize_video(
            settings,
            chat_id=88,
            video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            custom_prompt=None,
            force_whisper=True,
        )
    )

    assert result.summary_text == "- Whisper bullet"
    assert result.summary_basis == "whisper"
    assert result.video_id == "dQw4w9WgXcQ"
    assert calls["text"] == "full whisper transcript text"


def test_summarize_video_rejects_force_whisper_for_web_page(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(AiSummarizerError):
        asyncio.run(
            ai_summarizer.summarize_video(
                settings,
                chat_id=9,
                video_url="https://example.com/article",
                custom_prompt=None,
                force_whisper=True,
            )
        )
