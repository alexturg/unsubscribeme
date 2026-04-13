from __future__ import annotations

import asyncio
import calendar
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Sequence
from urllib.parse import parse_qs, urlparse

import aiohttp
import feedparser

from .config import Settings
from .youtube_summarize import SummarizationError, summarize_text_with_openai
from .youtube_transcribe import (
    TranscriptError,
    TranscriptSegment,
    fetch_transcript,
    transcript_options_from_settings,
)


class BullshitDetectorError(RuntimeError):
    """Raised when /bullshit pipeline could not produce a reliable result."""


@dataclass(frozen=True)
class BullshitRequest:
    channel_ref: str
    max_videos: int
    top_k: int


@dataclass(frozen=True)
class ChannelVideo:
    video_id: str
    title: str
    link: str
    published_ts: int
    suspicion_score: int
    suspicion_reasons: tuple[str, ...]


@dataclass(frozen=True)
class BullshitVideoSummary:
    video_id: str
    title: str
    link: str
    suspicion_score: int
    suspicion_reasons: tuple[str, ...]
    summary_text: str


@dataclass(frozen=True)
class BullshitSkippedVideo:
    video_id: str
    title: str
    reason: str


@dataclass(frozen=True)
class BullshitAnalysisResult:
    channel_id: str
    scanned_count: int
    shortlisted_count: int
    analyzed_videos: tuple[BullshitVideoSummary, ...]
    skipped_videos: tuple[BullshitSkippedVideo, ...]
    raw_analysis_text: str


DEFAULT_PROMPT_PATH = Path("data/prompts/bullshit_detector_v2.txt")
WHITESPACE_RE = re.compile(r"\s+")

# Scoring is intentionally rough: we only need a shortlist to avoid scanning all videos.
CLICKBAIT_PATTERNS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (
        re.compile(
            r"\b(сенсац|шок|разоблач|обман|манипуляц|катастроф|секрет|пугающ|"
            r"правда,?\s+о|вам\s+не\s+расскажут|никто\s+не\s+говорит)\b",
            flags=re.IGNORECASE,
        ),
        20,
        "Кликбейтная/эмоциональная лексика в заголовке.",
    ),
    (
        re.compile(
            r"\b(revolution|shocking|secret|truth\s+about|nobody\s+tells\s+you|"
            r"exposed|destroy|kills?\s+all|game\s*changer)\b",
            flags=re.IGNORECASE,
        ),
        20,
        "Англоязычная кликбейтная лексика.",
    ),
    (
        re.compile(r"\b(100%|без\s+ошибок|гарантир|навсегда|единственн[а-я]+\s+способ)\b", flags=re.IGNORECASE),
        14,
        "Абсолютные обещания и безапелляционные формулировки.",
    ),
    (
        re.compile(r"\b(убь[её]т\s+вс[её]|заменит\s+вс[её]|конец\s+професси[ий])\b", flags=re.IGNORECASE),
        16,
        "Гиперболы про "
        "радикальные последствия без контекста.",
    ),
)


def parse_bullshit_request_text(
    text: str,
    *,
    default_max_videos: int = 15,
    default_top_k: int = 5,
) -> BullshitRequest:
    raw = (text or "").strip()
    parts = raw.split()
    if not parts or not parts[0].startswith("/bullshit"):
        raise ValueError("bad_command")

    if len(parts) < 2 or not parts[1].strip():
        raise ValueError(
            "Использование: /bullshit <youtube_channel_url_or_channel_id> "
            "[videos=15] [top=5]"
        )

    channel_ref = parts[1].strip()
    max_videos = max(1, int(default_max_videos))
    top_k = max(1, int(default_top_k))

    for token in parts[2:]:
        normalized = token.strip().lower()
        if normalized.startswith("videos="):
            max_videos = _parse_positive_int(token.split("=", 1)[1], name="videos", max_value=40)
            continue
        if normalized.startswith("top="):
            top_k = _parse_positive_int(token.split("=", 1)[1], name="top", max_value=10)
            continue
        raise ValueError(
            f"Неизвестный аргумент '{token}'. Ожидаются только videos=<число> и top=<число>."
        )

    top_k = min(top_k, max_videos)
    return BullshitRequest(channel_ref=channel_ref, max_videos=max_videos, top_k=top_k)


