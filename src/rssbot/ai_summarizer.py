from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Settings
from .web_summarize import WebSummarizationError, fetch_webpage_content
from .youtube_context import VideoContext, VideoContextError, fetch_video_context
from .youtube_summarize import SummarizationError, summarize_text, summarize_text_with_openai
from .youtube_transcribe import (
    TranscriptError,
    TranscriptSegment,
    WhisperTranscriptionError,
    extract_video_id,
    fetch_transcript,
    transcript_options_from_settings,
    transcribe_video_with_whisper,
)


class AiSummarizerError(RuntimeError):
    """Raised when internal AI summarizer could not return a usable summary."""


@dataclass(frozen=True)
class AiSummaryRequest:
    video_url: str
    custom_prompt: Optional[str]


@dataclass(frozen=True)
class AiSummaryResult:
    summary_text: str
    summary_path: Optional[Path]
    transcript_path: Optional[Path]
    source_type: str = "youtube"
    summary_basis: str = "captions"
    video_id: Optional[str] = None


@dataclass(frozen=True)
class _YouTubeSummarySyncResult:
    video_id: str
    languages: list[str]
    segments: list[TranscriptSegment]
    source_text: str
    summary: str
    summary_basis: str


TRANSCRIPT_MISSING_MARKERS = (
    "no transcripts were found",
    "transcriptsdisabled",
    "transcript is disabled",
    "subtitles are disabled",
    "requested transcript is not available",
    "no transcript",
    "requestblocked",
    "ipblocked",
    "youtube is blocking requests from your ip",
    "connection aborted",
    "remotedisconnected",
    "proxyerror",
    "unable to connect to proxy",
    "max retries exceeded with url",
    "tunnel connection failed",
    "httpsconnectionpool",
    "connection reset by peer",
    "read timed out",
    "connect timeout",
    "timed out",
)
YOUTUBE_CONTEXT_SUMMARY_INSTRUCTION = (
    "Important: source text contains only video short description and viewer comments, "
    "not a full transcript. Be explicit when information is uncertain."
)


def parse_ai_request_text(text: str) -> AiSummaryRequest:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty")

    # Supports both "/ai ..." and "/ai@BotName ..." command forms.
    parts = raw.split(maxsplit=1)
    if not parts or not parts[0].startswith("/ai"):
        raise ValueError("bad_command")

    if len(parts) < 2 or not parts[1].strip():
        raise ValueError("missing_args")

    payload = parts[1].strip()
    url_parts = payload.split(maxsplit=1)
    video_url = url_parts[0].strip()
    custom_prompt = url_parts[1].strip() if len(url_parts) > 1 else None
    if custom_prompt == "":
        custom_prompt = None
    return AiSummaryRequest(video_url=video_url, custom_prompt=custom_prompt)


def split_message_chunks(text: str, max_len: int = 3800) -> list[str]:
    if max_len < 1:
        raise ValueError("max_len must be >= 1")
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_len:
            if current:
                chunks.append(current)
                current = ""
            for idx in range(0, len(line), max_len):
                chunks.append(line[idx : idx + max_len])
            continue

        if current and len(current) + len(line) > max_len:
            chunks.append(current)
            current = line
        else:
            current += line

    if current:
        chunks.append(current)
    return chunks


