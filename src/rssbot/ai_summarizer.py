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
from .youtube_summarize import SummarizationError, summarize_text, summarize_text_with_openai
from .youtube_transcribe import TranscriptError, TranscriptSegment, extract_video_id, fetch_transcript


class AiSummarizerError(RuntimeError):
    """Raised when internal AI summarizer could not return a usable summary."""


@dataclass(frozen=True)
class AiSummaryRequest:
    video_url: str
    custom_prompt: Optional[str]


@dataclass(frozen=True)
class AiSummaryResult:
    summary_text: str
    summary_path: Path
    transcript_path: Optional[Path]


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


def _infer_instruction_language(custom_prompt: Optional[str]) -> Optional[str]:
    text = (custom_prompt or "").strip().lower()
    if not text:
        return None
    cyr = sum(1 for ch in text if ("а" <= ch <= "я") or ch == "ё")
    lat = sum(1 for ch in text if "a" <= ch <= "z")
    if cyr == 0 and lat == 0:
        return None
    return "Russian" if cyr >= lat else "English"


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _summarize_sync(
    settings: Settings,
    *,
    video_url: str,
    custom_prompt: Optional[str],
) -> tuple[str, list[str], list[TranscriptSegment], str]:
    mode = (settings.AI_SUMMARIZER_MODE or "openai").strip().lower()
    if mode not in {"openai", "extractive"}:
        raise AiSummarizerError("AI_SUMMARIZER_MODE must be 'openai' or 'extractive'.")

    languages = _parse_languages(settings.AI_SUMMARIZER_LANGUAGES)
    if not languages:
        raise AiSummarizerError("AI_SUMMARIZER_LANGUAGES must include at least one language code.")

    max_sentences = max(1, int(settings.AI_SUMMARIZER_MAX_SENTENCES))
    max_input_words = max(0, int(settings.AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS))
    target_language = _infer_instruction_language(custom_prompt)

    video_id = extract_video_id(video_url)
    segments = fetch_transcript(video_id=video_id, languages=languages)
    plain_transcript = " ".join(segment.text for segment in segments)

    if mode == "openai":
        summary = summarize_text_with_openai(
            plain_transcript,
            max_sentences=max_sentences,
            model=settings.AI_SUMMARIZER_OPENAI_MODEL,
            custom_prompt=custom_prompt,
            max_input_words=max_input_words or None,
            api_key=getattr(settings, "OPENAI_API_KEY", None),
            target_language=target_language,
        )
    else:
        summary = summarize_text(plain_transcript, max_sentences=max_sentences)

    return video_id, languages, segments, summary


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
    mode = (settings.AI_SUMMARIZER_MODE or "openai").strip().lower()
    if mode not in {"openai", "extractive"}:
        raise AiSummarizerError("AI_SUMMARIZER_MODE must be 'openai' or 'extractive'.")

    max_sentences = max(1, int(settings.AI_SUMMARIZER_MAX_SENTENCES))
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
        summary = summarize_text_with_openai(
            source_text,
            max_sentences=max_sentences,
            model=settings.AI_SUMMARIZER_OPENAI_MODEL,
            custom_prompt=custom_prompt,
            max_input_words=_web_openai_input_word_budget(settings),
            api_key=getattr(settings, "OPENAI_API_KEY", None),
            target_language=target_language,
        )
    else:
        summary = summarize_text(source_text, max_sentences=max_sentences)

    return page.source_url, page.title, source_text, summary


async def summarize_video(
    settings: Settings,
    *,
    chat_id: int,
    video_url: str,
    custom_prompt: Optional[str],
) -> AiSummaryResult:
    """Summarize YouTube video or arbitrary webpage URL."""
    output_dir = settings.AI_SUMMARIZER_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    prefix = f"tg_{chat_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    timeout = max(10, int(settings.AI_SUMMARIZER_TIMEOUT_SEC))
    is_youtube_source = _looks_like_youtube_source(video_url)

    try:
        if is_youtube_source:
            video_id, languages, segments, summary = await asyncio.wait_for(
                asyncio.to_thread(
                    _summarize_sync,
                    settings,
                    video_url=video_url,
                    custom_prompt=custom_prompt,
                ),
                timeout=timeout,
            )
        else:
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
    except WebSummarizationError as exc:
        raise AiSummarizerError(str(exc)) from exc
    except SummarizationError as exc:
        raise AiSummarizerError(str(exc)) from exc

    summary_path = output_dir / f"{prefix}_summary.txt"
    source_text_path = output_dir / (
        f"{prefix}_{'transcript' if is_youtube_source else 'source'}.txt"
    )
    json_path = output_dir / f"{prefix}_result.json"

    result_obj: dict[str, object]
    source_text_for_file: str
    if is_youtube_source:
        source_text_for_file = "\n".join(
            f"[{_format_timestamp(segment.start)}] {segment.text}" for segment in segments
        )
        result_obj = {
            "source_type": "youtube",
            "video_id": video_id,
            "source_url": video_url,
            "languages": languages,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "segments": [segment.to_dict() for segment in segments],
            "summary": summary,
            "output_files": {
                "transcript": str(source_text_path.resolve()),
                "summary": str(summary_path.resolve()),
            },
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

    if not summary.strip():
        raise AiSummarizerError("Суммаризатор вернул пустой текст summary.")

    return AiSummaryResult(
        summary_text=summary,
        summary_path=summary_path,
        transcript_path=source_text_path,
    )


# Backward-compatible alias used by earlier integration code.
summarize_video_with_cli = summarize_video