def _parse_positive_int(value: str, *, name: str, max_value: int) -> int:
    try:
        parsed = int((value or "").strip())
    except Exception as exc:
        raise ValueError(f"Параметр {name} должен быть числом.") from exc
    if parsed < 1:
        raise ValueError(f"Параметр {name} должен быть >= 1.")
    if parsed > max_value:
        raise ValueError(f"Параметр {name} слишком большой. Максимум: {max_value}.")
    return parsed


def _clean_text(value: str) -> str:
    return WHITESPACE_RE.sub(" ", (value or "")).strip()


def _extract_video_id_from_entry(entry: feedparser.FeedParserDict) -> str | None:
    external_id = str(entry.get("id") or "")
    if ":video:" in external_id:
        candidate = external_id.split(":video:")[-1].strip()
        if candidate:
            return candidate

    link = str(entry.get("link") or "").strip()
    if not link:
        return None

    parsed = urlparse(link)
    video_from_query = (parse_qs(parsed.query).get("v") or [""])[0].strip()
    if video_from_query:
        return video_from_query

    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.endswith("youtu.be") and path_parts:
        return path_parts[0]
    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
        return path_parts[1]
    return None


def _entry_published_ts(entry: feedparser.FeedParserDict) -> int:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return int(calendar.timegm(parsed))
        except Exception:
            return 0
    return 0


def score_video_suspicion(title: str, description: str = "") -> tuple[int, tuple[str, ...]]:
    normalized_title = _clean_text(title)
    normalized_description = _clean_text(description)
    joined = f"{normalized_title}\n{normalized_description[:600]}"

    score = 0
    reasons: list[str] = []

    for pattern, delta, reason in CLICKBAIT_PATTERNS:
        if pattern.search(joined):
            score += delta
            reasons.append(reason)

    exclamations = normalized_title.count("!")
    if exclamations >= 2:
        score += min(16, 4 * (exclamations - 1))
        reasons.append("Много восклицательных знаков в заголовке.")

    questions = normalized_title.count("?")
    if questions >= 2:
        score += min(12, 3 * (questions - 1))
        reasons.append("Серия вопросов как триггер любопытства.")

    caps_tokens = re.findall(r"\b[A-ZА-ЯЁ]{4,}\b", normalized_title)
    if caps_tokens:
        score += min(16, 5 + (len(caps_tokens) - 1) * 3)
        reasons.append("Агрессивные CAPS-фрагменты в заголовке.")

    if re.search(r"\b\d{2,3}%\b", normalized_title):
        score += 8
        reasons.append("Процентные обещания в заголовке.")

    if re.search(r"\b(прямо\s+сейчас|срочно|немедленно|right\s+now|urgent)\b", normalized_title, flags=re.IGNORECASE):
        score += 10
        reasons.append("Давление на срочность.")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return min(100, score), unique_reasons


def shortlist_suspicious_videos(videos: Sequence[ChannelVideo], top_k: int) -> list[ChannelVideo]:
    if not videos:
        return []

    limit = max(1, min(len(videos), int(top_k)))
    all_zero = all(video.suspicion_score <= 0 for video in videos)

    if all_zero:
        return sorted(videos, key=lambda item: item.published_ts, reverse=True)[:limit]

    ranked = sorted(
        videos,
        key=lambda item: (item.suspicion_score, item.published_ts),
        reverse=True,
    )
    return ranked[:limit]


def _channel_feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


