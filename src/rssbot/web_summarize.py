from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import threading
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET


SPACE_RE = re.compile(r"\s+")
WORD_RE = re.compile(r"\b[\w'-]+\b", flags=re.UNICODE)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
SUPPORTED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
REDIRECT_HTTP_CODES = {301, 302, 303, 307, 308}
XML_CONTENT_TYPES = {"application/xml", "text/xml", "application/rss+xml", "application/atom+xml"}
REDDIT_HOST_ALIASES = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "new.reddit.com",
    "m.reddit.com",
    "np.reddit.com",
    "redd.it",
}
MAX_REDDIT_COMMENTS = 32
REDDIT_SHARE_REDIRECT_USER_AGENT = "Twitterbot"
REDDIT_RSS_USER_AGENT = "UnsubscribeMe/0.1 (+https://github.com/alexturg/unsubscribeme)"
REDDIT_BLOCK_PATTERNS = (
    "you've been blocked by network security",
    "you are unable to access reddit",
    "this request has been blocked by our security service",
    "whoa there, pardner",
)
NOISE_PATTERNS = (
    "accept all",
    "all rights reserved",
    "by continuing to use",
    "cookie policy",
    "enable javascript",
    "gdpr",
    "privacy policy",
    "sign in",
    "sign up",
    "subscribe",
    "terms of service",
    "use of cookies",
    "we use cookies",
)
SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "canvas",
    "form",
    "button",
    "input",
    "textarea",
    "select",
    "option",
    "nav",
    "footer",
}
BLOCK_TAGS = {
    "article",
    "blockquote",
    "br",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "main",
    "p",
    "pre",
    "section",
    "tr",
    "td",
    "ul",
    "ol",
}
PREFERRED_TAGS = {"article", "main"}
DESCRIPTION_KEYS = {"description", "og:description", "twitter:description"}


class WebSummarizationError(RuntimeError):
    """Raised when a webpage cannot be fetched or prepared for summarization."""


@dataclass(frozen=True)
class WebPageContent:
    source_url: str
    title: str
    cleaned_text: str


_REDDIT_CONTENT_CACHE: dict[tuple, tuple[float, WebPageContent]] = {}
_REDDIT_CACHE_LOCK = threading.Lock()
_REDDIT_FETCH_LOCK = threading.RLock()


def _serialize_reddit_fetch(fetch):
    @wraps(fetch)
    def wrapped(raw_url, *args, **kwargs):
        url = normalize_web_url(raw_url)
        if _is_reddit_host(urlsplit(url).hostname):
            # Recheck caches inside the lock: a title lookup may still be
            # fetching when the user starts /ai for the same post.
            with _REDDIT_FETCH_LOCK:
                return fetch(url, *args, **kwargs)
        return fetch(url, *args, **kwargs)
    return wrapped


def _reddit_content_cache_key(url: str, max_bytes: int, max_words: int, user_agent: str):
    parsed = urlsplit(url)
    if not _is_reddit_host(parsed.hostname):
        return None
    path = parsed.path.rstrip("/")
    if path.endswith("/.rss"):
        path = path[:-5]
    elif path.endswith(".json"):
        path = path[:-5]
    query = urlencode([
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"raw_json", "share_id"} and not key.lower().startswith("utm_")
    ])
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in REDDIT_HOST_ALIASES and host != "redd.it":
        host = "reddit.com"
    return parsed.scheme, host, parsed.port, path.rstrip("/"), query, max_bytes, max_words, user_agent


def _cached_reddit_content(key):
    if key is None:
        return None
    with _REDDIT_CACHE_LOCK:
        entry = _REDDIT_CONTENT_CACHE.get(key)
        if entry and time.monotonic() - entry[0] < 300:
            return entry[1]
        _REDDIT_CONTENT_CACHE.pop(key, None)
    return None


def _cache_reddit_content(key, page: WebPageContent) -> None:
    if key is None:
        return
    with _REDDIT_CACHE_LOCK:
        if len(_REDDIT_CONTENT_CACHE) >= 64:
            _REDDIT_CONTENT_CACHE.pop(next(iter(_REDDIT_CONTENT_CACHE)))
        _REDDIT_CONTENT_CACHE[key] = (time.monotonic(), page)


