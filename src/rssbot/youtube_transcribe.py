from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import re
import subprocess
import tempfile
from urllib.parse import parse_qs, urlparse

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
WHITESPACE_RE = re.compile(r"\s+")
OPENAI_TRANSCRIPTION_HARD_LIMIT_BYTES = 25 * 1024 * 1024
# Multipart/form-data wrapper adds overhead beyond raw audio file bytes.
OPENAI_TRANSCRIPTION_UPLOAD_OVERHEAD_BYTES = 1_200_000


class TranscriptError(RuntimeError):
    """Raised when transcript fetch fails."""


class WhisperTranscriptionError(RuntimeError):
    """Raised when Whisper-based transcription fails."""


class YouTubeMediaError(RuntimeError):
    """Raised when YouTube media metadata/audio download fails."""


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start: float
    duration: float

    def to_dict(self) -> dict[str, float | str]:
        return asdict(self)


@dataclass(frozen=True)
class YouTubeVideoInfo:
    video_id: str
    title: str
    duration_seconds: int | None
    filesize_bytes: int | None
    filesize_approx_bytes: int | None


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


def _as_int(value: object) -> int | None:
    try:
        result = int(value)  # type: ignore[arg-type]
    except Exception:
        return None
    return result if result >= 0 else None


def fetch_video_info(
    video_url_or_id: str,
    *,
    yt_dlp_binary: str = "yt-dlp",
    timeout_sec: int = 90,
) -> YouTubeVideoInfo:
    video_id = extract_video_id(video_url_or_id)
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        yt_dlp_binary,
        "--skip-download",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--dump-single-json",
        watch_url,
    ]

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(10, timeout_sec),
        )
    except FileNotFoundError as exc:
        raise YouTubeMediaError(f"yt-dlp binary not found: {yt_dlp_binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise YouTubeMediaError(f"yt-dlp metadata timeout ({timeout_sec} sec)") from exc
    except Exception as exc:
        raise YouTubeMediaError(f"yt-dlp metadata error: {exc}") from exc

    if proc.returncode != 0:
        details = _normalize_space(proc.stderr or proc.stdout or "")[:300]
        raise YouTubeMediaError(
            "yt-dlp could not load video metadata."
            + (f" Details: {details}" if details else "")
        )

    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        raise YouTubeMediaError(f"yt-dlp metadata parse error: {exc}") from exc

    if not isinstance(payload, dict):
        raise YouTubeMediaError("yt-dlp metadata payload is invalid.")

    title = str(payload.get("title") or "").strip()
    duration_seconds = _as_int(payload.get("duration"))
    filesize_bytes = _as_int(payload.get("filesize"))
    filesize_approx_bytes = _as_int(payload.get("filesize_approx"))

    return YouTubeVideoInfo(
        video_id=video_id,
        title=title,
        duration_seconds=duration_seconds,
        filesize_bytes=filesize_bytes,
        filesize_approx_bytes=filesize_approx_bytes,
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


def _run_subprocess_checked(
    cmd: list[str],
    *,
    timeout_sec: int,
    fail_message: str,
) -> None:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError as exc:
        binary = cmd[0] if cmd else "command"
        raise WhisperTranscriptionError(f"{fail_message}: не найден бинарник {binary}.") from exc
    except subprocess.TimeoutExpired as exc:
        raise WhisperTranscriptionError(
            f"{fail_message}: таймаут выполнения команды ({timeout_sec} сек)."
        ) from exc
    except Exception as exc:
        raise WhisperTranscriptionError(f"{fail_message}: {exc}") from exc

    if proc.returncode != 0:
        details = _normalize_space(proc.stderr or proc.stdout or "")[:300]
        raise WhisperTranscriptionError(
            f"{fail_message}: команда завершилась с ошибкой."
            + (f" Детали: {details}" if details else "")
        )


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

    _run_subprocess_checked(
        cmd,
        timeout_sec=timeout_sec,
        fail_message="Whisper fallback: не удалось скачать аудио через yt-dlp",
    )

    candidates = sorted(work_dir.glob("audio.*"))
    audio_candidates = [path for path in candidates if path.suffix.lower() not in {".part", ".ytdl"}]
    if not audio_candidates:
        raise WhisperTranscriptionError(
            "Whisper fallback: yt-dlp не создал аудиофайл для транскрипции."
        )

    return audio_candidates[0]


def download_audio_for_export(
    video_url_or_id: str,
    *,
    output_dir: Path,
    yt_dlp_binary: str = "yt-dlp",
    timeout_sec: int = 240,
    audio_format: str = "mp3",
    audio_quality: str = "5",
) -> Path:
    video_id = extract_video_id(video_url_or_id)
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = str((output_dir / f"{video_id}.%(ext)s").resolve())
    cmd = [
        yt_dlp_binary,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--extract-audio",
        "--audio-format",
        audio_format,
        "--audio-quality",
        str(audio_quality),
        "--output",
        output_template,
        watch_url,
    ]
    try:
        _run_subprocess_checked(
            cmd,
            timeout_sec=max(10, timeout_sec),
            fail_message="Audio export failed: yt-dlp download error",
        )
    except WhisperTranscriptionError as exc:
        raise YouTubeMediaError(str(exc)) from exc

    candidates = sorted(output_dir.glob(f"{video_id}.*"))
    audio_candidates = [
        path
        for path in candidates
        if path.suffix.lower() not in {".part", ".ytdl"} and path.is_file()
    ]
    if not audio_candidates:
        raise YouTubeMediaError("Audio export failed: yt-dlp did not produce an audio file.")
    return audio_candidates[0]


def _transcode_audio_for_whisper(
    *,
    source_audio: Path,
    output_audio: Path,
    timeout_sec: int,
) -> Path:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_audio),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "24k",
        str(output_audio),
    ]
    _run_subprocess_checked(
        cmd,
        timeout_sec=timeout_sec,
        fail_message="Whisper fallback: не удалось подготовить аудио через ffmpeg",
    )
    if not output_audio.exists():
        raise WhisperTranscriptionError(
            "Whisper fallback: ffmpeg не создал файл нормализованного аудио."
        )
    return output_audio