async def fetch_channel_latest_videos(
    channel_id: str,
    *,
    limit: int,
    timeout_sec: int,
) -> list[ChannelVideo]:
    feed_url = _channel_feed_url(channel_id)
    timeout = aiohttp.ClientTimeout(total=max(8, timeout_sec))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(feed_url) as response:
            if response.status >= 400:
                raise BullshitDetectorError(
                    f"YouTube RSS вернул статус {response.status} для канала {channel_id}."
                )
            payload = await response.read()

    parsed = feedparser.parse(payload)
    entries = list(getattr(parsed, "entries", []) or [])
    if not entries:
        raise BullshitDetectorError("В YouTube RSS нет доступных видео для анализа.")

    videos: list[ChannelVideo] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if len(videos) >= limit:
            break

        video_id = _extract_video_id_from_entry(entry)
        if not video_id or video_id in seen_ids:
            continue
        seen_ids.add(video_id)

        title = _clean_text(str(entry.get("title") or "")) or f"Видео {video_id}"
        link = _clean_text(str(entry.get("link") or ""))
        if not link:
            link = f"https://www.youtube.com/watch?v={video_id}"
        description = _clean_text(str(entry.get("summary") or entry.get("description") or ""))
        score, reasons = score_video_suspicion(title, description)
        videos.append(
            ChannelVideo(
                video_id=video_id,
                title=title,
                link=link,
                published_ts=_entry_published_ts(entry),
                suspicion_score=score,
                suspicion_reasons=reasons,
            )
        )

    if not videos:
        raise BullshitDetectorError("Не удалось извлечь видео из YouTube RSS ленты канала.")

    videos.sort(key=lambda item: item.published_ts, reverse=True)
    return videos[:limit]


def _render_transcript_plain_text(segments: list[TranscriptSegment]) -> str:
    lines: list[str] = []
    for segment in segments:
        text = _clean_text(segment.text)
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def _parse_languages(raw_languages: str) -> list[str]:
    result = [item.strip() for item in (raw_languages or "").split(",") if item.strip()]
    return result or ["ru", "en"]