def _reddit_cache_path(key, cache_dir: Path) -> Path:
    digest = hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def _read_reddit_disk_cache(key, cache_dir: Path | None, *, allow_stale: bool = False):
    if key is None or cache_dir is None:
        return None
    try:
        path = _reddit_cache_path(key, cache_dir)
        if path.stat().st_size > key[-3] * 2 + 10_000:
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(entry["fetched_at"])
        if age < 0 or age >= (86400 if allow_stale else 300):
            return None
        page = WebPageContent(**entry["page"])
        if not all(isinstance(value, str) for value in (page.source_url, page.title, page.cleaned_text)):
            return None
        if not page.cleaned_text.strip():
            return None
        if age >= 300:
            page = WebPageContent(
                source_url=page.source_url,
                title=page.title,
                cleaned_text=(
                    "Source note: Reddit is temporarily unavailable. This text was retrieved "
                    "from a cache less than 24 hours old; newer comments may be missing.\n"
                    + page.cleaned_text
                ),
            )
        return page
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_reddit_disk_cache(
    key, page: WebPageContent, cache_dir: Path | None, *, fetched_at: float | None = None
) -> None:
    if key is None or cache_dir is None:
        return
    temporary_path = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": time.time() if fetched_at is None else fetched_at,
            "page": {"source_url": page.source_url, "title": page.title, "cleaned_text": page.cleaned_text},
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_dir, delete=False) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False)
        os.replace(temporary_path, _reddit_cache_path(key, cache_dir))
        for path in sorted(cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[128:]:
            path.unlink(missing_ok=True)
    except OSError:
        logging.warning("Could not save Reddit source cache", exc_info=True)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp,
        code: int,
        msg: str,
        headers,
        newurl: str,
    ) -> None:
        return None


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.preferred_depth = 0
        self.in_title = False
        self.current_line_parts: list[str] = []
        self.primary_lines: list[str] = []
        self.secondary_lines: list[str] = []
        self.title_parts: list[str] = []
        self.meta_description = ""

    def _flush_line(self) -> None:
        if not self.current_line_parts:
            return
        line = _normalize_space(" ".join(self.current_line_parts))
        self.current_line_parts = []
        if not line:
            return
        target = self.primary_lines if self.preferred_depth > 0 else self.secondary_lines
        target.append(line)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {k.lower(): (v or "") for k, v in attrs if k}

        if tag == "meta" and not self.meta_description:
            meta_key = (
                attrs_map.get("name")
                or attrs_map.get("property")
                or attrs_map.get("itemprop")
                or ""
            ).strip().lower()
            if meta_key in DESCRIPTION_KEYS:
                content = _normalize_space(attrs_map.get("content", ""))
                if content:
                    self.meta_description = content

        if tag in BLOCK_TAGS:
            self._flush_line()

        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return

        if self.skip_depth == 0 and tag in PREFERRED_TAGS:
            self.preferred_depth += 1

        if self.skip_depth == 0 and tag == "title":
            self.in_title = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in {"meta", "br"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag in BLOCK_TAGS:
            self._flush_line()
        if tag in PREFERRED_TAGS and self.preferred_depth > 0:
            self.preferred_depth -= 1
        if tag in SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth > 0:
            return
        text = _normalize_space(data)
        if not text:
            return
        self.current_line_parts.append(text)
        if self.in_title:
            self.title_parts.append(text)


def _normalize_space(text: str) -> str:
    return SPACE_RE.sub(" ", text).strip()


def _word_count(text: str) -> int:
    return len(WORD_RE.findall(text))


def _is_reddit_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    return host in REDDIT_HOST_ALIASES or host.endswith(".reddit.com")


def _normalize_reddit_subreddit(subreddit: str) -> str:
    normalized = _normalize_space(subreddit)
    if normalized.lower().startswith("r/"):
        return normalized[2:].strip()
    return normalized


def _normalize_reddit_author(author: str) -> str:
    normalized = _normalize_space(author)
    if normalized.lower().startswith("u/"):
        return normalized[2:].strip()
    return normalized


def _is_reddit_share_url(url: str) -> bool:
    parsed = urlsplit(url)
    return bool(
        _is_reddit_host(parsed.hostname)
        and re.fullmatch(r"/r/[^/]+/s/[^/]+/?", parsed.path, flags=re.IGNORECASE)
    )


def _reddit_redirect_url(current_url: str, location: str) -> str:
    target = urljoin(current_url, location)
    parsed = urlsplit(target)
    # Reddit share links add tracking parameters to the canonical post URL.
    # Fetching that URL without them also matches the working direct-link path.
    if _is_reddit_share_url(current_url) and _is_reddit_host(parsed.hostname):
        if re.match(r"/r/[^/]+/comments/[^/]+(?:/|$)", parsed.path, flags=re.IGNORECASE):
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return target


def _reddit_post_feed_url(url: str) -> str:
    parsed = urlsplit(url)
    if not _is_reddit_host(parsed.hostname):
        return url
    if not re.match(r"/(?:r/[^/]+/)?comments/[a-z0-9]+(?:/|$)", parsed.path, re.IGNORECASE):
        return url
    if parsed.path.lower().endswith((".json", ".rss")):
        return url
    query = urlencode([
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"raw_json", "share_id"} and not key.lower().startswith("utm_")
    ])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/.rss", query, ""))