def _parse_languages(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _infer_instruction_language(custom_prompt: Optional[str]) -> str:
    text = (custom_prompt or "").strip().lower()
    if not text:
        return "Russian"
    cyr = sum(1 for ch in text if ("а" <= ch <= "я") or ch == "ё")
    lat = sum(1 for ch in text if "a" <= ch <= "z")
    if cyr == 0 and lat == 0:
        return "Russian"
    return "Russian" if cyr >= lat else "English"


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _require_supported_mode(settings: Settings) -> str:
    mode = (settings.AI_SUMMARIZER_MODE or "openai").strip().lower()
    if mode not in {"openai", "extractive"}:
        raise AiSummarizerError("AI_SUMMARIZER_MODE must be 'openai' or 'extractive'.")
    return mode


def _summarize_source_text_by_mode(
    settings: Settings,
    *,
    source_text: str,
    custom_prompt: Optional[str],
    target_language: str,
    max_input_words_override: Optional[int] = None,
) -> str:
    mode = _require_supported_mode(settings)
    max_sentences = max(1, int(settings.AI_SUMMARIZER_MAX_SENTENCES))
    if mode == "openai":
        max_input_words = max(0, int(settings.AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS))
        if max_input_words_override is not None:
            max_input_words = max(220, int(max_input_words_override))
        return summarize_text_with_openai(
            source_text,
            max_sentences=max_sentences,
            model=settings.AI_SUMMARIZER_OPENAI_MODEL,
            custom_prompt=custom_prompt,
            max_input_words=max_input_words or None,
            api_key=getattr(settings, "OPENAI_API_KEY", None),
            target_language=target_language,
        )
    return summarize_text(source_text, max_sentences=max_sentences)


def _transcript_error_means_missing_subtitles(exc: TranscriptError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in TRANSCRIPT_MISSING_MARKERS)


def _youtube_context_openai_input_word_budget(settings: Settings) -> int:
    context_budget = max(
        260,
        int(getattr(settings, "AI_SUMMARIZER_YOUTUBE_CONTEXT_OPENAI_MAX_INPUT_WORDS", 900)),
    )
    common_budget = max(0, int(settings.AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS))
    if common_budget:
        return min(common_budget, context_budget)
    return context_budget


def _build_youtube_context_source(context: VideoContext) -> str:
    lines: list[str] = []
    if context.title:
        lines.append(f"Video title: {context.title}")
    if context.short_description:
        lines.append("Short video description:")
        lines.append(context.short_description)
    if context.comments:
        lines.append("Top viewer comments:")
        lines.extend(f"- {comment}" for comment in context.comments)
    return "\n".join(lines).strip()


def _summarize_youtube_context_fallback(
    settings: Settings,
    *,
    video_id: str,
    custom_prompt: Optional[str],
    target_language: str,
) -> _YouTubeSummarySyncResult:
    context = fetch_video_context(
        video_id=video_id,
        timeout_sec=max(3, int(getattr(settings, "AI_SUMMARIZER_YOUTUBE_CONTEXT_FETCH_TIMEOUT_SEC", 15))),
        max_html_bytes=max(
            250_000,
            int(getattr(settings, "AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_HTML_BYTES", 2_500_000)),
        ),
        max_description_words=max(
            80,
            int(getattr(settings, "AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_DESCRIPTION_WORDS", 220)),
        ),
        max_comments=max(0, int(getattr(settings, "AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_COMMENTS", 12))),
        max_comment_words=max(
            6,
            int(getattr(settings, "AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_COMMENT_WORDS", 36)),
        ),
    )
    context_text = _build_youtube_context_source(context)
    if not context_text:
        raise AiSummarizerError("Fallback-контент YouTube пустой: нет описания и комментариев.")

    mode = _require_supported_mode(settings)
    fallback_prompt = (
        YOUTUBE_CONTEXT_SUMMARY_INSTRUCTION
        if not custom_prompt
        else f"{custom_prompt}\n\n{YOUTUBE_CONTEXT_SUMMARY_INSTRUCTION}"
    )
    if mode == "openai":
        summary = _summarize_source_text_by_mode(
            settings,
            source_text=context_text,
            custom_prompt=fallback_prompt,
            target_language=target_language,
            max_input_words_override=_youtube_context_openai_input_word_budget(settings),
        )
    else:
        summary = _summarize_source_text_by_mode(
            settings,
            source_text=context_text,
            custom_prompt=None,
            target_language=target_language,
        )

    return _YouTubeSummarySyncResult(
        video_id=video_id,
        languages=[],
        segments=[],
        source_text=context_text,
        summary=summary,
        summary_basis="metadata_comments",
    )


def _summarize_sync(
    settings: Settings,
    *,
    video_url: str,
    custom_prompt: Optional[str],
) -> _YouTubeSummarySyncResult:
    _require_supported_mode(settings)
    languages = _parse_languages(settings.AI_SUMMARIZER_LANGUAGES)
    if not languages:
        raise AiSummarizerError("AI_SUMMARIZER_LANGUAGES must include at least one language code.")

    target_language = _infer_instruction_language(custom_prompt)
    video_id = extract_video_id(video_url)

    try:
        segments = fetch_transcript(
            video_id=video_id,
            languages=languages,
            **transcript_options_from_settings(settings),
        )
    except TranscriptError as exc:
        if not _transcript_error_means_missing_subtitles(exc):
            raise
        return _summarize_youtube_context_fallback(
            settings,
            video_id=video_id,
            custom_prompt=custom_prompt,
            target_language=target_language,
        )

    plain_transcript = " ".join(segment.text for segment in segments)
    summary = _summarize_source_text_by_mode(
        settings,
        source_text=plain_transcript,
        custom_prompt=custom_prompt,
        target_language=target_language,
    )
    return _YouTubeSummarySyncResult(
        video_id=video_id,
        languages=languages,
        segments=segments,
        source_text=plain_transcript,
        summary=summary,
        summary_basis="captions",
    )


def _summarize_sync_with_whisper(
    settings: Settings,
    *,
    video_url: str,
    custom_prompt: Optional[str],
) -> _YouTubeSummarySyncResult:
    _require_supported_mode(settings)
    target_language = _infer_instruction_language(custom_prompt)
    video_id = extract_video_id(video_url)

    transcript_text = transcribe_video_with_whisper(
        video_id,
        model=str(getattr(settings, "AI_SUMMARIZER_WHISPER_MODEL", "whisper-1")),
        api_key=getattr(settings, "OPENAI_API_KEY", None),
        max_audio_megabytes=max(
            8,
            int(getattr(settings, "AI_SUMMARIZER_WHISPER_MAX_AUDIO_MB", 24)),
        ),
        download_timeout_sec=max(
            20,
            int(getattr(settings, "AI_SUMMARIZER_WHISPER_DOWNLOAD_TIMEOUT_SEC", 240)),
        ),
        yt_dlp_binary=str(getattr(settings, "AI_SUMMARIZER_WHISPER_YTDLP_BINARY", "yt-dlp")),
    )
    summary = _summarize_source_text_by_mode(
        settings,
        source_text=transcript_text,
        custom_prompt=custom_prompt,
        target_language=target_language,
    )
    return _YouTubeSummarySyncResult(
        video_id=video_id,
        languages=[],
        segments=[],
        source_text=transcript_text,
        summary=summary,
        summary_basis="whisper",
    )


def _looks_like_youtube_source(source: str) -> bool:
    try:
        extract_video_id(source)
        return True
    except ValueError:
        return False


def _web_openai_input_word_budget(settings: Settings) -> int:
    common_budget = max(0, int(settings.AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS))
    if common_budget:
        return common_budget
    web_budget = max(220, int(getattr(settings, "AI_SUMMARIZER_WEB_OPENAI_MAX_INPUT_WORDS", 1400)))
    return web_budget


def _summarize_web_sync(
    settings: Settings,
    *,
    page_url: str,
    custom_prompt: Optional[str],
) -> tuple[str, str, str, str]:
    mode = _require_supported_mode(settings)
    target_language = _infer_instruction_language(custom_prompt)
    fetch_timeout = max(3, int(getattr(settings, "AI_SUMMARIZER_WEB_FETCH_TIMEOUT_SEC", 15)))
    fetch_max_bytes = max(
        200_000,
        int(getattr(settings, "AI_SUMMARIZER_WEB_MAX_RESPONSE_BYTES", 2_000_000)),
    )
    fetch_max_words = max(
        320,
        int(getattr(settings, "AI_SUMMARIZER_WEB_MAX_EXTRACTED_WORDS", 4500)),
    )

    page = fetch_webpage_content(
        page_url,
        timeout_sec=fetch_timeout,
        max_bytes=fetch_max_bytes,
        max_words=fetch_max_words,
    )
    source_text = page.cleaned_text

    if mode == "openai":
        summary = _summarize_source_text_by_mode(
            settings,
            source_text=source_text,
            custom_prompt=custom_prompt,
            target_language=target_language,
            max_input_words_override=_web_openai_input_word_budget(settings),
        )
    else:
        summary = _summarize_source_text_by_mode(
            settings,
            source_text=source_text,
            custom_prompt=None,
            target_language=target_language,
        )

    return page.source_url, page.title, source_text, summary


async def summarize_video(
    settings: Settings,
    *,
    chat_id: int,
    video_url: str,
    custom_prompt: Optional[str],
    force_whisper: bool = False,
) -> AiSummaryResult:
    """Summarize YouTube video or arbitrary webpage URL."""
    timeout = max(10, int(settings.AI_SUMMARIZER_TIMEOUT_SEC))
    is_youtube_source = _looks_like_youtube_source(video_url)

    youtube_result: Optional[_YouTubeSummarySyncResult] = None
    source_url = ""
    source_title = ""
    source_text = ""
    summary = ""
    summary_basis = "web_page"
    video_id: Optional[str] = None

    try:
        if is_youtube_source:
            sync_fn = _summarize_sync_with_whisper if force_whisper else _summarize_sync
            youtube_result = await asyncio.wait_for(
                asyncio.to_thread(
                    sync_fn,
                    settings,
                    video_url=video_url,
                    custom_prompt=custom_prompt,
                ),
                timeout=timeout,
            )
            summary = youtube_result.summary
            summary_basis = youtube_result.summary_basis
            video_id = youtube_result.video_id
        else:
            if force_whisper:
                raise AiSummarizerError("Whisper-доступен только для YouTube-ссылок.")
            source_url, source_title, source_text, summary = await asyncio.wait_for(
                asyncio.to_thread(
                    _summarize_web_sync,
                    settings,
                    page_url=video_url,
                    custom_prompt=custom_prompt,
                ),
                timeout=timeout,
            )
    except asyncio.TimeoutError as exc:
        raise AiSummarizerError(f"Таймаут суммаризации ({timeout} сек).") from exc
    except ValueError as exc:
        raise AiSummarizerError(str(exc)) from exc
    except TranscriptError as exc:
        raise AiSummarizerError(str(exc)) from exc
    except VideoContextError as exc:
        raise AiSummarizerError(str(exc)) from exc
    except WhisperTranscriptionError as exc:
        raise AiSummarizerError(str(exc)) from exc
    except WebSummarizationError as exc:
        raise AiSummarizerError(str(exc)) from exc
    except SummarizationError as exc:
        raise AiSummarizerError(str(exc)) from exc

    if not summary.strip():
        raise AiSummarizerError("Суммаризатор вернул пустой текст summary.")

    persist_outputs = bool(getattr(settings, "AI_SUMMARIZER_SAVE_OUTPUT_FILES", False))
    summary_path: Optional[Path] = None
    source_text_path: Optional[Path] = None
    if persist_outputs:
        output_dir = settings.AI_SUMMARIZER_OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"tg_{chat_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"

        summary_path = output_dir / f"{prefix}_summary.txt"
        source_suffix = (
            "transcript"
            if is_youtube_source and summary_basis in {"captions", "whisper"}
            else "source"
        )
        source_text_path = output_dir / f"{prefix}_{source_suffix}.txt"
        json_path = output_dir / f"{prefix}_result.json"

        result_obj: dict[str, object]
        source_text_for_file: str
        if is_youtube_source and youtube_result is not None:
            if youtube_result.summary_basis == "captions" and youtube_result.segments:
                source_text_for_file = "\n".join(
                    f"[{_format_timestamp(segment.start)}] {segment.text}"
                    for segment in youtube_result.segments
                )
            else:
                source_text_for_file = youtube_result.source_text

            output_files: dict[str, str] = {
                "summary": str(summary_path.resolve()),
            }
            if source_suffix == "transcript":
                output_files["transcript"] = str(source_text_path.resolve())
            else:
                output_files["source_text"] = str(source_text_path.resolve())

            result_obj = {
                "source_type": "youtube",
                "video_id": youtube_result.video_id,
                "source_url": video_url,
                "summary_basis": youtube_result.summary_basis,
                "languages": youtube_result.languages,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "segments": [segment.to_dict() for segment in youtube_result.segments],
                "summary": summary,
                "output_files": output_files,
            }
        else:
            source_text_for_file = source_text
            result_obj = {
                "source_type": "web_page",
                "source_url": source_url,
                "source_title": source_title,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "summary": summary,
                "output_files": {
                    "source_text": str(source_text_path.resolve()),
                    "summary": str(summary_path.resolve()),
                },
            }

        try:
            source_text_path.write_text(source_text_for_file + "\n", encoding="utf-8")
            summary_path.write_text(summary + "\n", encoding="utf-8")
            json_path.write_text(
                json.dumps(result_obj, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise AiSummarizerError(f"Не удалось сохранить результат суммаризации: {exc}") from exc

    return AiSummaryResult(
        summary_text=summary,
        summary_path=summary_path,
        transcript_path=source_text_path,
        source_type="youtube" if is_youtube_source else "web_page",
        summary_basis=summary_basis,
        video_id=video_id,
    )


# Backward-compatible alias used by earlier integration code.
summarize_video_with_cli = summarize_video
