from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit


SPACE_RE = re.compile(r"\s+")
WORD_RE = re.compile(r"\b[\w'-]+\b", flags=re.UNICODE)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
SUPPORTED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
REDIRECT_HTTP_CODES = {301, 302, 303, 307, 308}
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


def _next_reddit_fallback_url(current_url: str) -> str | None:
    parsed = urlsplit(current_url)
    if not _is_reddit_host(parsed.hostname):
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

    return None


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


def fetch_webpage_content(
    raw_url: str,
    *,
    timeout_sec: int = 15,
    max_bytes: int = 2_000_000,
    max_redirects: int = 4,
    max_words: int = 4500,
    user_agent: str = DEFAULT_USER_AGENT,
) -> WebPageContent:
    if timeout_sec < 1:
        raise WebSummarizationError("timeout_sec must be >= 1")
    if max_bytes < 1024:
        raise WebSummarizationError("max_bytes must be >= 1024")
    if max_redirects < 0:
        raise WebSummarizationError("max_redirects must be >= 0")

    current_url = validate_web_url_for_fetch(raw_url)
    opener = urllib.request.build_opener(_NoRedirectHandler())
    request_headers = {
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1",
        "Accept-Encoding": "identity",
        "User-Agent": user_agent,
    }

    for _ in range(max_redirects + 1):
        current_url = validate_web_url_for_fetch(current_url)
        request = urllib.request.Request(current_url, headers=request_headers, method="GET")

        try:
            with opener.open(request, timeout=timeout_sec) as response:
                final_url = validate_web_url_for_fetch(response.geturl() or current_url)
                final_parts = urlsplit(final_url)
                content_type_header = response.headers.get("Content-Type", "")
                content_type = content_type_header.split(";", 1)[0].strip().lower()
                is_reddit_json = _is_reddit_host(final_parts.hostname) and (
                    content_type == "application/json" or final_parts.path.lower().endswith(".json")
                )
                if content_type and content_type not in SUPPORTED_CONTENT_TYPES and not is_reddit_json:
                    raise WebSummarizationError(
                        f"Неподдерживаемый Content-Type: {content_type or 'unknown'}."
                    )
                payload = _read_limited(response, max_bytes=max_bytes)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
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
                current_url = urljoin(current_url, location)
                continue
            raise WebSummarizationError(
                f"Не удалось загрузить страницу: HTTP {exc.code}."
            ) from exc
        except urllib.error.URLError as exc:
            reason = str(exc.reason).strip() if getattr(exc, "reason", None) else str(exc).strip()
            raise WebSummarizationError(
                f"Не удалось загрузить страницу: {reason or 'network error'}."
            ) from exc

        decoded = _decode_payload(payload, content_type_header)
        if is_reddit_json:
            title, cleaned_text = _extract_text_from_reddit_json(decoded, max_words=max_words)
        elif content_type == "text/plain":
            title, cleaned_text = _extract_text_from_plaintext(decoded, max_words=max_words)
        else:
            title, cleaned_text = extract_readable_text(decoded, max_words=max_words)

        if not cleaned_text.strip():
            raise WebSummarizationError(
                "Не удалось извлечь читаемый текст из страницы."
            )
        return WebPageContent(source_url=final_url, title=title, cleaned_text=cleaned_text)

    raise WebSummarizationError("Слишком много редиректов при загрузке страницы.")