def _next_reddit_fallback_url(current_url: str) -> str | None:
    parsed = urlsplit(current_url)
    if not _is_reddit_host(parsed.hostname):
        return None
    if _is_reddit_share_url(current_url):
        return None
    if parsed.path.lower().endswith(".rss"):
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "old.reddit.com":
        port = parsed.port
        netloc = "old.reddit.com" if port is None else f"old.reddit.com:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))

    path = parsed.path or "/"
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    has_raw_json = any(key.lower() == "raw_json" for key, _ in query_items)

    if not path.lower().endswith(".json"):
        json_path = "/.json" if path in {"", "/"} else f"{path.rstrip('/')}.json"
        if not has_raw_json:
            query_items.append(("raw_json", "1"))
        return urlunsplit((parsed.scheme, parsed.netloc, json_path, urlencode(query_items), ""))

    if not has_raw_json:
        query_items.append(("raw_json", "1"))
        return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query_items), ""))

    normalized_path = path[:-5] if path.lower().endswith(".json") else path
    if "/comments/" in normalized_path and not normalized_path.lower().endswith("/.rss"):
        rss_path = normalized_path if normalized_path.endswith("/") else f"{normalized_path}/"
        rss_path = f"{rss_path}.rss"
        port = parsed.port
        netloc = "www.reddit.com" if port is None else f"www.reddit.com:{port}"
        rss_query = urlencode(
            [(key, value) for key, value in query_items if key.lower() != "raw_json"]
        )
        return urlunsplit((parsed.scheme, netloc, rss_path, rss_query, ""))

    return None


def _looks_like_reddit_access_block(text: str) -> bool:
    lowered = (text or "").lower()
    if "reddit" not in lowered:
        return False
    return any(pattern in lowered for pattern in REDDIT_BLOCK_PATTERNS)


