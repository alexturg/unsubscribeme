from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import logging
import re
import subprocess
import tempfile
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse
from typing import Iterable, Optional

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
WHITESPACE_RE = re.compile(r"\s+")
OPENAI_TRANSCRIPTION_HARD_LIMIT_BYTES = 25 * 1024 * 1024
# Multipart/form-data wrapper adds overhead beyond raw audio file bytes.
OPENAI_TRANSCRIPTION_UPLOAD_OVERHEAD_BYTES = 1_200_000
TRANSCRIPT_MISSING_MARKERS = (
    "no transcripts were found",
    "transcriptsdisabled",
    "transcript is disabled",
    "subtitles are disabled",
    "requested transcript is not available",
    "no transcript",
)
PROXY_LIST_MAX_BYTES = 1_200_000

logger = logging.getLogger(__name__)


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


def _message_has_any_marker(message: str, markers: tuple[str, ...]) -> bool:
    lowered = (message or "").strip().lower()
    return any(marker in lowered for marker in markers)


def _normalize_proxy_url(raw_proxy: str) -> Optional[str]:
    candidate = (raw_proxy or "").strip()
    if not candidate or candidate.startswith("#"):
        return None

    # Keep first token in case line has "ip:port extra-data".
    candidate = candidate.split()[0].strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5", "socks5h"}:
        return None
    if not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc}"


def _parse_proxy_urls(proxy_urls: str | Iterable[str] | None) -> list[str]:
    if proxy_urls is None:
        return []

    raw_items: list[str] = []
    if isinstance(proxy_urls, str):
        raw_items.extend(proxy_urls.replace(",", "\n").splitlines())
    else:
        for item in proxy_urls:
            raw_items.append(str(item))

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        parsed = _normalize_proxy_url(raw_item)
        if not parsed or parsed in seen:
            continue
        seen.add(parsed)
        normalized.append(parsed)
    return normalized


def _fetch_proxy_list_from_url(*, proxy_list_url: str, timeout_sec: int) -> list[str]:
    url = (proxy_list_url or "").strip()
    if not url:
        return []

    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return []

    req = urllib_request.Request(
        url,
        headers={"User-Agent": "unsubscribeme/1.0 (+https://github.com/vakhov/fresh-proxy-list)"},
    )
    try:
        with urllib_request.urlopen(req, timeout=max(2, timeout_sec)) as response:
            payload = response.read(PROXY_LIST_MAX_BYTES)
    except (TimeoutError, urllib_error.URLError, OSError):
        return []

    text = payload.decode("utf-8", errors="replace")
    return _parse_proxy_urls(text)


def _build_proxy_candidates(
    *,
    proxy_urls: str | Iterable[str] | None,
    proxy_list_url: str | None,
    proxy_list_timeout_sec: int,
    proxy_max_tries: int,
) -> list[str]:
    limit = max(0, int(proxy_max_tries))
    if limit == 0:
        return []

    combined: list[str] = []
    seen: set[str] = set()

    for proxy in _parse_proxy_urls(proxy_urls):
        if proxy in seen:
            continue
        seen.add(proxy)
        combined.append(proxy)
        if len(combined) >= limit:
            return combined

    for proxy in _fetch_proxy_list_from_url(
        proxy_list_url=proxy_list_url or "",
        timeout_sec=proxy_list_timeout_sec,
    ):
        if proxy in seen:
            continue
        seen.add(proxy)
        combined.append(proxy)
        if len(combined) >= limit:
            break
    return combined


def _build_timeout_http_client(request_timeout_sec: int) -> object | None:
    try:
        import requests
    except Exception:
        return None

    timeout = max(1, int(request_timeout_sec))

    class _TimeoutSession(requests.Session):
        def __init__(self, default_timeout: int) -> None:
            super().__init__()
            self._default_timeout = default_timeout

        def request(self, method, url, **kwargs):
            kwargs.setdefault("timeout", self._default_timeout)
            return super().request(method, url, **kwargs)

    return _TimeoutSession(timeout)


def _call_with_languages_and_optional_proxies(
    call_target: object,
    *,
    video_id: str,
    languages: list[str],
    proxy_url: Optional[str],
) -> object:
    kwargs: dict[str, object] = {"languages": languages}
    if proxy_url:
        kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        try:
            return call_target(video_id, **kwargs)  # type: ignore[misc]
        except TypeError:
            kwargs.pop("proxies", None)
    return call_target(video_id, **kwargs)  # type: ignore[misc]


def _instantiate_api_client(api_class: object, candidates: list[dict[str, object]]) -> object:
    seen: set[tuple[str, ...]] = set()
    for kwargs in candidates:
        signature = tuple(sorted(kwargs.keys()))
        if signature in seen:
            continue
        seen.add(signature)
        try:
            return api_class(**kwargs)  # type: ignore[misc]
        except TypeError:
            continue
    return api_class()  # type: ignore[misc]


def _create_youtube_transcript_api_client(
    api_class: object,
    proxy_url: Optional[str],
    *,
    request_timeout_sec: int,
) -> object:
    http_client = _build_timeout_http_client(request_timeout_sec)
    proxy_kwargs = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    try:
        from youtube_transcript_api.proxies import GenericProxyConfig
    except Exception:
        GenericProxyConfig = None  # type: ignore[assignment]

    proxy_config = None
    if proxy_url and GenericProxyConfig is not None:
        try:
            proxy_config = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
        except Exception:
            proxy_config = None

    init_candidates: list[dict[str, object]] = []
    if proxy_config is not None and http_client is not None:
        init_candidates.append({"proxy_config": proxy_config, "http_client": http_client})
    if proxy_config is not None:
        init_candidates.append({"proxy_config": proxy_config})
    if proxy_kwargs is not None and http_client is not None:
        init_candidates.append({"proxies": proxy_kwargs, "http_client": http_client})
    if proxy_kwargs is not None:
        init_candidates.append({"proxies": proxy_kwargs})
    if http_client is not None:
        init_candidates.append({"http_client": http_client})
    init_candidates.append({})
    return _instantiate_api_client(api_class, init_candidates)