def _read_bullshit_prompt(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise BullshitDetectorError(
            f"Не найден файл промпта: {path}. Сохраните шаблон для /bullshit."
        ) from exc
    except OSError as exc:
        raise BullshitDetectorError(f"Не удалось прочитать файл промпта: {exc}") from exc

    if not content:
        raise BullshitDetectorError(f"Файл промпта пустой: {path}")
    return content


def _bullshit_summary_focus_prompt() -> str:
    return (
        "Сделай технически точную суммаризацию транскрипта для последующей проверки на "
        "манипуляции и кликбейт. Сохраняй конкретику: ключевые тезисы, причинно-следственные "
        "связи, обещания, выводы и любые спорные/безапелляционные утверждения. "
        "Не смягчай формулировки автора и не добавляй факты от себя."
    )


def _llm_output_text(response: object) -> str:
    raw = getattr(response, "output_text", None)
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _analyze_with_openai(
    *,
    model: str,
    api_key: str | None,
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise BullshitDetectorError(
            "Missing dependency 'openai'. Install it with: pip install openai"
        ) from exc

    try:
        client = OpenAI(api_key=api_key) if api_key else OpenAI()
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_output_tokens=max(700, max_output_tokens),
        )
    except Exception as exc:
        message = str(exc).strip() or "Unknown OpenAI API error"
        raise BullshitDetectorError(f"OpenAI analysis failed: {message}") from exc

    output = _llm_output_text(response)
    if not output:
        raise BullshitDetectorError("OpenAI вернул пустой анализ /bullshit.")
    return output


def _format_bullshit_input(videos: Sequence[BullshitVideoSummary]) -> str:
    chunks: list[str] = []
    for video in videos:
        chunks.append("——")
        chunks.append(video.title)
        chunks.append("—")
        chunks.append(video.summary_text)
    chunks.append("——")
    return "\n".join(chunks).strip()


async def run_bullshit_detector(
    settings: Settings,
    *,
    channel_id: str,
    max_videos: int,
    top_k: int,
) -> BullshitAnalysisResult:
    openai_model = str(
        getattr(settings, "AI_BULLSHIT_OPENAI_MODEL", None)
        or getattr(settings, "AI_SUMMARIZER_OPENAI_MODEL", "gpt-4.1-mini")
    )
    fetch_timeout_sec = max(8, int(getattr(settings, "AI_BULLSHIT_FETCH_TIMEOUT_SEC", 20)))
    summary_sentences = max(5, int(getattr(settings, "AI_BULLSHIT_SUMMARY_SENTENCES", 10)))
    summary_input_words = int(getattr(settings, "AI_BULLSHIT_SUMMARY_MAX_INPUT_WORDS", 1600))
    max_output_tokens = int(getattr(settings, "AI_BULLSHIT_OPENAI_MAX_OUTPUT_TOKENS", 2200))
    prompt_path = Path(getattr(settings, "AI_BULLSHIT_PROMPT_PATH", DEFAULT_PROMPT_PATH))
    api_key = getattr(settings, "OPENAI_API_KEY", None)

    prompt_text = _read_bullshit_prompt(prompt_path)
    languages = _parse_languages(getattr(settings, "AI_SUMMARIZER_LANGUAGES", "ru,en"))

    scanned = await fetch_channel_latest_videos(
        channel_id,
        limit=max(1, max_videos),
        timeout_sec=fetch_timeout_sec,
    )
    shortlisted = shortlist_suspicious_videos(scanned, top_k=max(1, top_k))

    analyzed: list[BullshitVideoSummary] = []
    skipped: list[BullshitSkippedVideo] = []

    for video in shortlisted:
        try:
            segments = await asyncio.to_thread(
                fetch_transcript,
                video_id=video.video_id,
                languages=languages,
                **transcript_options_from_settings(settings),
            )
        except TranscriptError as exc:
            skipped.append(
                BullshitSkippedVideo(
                    video_id=video.video_id,
                    title=video.title,
                    reason=f"нет субтитров: {str(exc)[:120]}",
                )
            )
            continue

        transcript_text = _render_transcript_plain_text(segments)
        if not transcript_text:
            skipped.append(
                BullshitSkippedVideo(
                    video_id=video.video_id,
                    title=video.title,
                    reason="пустой текст субтитров",
                )
            )
            continue

        try:
            summary = await asyncio.to_thread(
                summarize_text_with_openai,
                transcript_text,
                max_sentences=summary_sentences,
                model=openai_model,
                custom_prompt=_bullshit_summary_focus_prompt(),
                max_input_words=summary_input_words if summary_input_words > 0 else None,
                api_key=api_key,
                target_language="Russian",
            )
        except SummarizationError as exc:
            skipped.append(
                BullshitSkippedVideo(
                    video_id=video.video_id,
                    title=video.title,
                    reason=f"ошибка суммаризации: {str(exc)[:120]}",
                )
            )
            continue

        analyzed.append(
            BullshitVideoSummary(
                video_id=video.video_id,
                title=video.title,
                link=video.link,
                suspicion_score=video.suspicion_score,
                suspicion_reasons=video.suspicion_reasons,
                summary_text=summary,
            )
        )

    if not analyzed:
        raise BullshitDetectorError(
            "Не удалось подготовить суммаризации для анализа. "
            "Проверьте субтитры/доступность видео и ключ OpenAI."
        )

    dataset = _format_bullshit_input(analyzed)
    user_prompt = (
        f"Канал YouTube: {channel_id}\n"
        f"Проанализируй {len(analyzed)} видео(ролика) в формате ниже:\n\n"
        f"{dataset}"
    )

    final_analysis = await asyncio.to_thread(
        _analyze_with_openai,
        model=openai_model,
        api_key=api_key,
        system_prompt=prompt_text,
        user_prompt=user_prompt,
        max_output_tokens=max_output_tokens,
    )

    return BullshitAnalysisResult(
        channel_id=channel_id,
        scanned_count=len(scanned),
        shortlisted_count=len(shortlisted),
        analyzed_videos=tuple(analyzed),
        skipped_videos=tuple(skipped),
        raw_analysis_text=final_analysis,
    )