def _is_public_ip(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return False
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return ip.is_global


def _host_ips(hostname: str, port: int) -> set[str]:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebSummarizationError(f"Не удалось резолвить хост: {hostname}") from exc

    ips: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if ip:
            ips.add(ip)
    return ips


def _ensure_public_host(hostname: str, port: int) -> None:
    try:
        if not _is_public_ip(hostname):
            raise WebSummarizationError(
                "URL указывает на внутренний или небезопасный IP-адрес."
            )
        return
    except ValueError:
        pass

    ips = _host_ips(hostname, port)
    if not ips:
        raise WebSummarizationError(f"Не удалось определить IP для хоста: {hostname}")

    non_public = sorted(ip for ip in ips if not _is_public_ip(ip))
    if non_public:
        raise WebSummarizationError(
            "URL резолвится во внутренний или небезопасный адрес и заблокирован."
        )


def normalize_web_url(raw_url: str) -> str:
    value = (raw_url or "").strip()
    if not value:
        raise WebSummarizationError("Пустой URL.")

    if "://" not in value:
        value = f"https://{value}"

    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise WebSummarizationError("Разрешены только URL со схемой http/https.")

    if parsed.username or parsed.password:
        raise WebSummarizationError("URL с userinfo не поддерживаются.")

    if not parsed.hostname:
        raise WebSummarizationError("Некорректный URL: отсутствует host.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise WebSummarizationError("Некорректный порт в URL.") from exc

    if port is not None and not (1 <= port <= 65535):
        raise WebSummarizationError("Порт URL вне допустимого диапазона.")

    try:
        ascii_host = parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebSummarizationError("Некорректный host в URL.") from exc

    if ":" in ascii_host and not ascii_host.startswith("["):
        ascii_host = f"[{ascii_host}]"

    netloc = ascii_host if port is None else f"{ascii_host}:{port}"
    path = quote(parsed.path or "", safe="/%:@-._~!$&'()*+,;=")
    query = quote(parsed.query or "", safe="=&%:@-._~!$'()*+,;/?")
    return urlunsplit((scheme, netloc, path, query, ""))


def validate_web_url_for_fetch(raw_url: str) -> str:
    url = normalize_web_url(raw_url)
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    assert parsed.hostname is not None
    _ensure_public_host(parsed.hostname, port)
    return url


def _decode_payload(payload: bytes, content_type_header: str) -> str:
    charset = None
    if "charset=" in content_type_header.lower():
        charset = content_type_header.lower().split("charset=", maxsplit=1)[1].split(";", 1)[0]
        charset = charset.strip(" '\"")

    candidates = [charset, "utf-8", "cp1251", "latin-1"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return payload.decode(candidate, errors="strict")
        except Exception:
            continue
    return payload.decode("utf-8", errors="replace")


def _read_limited(response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise WebSummarizationError(
                f"Размер страницы превышает лимит {max_bytes} байт."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _is_noise_line(line: str) -> bool:
    lowered = line.lower()
    if len(line) < 120 and any(pattern in lowered for pattern in NOISE_PATTERNS):
        return True
    if lowered.startswith(("http://", "https://")) and _word_count(line) < 4:
        return True
    if line.count("|") >= 4 and _word_count(line) < 8:
        return True
    return False


def _dedupe_and_filter_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in lines:
        normalized = _normalize_space(line)
        if not normalized:
            continue
        if _is_noise_line(normalized):
            continue
        if _word_count(normalized) < 3 and len(normalized) < 20:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _limit_lines_by_words(lines: list[str], max_words: int) -> list[str]:
    if max_words < 1:
        return []
    kept: list[str] = []
    used_words = 0
    for line in lines:
        count = _word_count(line)
        if count <= 0:
            continue
        if kept and used_words + count > max_words:
            break
        kept.append(line)
        used_words += count
    return kept


def extract_readable_text(raw_text: str, max_words: int = 4500) -> tuple[str, str]:
    parser = _ReadableHTMLParser()
    parser.feed(raw_text)
    parser._flush_line()

    title = _normalize_space(" ".join(parser.title_parts))
    description = _normalize_space(parser.meta_description)

    ordered_lines = parser.primary_lines + parser.secondary_lines
    if len(parser.primary_lines) < 5:
        ordered_lines = parser.secondary_lines + parser.primary_lines

    content_lines = _dedupe_and_filter_lines(ordered_lines)

    meta_lines: list[str] = []
    if title:
        meta_lines.append(f"Title: {title}")
    if description and description.lower() != title.lower():
        meta_lines.append(f"Description: {description}")

    meta_words = sum(_word_count(line) for line in meta_lines)
    content_budget = max(120, max_words - meta_words)
    trimmed_content = _limit_lines_by_words(content_lines, max_words=content_budget)

    if not trimmed_content and description:
        trimmed_content = [description]
    if not trimmed_content and title:
        trimmed_content = [title]

    content_text = "\n".join(trimmed_content).strip()
    if meta_lines and content_text:
        return title, "\n".join(meta_lines + ["Content:", content_text])
    if meta_lines:
        return title, "\n".join(meta_lines)
    return title, content_text


def _extract_text_from_plaintext(raw_text: str, max_words: int) -> tuple[str, str]:
    lines = [_normalize_space(line) for line in raw_text.splitlines()]
    clean_lines = _dedupe_and_filter_lines(lines)
    trimmed = _limit_lines_by_words(clean_lines, max_words=max_words)
    return "", "\n".join(trimmed)


def _xml_local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", maxsplit=1)[1].lower()
    return tag.lower()


def _xml_first_child_text(node: ET.Element, names: set[str]) -> str:
    for child in list(node):
        if _xml_local_name(child.tag) not in names:
            continue
        text = _normalize_space(" ".join(part for part in child.itertext()))
        if text:
            return text
    return ""


def _reddit_listing_children(value: object) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        return []
    data = value.get("data")
    if not isinstance(data, dict):
        return []
    children = data.get("children")
    if not isinstance(children, list):
        return []
    return [child for child in children if isinstance(child, dict)]


def _extract_reddit_post(value: object) -> tuple[str, str, str, str]:
    for child in _reddit_listing_children(value):
        payload = child.get("data")
        if not isinstance(payload, dict):
            continue
        title = _normalize_space(str(payload.get("title") or ""))
        body = _normalize_space(str(payload.get("selftext") or payload.get("body") or ""))
        subreddit = _normalize_reddit_subreddit(str(payload.get("subreddit") or ""))
        author = _normalize_reddit_author(str(payload.get("author") or ""))
        if title or body or subreddit or author:
            return title, body, subreddit, author
    return "", "", "", ""


def _collect_reddit_comment_bodies(value: object, out: list[str], max_items: int) -> None:
    if len(out) >= max_items:
        return

    if isinstance(value, list):
        for item in value:
            _collect_reddit_comment_bodies(item, out, max_items)
            if len(out) >= max_items:
                return
        return

    if not isinstance(value, dict):
        return

    kind = str(value.get("kind") or "").lower()
    payload = value.get("data")

    if kind == "t1" and isinstance(payload, dict):
        body = _normalize_space(str(payload.get("body") or ""))
        if body:
            out.append(body)
            if len(out) >= max_items:
                return
        replies = payload.get("replies")
        if isinstance(replies, (dict, list)):
            _collect_reddit_comment_bodies(replies, out, max_items)
        return

    for child in _reddit_listing_children(value):
        _collect_reddit_comment_bodies(child, out, max_items)
        if len(out) >= max_items:
            return


def _extract_text_from_reddit_json(raw_text: str, max_words: int) -> tuple[str, str]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise WebSummarizationError("Не удалось разобрать JSON-представление Reddit.") from exc

    post_title = ""
    post_body = ""
    subreddit = ""
    author = ""
    comments: list[str] = []

    if isinstance(payload, list):
        if payload:
            post_title, post_body, subreddit, author = _extract_reddit_post(payload[0])
        if len(payload) > 1:
            _collect_reddit_comment_bodies(payload[1], comments, MAX_REDDIT_COMMENTS)
    elif isinstance(payload, dict):
        post_title, post_body, subreddit, author = _extract_reddit_post(payload)
        _collect_reddit_comment_bodies(payload, comments, MAX_REDDIT_COMMENTS)
    else:
        raise WebSummarizationError("Неожиданный формат JSON от Reddit.")

    lines: list[str] = []
    if post_title:
        lines.append(f"Title: {post_title}")
    if subreddit:
        lines.append(f"Subreddit: r/{subreddit}")
    if author:
        lines.append(f"Author: u/{author}")
    if post_body:
        lines.append(f"Post: {post_body}")
    for idx, comment in enumerate(comments, start=1):
        lines.append(f"Comment {idx}: {comment}")

    cleaned_lines = _dedupe_and_filter_lines(lines)
    trimmed_lines = _limit_lines_by_words(cleaned_lines, max_words=max_words)
    return post_title, "\n".join(trimmed_lines)


def _extract_text_from_xml_feed(raw_text: str, max_words: int) -> tuple[str, str]:
    try:
        root = ET.fromstring(raw_text)
    except ET.ParseError as exc:
        raise WebSummarizationError("Не удалось разобрать XML/RSS страницу.") from exc

    root_name = _xml_local_name(root.tag)
    container = root
    if root_name == "rss":
        for child in list(root):
            if _xml_local_name(child.tag) == "channel":
                container = child
                break

    title = _xml_first_child_text(container, {"title"})
    lines: list[str] = []
    if title:
        lines.append(f"Title: {title}")

    entry_nodes = [
        node for node in container.iter() if _xml_local_name(node.tag) in {"entry", "item"}
    ]
    for idx, entry in enumerate(entry_nodes[: MAX_REDDIT_COMMENTS + 1], start=1):
        entry_title = _xml_first_child_text(entry, {"title"})
        entry_body = _xml_first_child_text(entry, {"content", "summary", "description"})
        entry_author = _xml_first_child_text(entry, {"author", "creator", "name"})

        parts: list[str] = []
        if entry_title:
            parts.append(entry_title)
        if entry_author:
            parts.append(f"by {entry_author}")
        header = " - ".join(parts)
        if header:
            line = f"Entry {idx}: {header}"
            if entry_body:
                line = f"{line}. {entry_body}"
        else:
            line = entry_body
        normalized = _normalize_space(line)
        if normalized:
            lines.append(normalized)

    cleaned = _dedupe_and_filter_lines(lines)
    trimmed = _limit_lines_by_words(cleaned, max_words=max_words)
    return title, "\n".join(trimmed)


def _open_web_request(opener, request: urllib.request.Request, timeout_sec: int):
    try:
        return opener.open(request, timeout=timeout_sec)
    except urllib.error.HTTPError as exc:
        if exc.code != 429 or not _is_reddit_host(urlsplit(request.full_url).hostname):
            raise
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        is_rss = urlsplit(request.full_url).path.lower().endswith(".rss")
        max_delay = 60 if is_rss else 5
        try:
            delay = max(1, int(retry_after)) if retry_after else (60 if is_rss else 2)
        except ValueError:
            raise exc
        # Keep the retry within the summarizer's overall timeout. Longer limits
        # must be reported rather than repeatedly requesting the same endpoint.
        if delay > max_delay:
            raise
        if exc.fp is not None:
            exc.close()
        logging.info("Reddit returned 429; retrying once in %s seconds", delay)
        time.sleep(delay)
        return opener.open(request, timeout=timeout_sec)


@_serialize_reddit_fetch
def fetch_webpage_content(
    raw_url: str,
    *,
    timeout_sec: int = 15,
    max_bytes: int = 2_000_000,
    max_redirects: int = 4,
    max_words: int = 4500,
    user_agent: str = DEFAULT_USER_AGENT,
    cache_dir: Path | None = None,
) -> WebPageContent:
    if timeout_sec < 1:
        raise WebSummarizationError("timeout_sec must be >= 1")
    if max_bytes < 1024:
        raise WebSummarizationError("max_bytes must be >= 1024")
    if max_redirects < 0:
        raise WebSummarizationError("max_redirects must be >= 0")

    current_url = validate_web_url_for_fetch(raw_url)
    original_cache_key = _reddit_content_cache_key(current_url, max_bytes, max_words, user_agent)
    opener = urllib.request.build_opener(_NoRedirectHandler())
    request_headers = {
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1",
        "Accept-Encoding": "identity",
        "User-Agent": user_agent,
    }

    for _ in range(max_redirects + 1):
        current_url = validate_web_url_for_fetch(current_url)
        cache_key = _reddit_content_cache_key(current_url, max_bytes, max_words, user_agent)
        cached_page = _cached_reddit_content(cache_key)
        if cached_page is None:
            cached_page = _read_reddit_disk_cache(cache_key, cache_dir)
        if cached_page is not None:
            if cache_dir is not None and original_cache_key != cache_key:
                try:
                    saved_at = json.loads(
                        _reddit_cache_path(cache_key, cache_dir).read_text(encoding="utf-8")
                    )["fetched_at"]
                    _write_reddit_disk_cache(
                        original_cache_key, cached_page, cache_dir, fetched_at=float(saved_at)
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            return cached_page
        current_url = _reddit_post_feed_url(current_url)
        headers = dict(request_headers)
        if _is_reddit_host(urlsplit(current_url).hostname) and urlsplit(current_url).path.lower().endswith(".rss"):
            headers["User-Agent"] = REDDIT_RSS_USER_AGENT
            headers["Accept"] = "application/atom+xml,application/rss+xml"
        elif _is_reddit_share_url(current_url):
            headers["User-Agent"] = REDDIT_SHARE_REDIRECT_USER_AGENT
        request = urllib.request.Request(current_url, headers=headers, method="GET")

        try:
            with _open_web_request(opener, request, timeout_sec) as response:
                final_url = validate_web_url_for_fetch(response.geturl() or current_url)
                final_parts = urlsplit(final_url)
                content_type_header = response.headers.get("Content-Type", "")
                content_type = content_type_header.split(";", 1)[0].strip().lower()
                is_reddit_json = _is_reddit_host(final_parts.hostname) and (
                    content_type == "application/json" or final_parts.path.lower().endswith(".json")
                )
                is_xml_feed = content_type in XML_CONTENT_TYPES or final_parts.path.lower().endswith(
                    ".rss"
                )
                if (
                    content_type
                    and content_type not in SUPPORTED_CONTENT_TYPES
                    and not is_reddit_json
                    and not is_xml_feed
                ):
                    raise WebSummarizationError(
                        f"Неподдерживаемый Content-Type: {content_type or 'unknown'}."
                    )
                payload = _read_limited(response, max_bytes=max_bytes)
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 429}:
                if _is_reddit_share_url(current_url):
                    # Some Reddit edges reject browser requests to /s/... but
                    # expose the canonical post to link-preview clients.
                    head_request = urllib.request.Request(
                        current_url,
                        headers={
                            **request_headers,
                            "User-Agent": REDDIT_SHARE_REDIRECT_USER_AGENT,
                        },
                        method="HEAD",
                    )
                    try:
                        with opener.open(head_request, timeout=timeout_sec) as head_response:
                            head_location = head_response.headers.get("Location")
                    except urllib.error.HTTPError as head_exc:
                        head_location = (
                            head_exc.headers.get("Location")
                            if head_exc.code in REDIRECT_HTTP_CODES and head_exc.headers
                            else None
                        )
                    except urllib.error.URLError:
                        head_location = None
                    if head_location:
                        current_url = _reddit_redirect_url(current_url, head_location)
                        continue
                reddit_fallback_url = _next_reddit_fallback_url(current_url)
                if reddit_fallback_url:
                    current_url = reddit_fallback_url
                    continue
            if exc.code in REDIRECT_HTTP_CODES:
                location = exc.headers.get("Location") if exc.headers else None
                if not location:
                    raise WebSummarizationError(
                        f"Редирект без Location (HTTP {exc.code})."
                    ) from exc
                current_url = _reddit_redirect_url(current_url, location)
                continue
            logging.warning("Web fetch failed: HTTP %s for %s", exc.code, current_url)
            if exc.code in {403, 429}:
                for key in (original_cache_key, cache_key):
                    saved_page = _read_reddit_disk_cache(key, cache_dir, allow_stale=True)
                    if saved_page is not None:
                        logging.warning("Using saved Reddit text after HTTP %s", exc.code)
                        return saved_page
            raise WebSummarizationError(
                f"Не удалось загрузить страницу: HTTP {exc.code}."
            ) from exc
        except urllib.error.URLError as exc:
            reason = str(exc.reason).strip() if getattr(exc, "reason", None) else str(exc).strip()
            raise WebSummarizationError(
                f"Не удалось загрузить страницу: {reason or 'network error'}."
            ) from exc

        decoded = _decode_payload(payload, content_type_header)
        if _looks_like_reddit_access_block(decoded):
            reddit_fallback_url = _next_reddit_fallback_url(current_url)
            if reddit_fallback_url:
                current_url = reddit_fallback_url
                continue
            raise WebSummarizationError(
                "Reddit ограничил доступ к странице с этого IP/сети. "
                "Попробуйте другой IP/VPN или пришлите текст поста вручную."
            )
        if is_reddit_json:
            title, cleaned_text = _extract_text_from_reddit_json(decoded, max_words=max_words)
        elif is_xml_feed:
            title, cleaned_text = _extract_text_from_xml_feed(decoded, max_words=max_words)
        elif content_type == "text/plain":
            title, cleaned_text = _extract_text_from_plaintext(decoded, max_words=max_words)
        else:
            title, cleaned_text = extract_readable_text(decoded, max_words=max_words)

        if not cleaned_text.strip():
            raise WebSummarizationError(
                "Не удалось извлечь читаемый текст из страницы."
            )
        page = WebPageContent(source_url=final_url, title=title, cleaned_text=cleaned_text)
        _cache_reddit_content(original_cache_key, page)
        _write_reddit_disk_cache(original_cache_key, page, cache_dir)
        final_cache_key = _reddit_content_cache_key(final_url, max_bytes, max_words, user_agent)
        if final_cache_key != original_cache_key:
            _write_reddit_disk_cache(final_cache_key, page, cache_dir)
        _cache_reddit_content(
            _reddit_content_cache_key(final_url, max_bytes, max_words, user_agent), page
        )
        return page

    raise WebSummarizationError("Слишком много редиректов при загрузке страницы.")