def _fetch_transcript_raw(
    *,
    api_class: object,
    video_id: str,
    languages: list[str],
    proxy_url: Optional[str],
    request_timeout_sec: int,
) -> object:
    api = _create_youtube_transcript_api_client(
        api_class,
        proxy_url,
        request_timeout_sec=request_timeout_sec,
    )
    if hasattr(api, "fetch"):
        return _call_with_languages_and_optional_proxies(
            getattr(api, "fetch"),
            video_id=video_id,
            languages=languages,
            proxy_url=proxy_url,
        )

    if hasattr(api, "get_transcript"):
        return _call_with_languages_and_optional_proxies(
            getattr(api, "get_transcript"),
            video_id=video_id,
            languages=languages,
            proxy_url=proxy_url,
        )

    if hasattr(api_class, "get_transcript"):
        return _call_with_languages_and_optional_proxies(
            getattr(api_class, "get_transcript"),
            video_id=video_id,
            languages=languages,
            proxy_url=proxy_url,
        )

    raise RuntimeError("Unsupported youtube-transcript-api interface: missing fetch/get_transcript")


def transcript_options_from_settings(settings: object) -> dict[str, object]:
    return {
        "proxy_urls": getattr(settings, "AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_URLS", ""),
        "proxy_list_url": getattr(settings, "AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_URL", ""),
        "proxy_list_timeout_sec": max(
            2,
            int(getattr(settings, "AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_TIMEOUT_SEC", 8)),
        ),
        "proxy_max_tries": max(
            0,
            int(getattr(settings, "AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_MAX_TRIES", 6)),
        ),
        "request_timeout_sec": max(
            1,
            int(getattr(settings, "AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_REQUEST_TIMEOUT_SEC", 8)),
        ),
    }


def fetch_transcript(
    video_id: str,
    languages: list[str],
    *,
    proxy_urls: str | Iterable[str] | None = None,
    proxy_list_url: str | None = None,
    proxy_list_timeout_sec: int = 8,
    proxy_max_tries: int = 6,
    request_timeout_sec: int = 8,
) -> list[TranscriptSegment]:
    normalized_languages = [lang.strip() for lang in languages if lang.strip()]
    if not normalized_languages:
        normalized_languages = ["en"]

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise TranscriptError(
            "Missing dependency 'youtube-transcript-api'. Install requirements first."
        ) from exc

    last_error: Exception | None = None
    try:
        raw_segments = _fetch_transcript_raw(
            api_class=YouTubeTranscriptApi,
            video_id=video_id,
            languages=normalized_languages,
            proxy_url=None,
            request_timeout_sec=max(1, int(request_timeout_sec)),
        )
    except Exception as exc:  # pragma: no cover - external API errors are runtime-dependent
        last_error = exc

    if last_error is None:
        segments = _normalize_segments(raw_segments)
        if not segments:
            raise TranscriptError(
                "Transcript was fetched but returned empty content for the selected languages."
            )
        return segments

    if _message_has_any_marker(str(last_error), TRANSCRIPT_MISSING_MARKERS):
        message = str(last_error).strip() or "Unknown transcript fetch error"
        raise TranscriptError(
            f"Failed to fetch transcript for video '{video_id}'. Details: {message}"
        ) from last_error

    proxy_candidates = _build_proxy_candidates(
        proxy_urls=proxy_urls,
        proxy_list_url=proxy_list_url,
        proxy_list_timeout_sec=max(2, int(proxy_list_timeout_sec)),
        proxy_max_tries=max(0, int(proxy_max_tries)),
    )
    logger.info(
        "Transcript fetch direct failed for video_id=%s; proxy_candidates=%s",
        video_id,
        len(proxy_candidates),
    )
    if not proxy_candidates:
        message = str(last_error).strip() or "Unknown transcript fetch error"
        raise TranscriptError(
            f"Failed to fetch transcript for video '{video_id}'. Details: {message}"
        ) from last_error

    proxy_attempts = 0
    for idx, proxy_url in enumerate(proxy_candidates, start=1):
        try:
            raw_segments = _fetch_transcript_raw(
                api_class=YouTubeTranscriptApi,
                video_id=video_id,
                languages=normalized_languages,
                proxy_url=proxy_url,
                request_timeout_sec=max(1, int(request_timeout_sec)),
            )
            segments = _normalize_segments(raw_segments)
            if not segments:
                raise TranscriptError(
                    "Transcript was fetched but returned empty content for the selected languages."
                )
            logger.info(
                "Transcript fetch succeeded via proxy for video_id=%s attempt=%s/%s",
                video_id,
                idx,
                len(proxy_candidates),
            )
            return segments
        except Exception as exc:  # pragma: no cover - external API errors are runtime-dependent
            last_error = exc
            proxy_attempts += 1
            message = _normalize_space(str(exc))[:220] or "Unknown error"
            logger.info(
                "Transcript fetch proxy attempt failed video_id=%s attempt=%s/%s: %s",
                video_id,
                idx,
                len(proxy_candidates),
                message,
            )

    message = str(last_error).strip() or "Unknown transcript fetch error"
    raise TranscriptError(
        "Failed to fetch transcript for video "
        f"'{video_id}'. Details: {message} "
        f"(direct request + {proxy_attempts} proxy attempt(s) failed)"
    ) from last_error


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
