from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from urllib.parse import parse_qs, urlparse

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


class TranscriptError(RuntimeError):
    """Raised when transcript fetch fails."""


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start: float
    duration: float

    def to_dict(self) -> dict[str, float | str]:
        return asdict(self)


def extract_video_id(url_or_id: str) -> str:
    candidate = url_or_id.strip()
    if VIDEO_ID_PATTERN.match(candidate):
        return candidate

    if "://" not in candidate and ("youtube.com" in candidate or "youtu.be" in candidate):
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if host.endswith("youtu.be") and path_parts:
        maybe_id = path_parts[0]
        if VIDEO_ID_PATTERN.match(maybe_id):
            return maybe_id

    if "youtube.com" in host or host.endswith("youtube-nocookie.com"):
        video_param = parse_qs(parsed.query).get("v", [""])[0]
        if VIDEO_ID_PATTERN.match(video_param):
            return video_param

        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            maybe_id = path_parts[1]
            if VIDEO_ID_PATTERN.match(maybe_id):
                return maybe_id

    raise ValueError(
        "Could not parse YouTube video ID. Provide a full URL like "
        "https://www.youtube.com/watch?v=... or a raw 11-char video ID."
    )


def _normalize_segments(raw_segments: object) -> list[TranscriptSegment]:
    if hasattr(raw_segments, "snippets"):
        iterable = list(getattr(raw_segments, "snippets"))
    else:
        iterable = list(raw_segments)  # type: ignore[arg-type]

    result: list[TranscriptSegment] = []
    for item in iterable:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            start = float(item.get("start", 0.0))
            duration = float(item.get("duration", 0.0))
        else:
            text = str(getattr(item, "text", "")).strip()
            start = float(getattr(item, "start", 0.0))
            duration = float(getattr(item, "duration", 0.0))

        if text:
            result.append(TranscriptSegment(text=text, start=start, duration=duration))

    return result


def fetch_transcript(video_id: str, languages: list[str]) -> list[TranscriptSegment]:
    normalized_languages = [lang.strip() for lang in languages if lang.strip()]
    if not normalized_languages:
        normalized_languages = ["en"]

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise TranscriptError(
            "Missing dependency 'youtube-transcript-api'. Install requirements first."
        ) from exc

    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            raw_segments = YouTubeTranscriptApi.get_transcript(  # type: ignore[attr-defined]
                video_id,
                languages=normalized_languages,
            )
        else:
            api = YouTubeTranscriptApi()
            raw_segments = api.fetch(video_id, languages=normalized_languages)
    except Exception as exc:  # pragma: no cover - external API errors are runtime-dependent
        message = str(exc).strip() or "Unknown transcript fetch error"
        raise TranscriptError(
            f"Failed to fetch transcript for video '{video_id}'. Details: {message}"
        ) from exc

    segments = _normalize_segments(raw_segments)
    if not segments:
        raise TranscriptError(
            "Transcript was fetched but returned empty content for the selected languages."
        )

    return segments