def _split_audio_for_whisper(
    *,
    source_audio: Path,
    work_dir: Path,
    segment_seconds: int,
    timeout_sec: int,
) -> list[Path]:
    suffix = source_audio.suffix or ".mp3"
    for stale in work_dir.glob(f"{source_audio.stem}_part_*{suffix}"):
        try:
            stale.unlink()
        except OSError:
            pass

    pattern = work_dir / f"{source_audio.stem}_part_%03d{suffix}"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_audio),
        "-f",
        "segment",
        "-segment_time",
        str(max(30, segment_seconds)),
        "-c",
        "copy",
        str(pattern),
    ]
    _run_subprocess_checked(
        cmd,
        timeout_sec=timeout_sec,
        fail_message="Whisper fallback: не удалось разбить аудио на части",
    )

    parts = sorted(work_dir.glob(f"{source_audio.stem}_part_*{suffix}"))
    if len(parts) < 2:
        raise WhisperTranscriptionError(
            "Whisper fallback: аудио не удалось разбить на части безопасного размера."
        )
    return parts


def _extract_transcription_text(response: object) -> str:
    if isinstance(response, str):
        return _normalize_space(response)
    text = getattr(response, "text", "")
    if isinstance(text, str):
        return _normalize_space(text)
    return ""


def _is_payload_too_large_error(exc: Exception) -> bool:
    message = _normalize_space(str(exc)).lower()
    return "413" in message and "maximum content size limit" in message


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
    safe_upload_bytes = min(
        max_audio_bytes,
        OPENAI_TRANSCRIPTION_HARD_LIMIT_BYTES - OPENAI_TRANSCRIPTION_UPLOAD_OVERHEAD_BYTES,
    )
    if safe_upload_bytes < 1_000_000:
        raise ValueError("max_audio_megabytes is too low for Whisper upload.")

    with tempfile.TemporaryDirectory(prefix="yt_whisper_") as tmp_dir:
        work_dir = Path(tmp_dir)
        downloaded_audio = _download_audio_for_whisper(
            watch_url=watch_url,
            work_dir=work_dir,
            yt_dlp_binary=yt_dlp_binary,
            timeout_sec=download_timeout_sec,
        )

        normalized_audio = _transcode_audio_for_whisper(
            source_audio=downloaded_audio,
            output_audio=work_dir / "audio_for_whisper.mp3",
            timeout_sec=download_timeout_sec,
        )
        try:
            normalized_size = normalized_audio.stat().st_size
        except OSError as exc:
            raise WhisperTranscriptionError(
                f"Whisper fallback: не удалось прочитать размер нормализованного аудио ({exc})."
            ) from exc

        audio_parts: list[Path] = [normalized_audio]
        if normalized_size > safe_upload_bytes:
            audio_parts = _split_audio_for_whisper(
                source_audio=normalized_audio,
                work_dir=work_dir,
                segment_seconds=900,
                timeout_sec=download_timeout_sec,
            )
            oversize_parts = [part for part in audio_parts if part.stat().st_size > safe_upload_bytes]
            if oversize_parts:
                audio_parts = _split_audio_for_whisper(
                    source_audio=normalized_audio,
                    work_dir=work_dir,
                    segment_seconds=300,
                    timeout_sec=download_timeout_sec,
                )

        oversize_after_split = [part for part in audio_parts if part.stat().st_size > safe_upload_bytes]
        if oversize_after_split:
            largest_part = max(oversize_after_split, key=lambda path: path.stat().st_size)
            size_mb = largest_part.stat().st_size / (1024 * 1024)
            raise WhisperTranscriptionError(
                "Whisper fallback: даже после разбиения аудио часть остаётся слишком большой "
                f"({size_mb:.1f} MB). Попробуйте более короткое видео."
            )

        transcript_chunks: list[str] = []
        try:
            client = OpenAI(api_key=api_key) if api_key else OpenAI()
            for audio_part in audio_parts:
                with audio_part.open("rb") as audio_file:
                    try:
                        response = client.audio.transcriptions.create(
                            model=model,
                            file=audio_file,
                            response_format="text",
                        )
                    except Exception as exc:
                        if _is_payload_too_large_error(exc):
                            raise WhisperTranscriptionError(
                                "Whisper fallback: часть аудио превысила лимит OpenAI. "
                                "Попробуйте уменьшить длительность видео."
                            ) from exc
                        raise
                text_part = _extract_transcription_text(response)
                if text_part:
                    transcript_chunks.append(text_part)
        except Exception as exc:
            if isinstance(exc, WhisperTranscriptionError):
                raise
            message = _normalize_space(str(exc)) or "Unknown Whisper API error"
            raise WhisperTranscriptionError(f"Whisper transcription failed: {message}") from exc

    transcript = _normalize_space("\n".join(transcript_chunks))
    if not transcript:
        raise WhisperTranscriptionError("Whisper transcription returned empty text.")
    return transcript
