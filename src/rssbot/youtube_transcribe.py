from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import parse_qs, urlparse

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
WHITESPACE_RE = re.compile(r"\s+")


class TranscriptError(RuntimeError):
    """Raised when transcript fetch fails."""


class WhisperTranscriptionError(RuntimeError):
    """Raised when Whisper-based transcription fails."""


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


def _normalize_space(text: str) -> str:
    return WHITESPACE_RE.sub(" ", (text or "")).strip()


def _download_audio_for_whisper(
    *,
    watch_url: str,
    work_dir: Path,
    yt_dlp_binary: str,
    timeout_sec: int,
) -> Path:
    output_template = str((work_dir / "audio.%(ext)s").resolve())
    cmd = [
        yt_dlp_binary,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "9",
        "--output",
        output_template,
        watch_url,
    ]

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError as exc:
        raise WhisperTranscriptionError(
            "Whisper fallback недоступен: не найден бинарник yt-dlp на сервере."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WhisperTranscriptionError(
            f"Whisper fallback: таймаут скачивания аудио ({timeout_sec} сек)."
        ) from exc
    except Exception as exc:
        raise WhisperTranscriptionError(f"Whisper fallback: ошибка скачивания аудио: {exc}") from exc

    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        details = _normalize_space(details)[:300]
        raise WhisperTranscriptionError(
            "Whisper fallback: не удалось скачать аудио через yt-dlp."
            + (f" Детали: {details}" if details else "")
        )

    candidates = sorted(work_dir.glob("audio.*"))
    audio_candidates = [path for path in candidates if path.suffix.lower() not in {".part", ".ytdl"}]
    if not audio_candidates:
        raise WhisperTranscriptionError(
            "Whisper fallback: yt-dlp не создал аудиофайл для транскрипции."
        )

    return audio_candidates[0]


def transcribe_video_with_whisper(
    video_url_or_id: str,
    *,
    model: str = "whisper-1",
    api_key: str | None = None,
    max_audio_megabytes: int = 24,
    download_timeout_sec: int = 240,
    yt_dlp_binary: str = "yt-dlp",
) -> str:
    if max_audio_megabytes < 1:
        raise ValueError("max_audio_megabytes must be >= 1")
    if download_timeout_sec < 10:
        raise ValueError("download_timeout_sec must be >= 10")

    video_id = extract_video_id(video_url_or_id)
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise WhisperTranscriptionError(
            "Missing optional dependency 'openai'. Install it with: pip install openai"
        ) from exc

    max_audio_bytes = max_audio_megabytes * 1024 * 1024

    with tempfile.TemporaryDirectory(prefix="yt_whisper_") as tmp_dir:
        work_dir = Path(tmp_dir)
        audio_path = _download_audio_for_whisper(
            watch_url=watch_url,
            work_dir=work_dir,
            yt_dlp_binary=yt_dlp_binary,
            timeout_sec=download_timeout_sec,
        )

        try:
            size_bytes = audio_path.stat().st_size
        except OSError as exc:
            raise WhisperTranscriptionError(
                f"Whisper fallback: не удалось прочитать размер аудиофайла ({exc})."
            ) from exc
        if size_bytes > max_audio_bytes:
            size_mb = size_bytes / (1024 * 1024)
            raise WhisperTranscriptionError(
                f"Whisper fallback: аудио слишком большое ({size_mb:.1f} MB), лимит {max_audio_megabytes} MB."
            )

        try:
            client = OpenAI(api_key=api_key) if api_key else OpenAI()
            with audio_path.open("rb") as audio_file:
                response = client.audio.transcriptions.create(
                    model=model,
                    file=audio_file,
                    response_format="text",
                )
        except Exception as exc:
            message = _normalize_space(str(exc)) or "Unknown Whisper API error"
            raise WhisperTranscriptionError(f"Whisper transcription failed: {message}") from exc

    if isinstance(response, str):
        transcript = response
    else:
        transcript = getattr(response, "text", "")
        if not isinstance(transcript, str):
            transcript = ""

    transcript = _normalize_space(transcript)
    if not transcript:
        raise WhisperTranscriptionError("Whisper transcription returned empty text.")
    return transcript
