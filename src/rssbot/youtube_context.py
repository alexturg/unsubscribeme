from __future__ import annotations

from dataclasses import dataclass
import json
import re
import urllib.error
import urllib.request


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

SPACE_RE = re.compile(r"\s+")
URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)
INITIAL_DATA_MARKERS = (
    "var ytInitialData =",
    "window['ytInitialData'] =",
    'window["ytInitialData"] =',
    "ytInitialData =",
)
PLAYER_RESPONSE_MARKERS = (
    "var ytInitialPlayerResponse =",
    "window['ytInitialPlayerResponse'] =",
    'window["ytInitialPlayerResponse"] =',
    "ytInitialPlayerResponse =",
    '"ytInitialPlayerResponse":',
)


class VideoContextError(RuntimeError):
    """Raised when fallback YouTube context could not be extracted."""


@dataclass(frozen=True)
class VideoContext:
    video_id: str
    title: str
    short_description: str
    comments: list[str]
    watch_url: str


def _normalize_space(text: str) -> str:
    return SPACE_RE.sub(" ", (text or "")).strip()


def _word_count(text: str) -> int:
    return len(_normalize_space(text).split())


def _truncate_words(text: str, max_words: int) -> str:
    normalized = _normalize_space(text)
    if max_words < 1:
        return ""
    words = normalized.split()
    if len(words) <= max_words:
        return normalized
    return " ".join(words[:max_words]).strip()


def _clean_comment_text(text: str) -> str:
    cleaned = _normalize_space(URL_RE.sub("", text or ""))
    return cleaned.strip(" .,-")


def _renderer_text(value: object) -> str:
    if isinstance(value, str):
        return _normalize_space(value)
    if not isinstance(value, dict):
        return ""

    simple_text = value.get("simpleText")
    if isinstance(simple_text, str):
        return _normalize_space(simple_text)

    runs = value.get("runs")
    if isinstance(runs, list):
        parts: list[str] = []
        for run in runs:
            if isinstance(run, dict):
                text = run.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return _normalize_space("".join(parts))

    text = value.get("text")
    if isinstance(text, str):
        return _normalize_space(text)
    return ""


def _extract_json_object(raw_html: str, marker: str) -> dict[str, object] | None:
    search_from = 0
    while True:
        marker_pos = raw_html.find(marker, search_from)
        if marker_pos < 0:
            return None

        start = raw_html.find("{", marker_pos + len(marker))
        if start < 0:
            return None

        depth = 0
        in_string = False
        escaping = False
        end = None
        for idx in range(start, len(raw_html)):
            ch = raw_html[idx]
            if in_string:
                if escaping:
                    escaping = False
                elif ch == "\\":
                    escaping = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break

        if end is None:
            return None

        payload = raw_html[start:end]
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            search_from = marker_pos + len(marker)
            continue
        return parsed if isinstance(parsed, dict) else None


def _extract_json_by_markers(raw_html: str, markers: tuple[str, ...]) -> dict[str, object] | None:
    for marker in markers:
        parsed = _extract_json_object(raw_html, marker)
        if parsed:
            return parsed
    return None


def _extract_short_description(
    player_response: dict[str, object] | None,
    initial_data: dict[str, object] | None,
) -> str:
    if isinstance(player_response, dict):
        video_details = player_response.get("videoDetails")
        if isinstance(video_details, dict):
            desc = video_details.get("shortDescription")
            if isinstance(desc, str) and desc.strip():
                return _normalize_space(desc)

        microformat = player_response.get("microformat")
        if isinstance(microformat, dict):
            renderer = microformat.get("playerMicroformatRenderer")
            if isinstance(renderer, dict):
                description = renderer.get("description")
                text = _renderer_text(description)
                if text:
                    return text

    if not isinstance(initial_data, dict):
        return ""

    stack: list[object] = [initial_data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            attributed = current.get("attributedDescriptionBodyText")
            text = _renderer_text(attributed)
            if text:
                return text
            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return ""


def _extract_title(player_response: dict[str, object] | None, raw_html: str) -> str:
    if isinstance(player_response, dict):
        video_details = player_response.get("videoDetails")
        if isinstance(video_details, dict):
            title = video_details.get("title")
            if isinstance(title, str) and title.strip():
                return _normalize_space(title)

    title_match = re.search(r"<title>(.*?)</title>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        text = title_match.group(1)
        return _normalize_space(text.replace("- YouTube", ""))
    return ""


def _extract_comments(initial_data: dict[str, object] | None, max_comments: int) -> list[str]:
    if not isinstance(initial_data, dict) or max_comments < 1:
        return []

    comments: list[str] = []
    seen: set[str] = set()
    stack: list[object] = [initial_data]
    while stack and len(comments) < max_comments:
        current = stack.pop()
        if isinstance(current, dict):
            renderer = current.get("commentRenderer")
            if isinstance(renderer, dict):
                text = _renderer_text(renderer.get("contentText"))
                cleaned = _clean_comment_text(text)
                key = cleaned.casefold()
                if cleaned and key not in seen:
                    seen.add(key)
                    comments.append(cleaned)

            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)

    return comments


def fetch_video_context(
    video_id: str,
    *,
    timeout_sec: int = 15,
    max_html_bytes: int = 2_500_000,
    max_description_words: int = 220,
    max_comments: int = 12,
    max_comment_words: int = 36,
) -> VideoContext:
    if timeout_sec < 1:
        raise ValueError("timeout_sec must be >= 1")
    if max_html_bytes < 50_000:
        raise ValueError("max_html_bytes must be >= 50000")
    if max_description_words < 1:
        raise ValueError("max_description_words must be >= 1")
    if max_comments < 0:
        raise ValueError("max_comments must be >= 0")
    if max_comment_words < 3:
        raise ValueError("max_comment_words must be >= 3")

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    request = urllib.request.Request(
        f"{watch_url}&hl=en",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            payload = response.read(max_html_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise VideoContextError(f"YouTube fallback failed: HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc)).strip() or "network error"
        raise VideoContextError(f"YouTube fallback failed: {reason}.") from exc
    except Exception as exc:
        raise VideoContextError(f"YouTube fallback failed: {exc}") from exc

    if len(payload) > max_html_bytes:
        raise VideoContextError("YouTube fallback failed: page is too large.")

    try:
        raw_html = payload.decode("utf-8", errors="replace")
    except Exception as exc:
        raise VideoContextError(f"YouTube fallback failed: cannot decode page ({exc}).") from exc

    player_response = _extract_json_by_markers(raw_html, PLAYER_RESPONSE_MARKERS)
    initial_data = _extract_json_by_markers(raw_html, INITIAL_DATA_MARKERS)

    title = _extract_title(player_response, raw_html)
    short_description = _truncate_words(
        _extract_short_description(player_response, initial_data),
        max_words=max_description_words,
    )

    raw_comments = _extract_comments(initial_data, max_comments=max_comments)
    comments: list[str] = []
    for comment in raw_comments:
        trimmed = _truncate_words(comment, max_words=max_comment_words)
        if not trimmed:
            continue
        if _word_count(trimmed) < 2:
            continue
        comments.append(trimmed)

    if not short_description and not comments:
        raise VideoContextError(
            "Не удалось извлечь описание или комментарии с YouTube-страницы для fallback."
        )

    return VideoContext(
        video_id=video_id,
        title=title,
        short_description=short_description,
        comments=comments,
        watch_url=watch_url,
    )
