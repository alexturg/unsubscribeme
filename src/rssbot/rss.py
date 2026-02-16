from __future__ import annotations

import asyncio
import hashlib
import calendar
import json
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo
import re

import aiohttp
import feedparser

from .db import Feed, Item, session_scope
from .config import Settings


def _extract_video_id(entry: feedparser.FeedParserDict) -> Optional[str]:
    # YouTube entries often have id like 'yt:video:VIDEOID' or link '...watch?v=VIDEOID'
    eid = entry.get("id") or ""
    if isinstance(eid, str) and ":video:" in eid:
        return eid.split(":video:")[-1]
    link = entry.get("link") or ""
    if "watch?v=" in link:
        return link.split("watch?v=")[-1].split("&")[0]
    return None


def _published_at(entry: feedparser.FeedParserDict) -> Optional[datetime]:
    """Return published/updated datetime in UTC.

    feedparser v6 provides struct_time in entry.published_parsed/updated_parsed.
    Use calendar.timegm to get correct UTC epoch instead of deprecated helpers.
    """
    try:
        if entry.get("published_parsed"):
            ts = calendar.timegm(entry.published_parsed)
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        if entry.get("updated_parsed"):
            ts = calendar.timegm(entry.updated_parsed)
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        pass
    # Fallback: try to parse ISO strings if present
    for key in ("published", "updated"):
        val = entry.get(key)
        if isinstance(val, str) and val:
            try:
                # datetime.fromisoformat won't parse Z; handle common cases
                s = val.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
    return None


def _summary_hash(entry: feedparser.FeedParserDict) -> Optional[str]:
    text = (entry.get("summary") or entry.get("description") or "").strip()
    if not text:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _parse_event_datetime(value: Any, default_tz: ZoneInfo) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None

    # Support common ISO forms, including a trailing Z.
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=default_tz)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # Support "DD.MM.YYYY HH:MM" as a practical fallback.
    m = re.match(
        r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*$",
        raw,
    )
    if not m:
        return None
    day, month, year, hour, minute = [int(x) for x in m.groups()]
    try:
        dt_local = datetime(year, month, day, hour, minute, tzinfo=default_tz)
        return dt_local.astimezone(timezone.utc)
    except Exception:
        return None


def _normalized_event_rows(payload: Any, default_tz: ZoneInfo) -> List[dict[str, Any]]:
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("events"), list):
        rows = payload["events"]
    else:
        return []

    normalized: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("name") or "").strip()
        link = str(raw.get("link") or raw.get("url") or "").strip()
        start_raw = raw.get("start_at", raw.get("starts_at", raw.get("start", raw.get("time"))))
        start_at = _parse_event_datetime(start_raw, default_tz)
        if not title or not link or not start_at:
            continue
        external_id = str(raw.get("id") or raw.get("external_id") or "").strip()
        if not external_id:
            # Fallback id if the parser doesn't provide one.
            # Strongly recommended: send stable `id` in payload.
            seed = f"{title}\n{link}\n{start_at.isoformat()}"
            external_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        normalized.append(
            {
                "external_id": external_id,
                "title": title,
                "link": link,
                "published_at": start_at,
            }
        )
    return normalized


def _unfold_ics_lines(text: str) -> list[str]:
    """Join folded iCalendar lines (RFC 5545)."""
    unfolded: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if (raw.startswith(" ") or raw.startswith("\t")) and unfolded:
            unfolded[-1] += raw[1:]
        else:
            unfolded.append(raw)
    return unfolded


def _ics_parse_key_params_and_value(line: str) -> tuple[str, dict[str, str], str]:
    if ":" not in line:
        return "", {}, ""
    head, value = line.split(":", 1)
    chunks = head.split(";")
    key = chunks[0].strip().upper()
    params: dict[str, str] = {}
    for chunk in chunks[1:]:
        if "=" not in chunk:
            continue
        p_key, p_val = chunk.split("=", 1)
        params[p_key.strip().upper()] = p_val.strip()
    return key, params, value.strip()


def _ics_unescape_text(value: str) -> str:
    return (
        value.replace("\\N", "\n")
        .replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _extract_first_url(text: str) -> Optional[str]:
    m = re.search(r"https?://[^\s<>()]+", text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,);")


def _parse_ics_datetime(value: str, params: dict[str, str], default_tz: ZoneInfo) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None

    tz: ZoneInfo = default_tz
    tzid = (params.get("TZID") or "").strip()
    if tzid:
        try:
            tz = ZoneInfo(tzid)
        except Exception:
            tz = default_tz

    # UTC format, e.g. 20260210T163000Z
    if raw.endswith("Z"):
        for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%MZ"):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue

    value_kind = (params.get("VALUE") or "").strip().upper()
    if value_kind == "DATE" or ("T" not in raw and len(raw) == 8):
        try:
            dt_local = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=tz)
            return dt_local.astimezone(timezone.utc)
        except Exception:
            return None

    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            dt_local = datetime.strptime(raw, fmt).replace(tzinfo=tz)
            return dt_local.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _normalized_ics_event_rows(
    content: bytes,
    default_tz: ZoneInfo,
    fallback_link: Optional[str] = None,
) -> List[dict[str, Any]]:
    text = content.decode("utf-8-sig", errors="replace")
    lines = _unfold_ics_lines(text)

    normalized: list[dict[str, Any]] = []
    event: Optional[dict[str, Any]] = None
    for line in lines:
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            event = {}
            continue
        if upper == "END:VEVENT":
            if not event:
                continue
            title = str(event.get("summary") or "").strip()
            start_at = event.get("start_at")
            if not isinstance(start_at, datetime):
                continue
            if not title:
                title = "Событие"
            link = str(event.get("url") or "").strip()
            if not link:
                description = str(event.get("description") or "")
                link = _extract_first_url(description) or ""
            if not link:
                link = (fallback_link or "").strip()
            if not link:
                continue

            uid = str(event.get("uid") or "").strip()
            recurrence_id = str(event.get("recurrence_id") or "").strip()
            external_id = uid
            if uid and recurrence_id:
                external_id = f"{uid}::{recurrence_id}"
            if not external_id:
                seed = f"{title}\n{link}\n{start_at.isoformat()}"
                external_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()

            normalized.append(
                {
                    "external_id": external_id,
                    "title": title,
                    "link": link,
                    "published_at": start_at,
                }
            )
            event = None
            continue
        if event is None or ":" not in line:
            continue

        key, params, value = _ics_parse_key_params_and_value(line)
        if not key:
            continue
        if key == "UID":
            event["uid"] = _ics_unescape_text(value)
        elif key == "SUMMARY":
            event["summary"] = _ics_unescape_text(value)
        elif key == "DESCRIPTION":
            event["description"] = _ics_unescape_text(value)
        elif key == "URL":
            event["url"] = _ics_unescape_text(value)
        elif key == "DTSTART":
            parsed = _parse_ics_datetime(value, params, default_tz)
            if parsed:
                event["start_at"] = parsed
        elif key == "RECURRENCE-ID":
            event["recurrence_id"] = _ics_unescape_text(value)

    return normalized


async def fetch_feed_http(feed: Feed) -> Tuple[int, Optional[str], Optional[str], Optional[bytes]]:
    headers = {}
    if feed.http_etag:
        headers["If-None-Match"] = feed.http_etag
    if feed.http_last_modified:
        headers["If-Modified-Since"] = feed.http_last_modified

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(feed.url, headers=headers) as resp:
            if resp.status == 304:
                return 304, resp.headers.get("ETag"), resp.headers.get("Last-Modified"), None
            content = await resp.read()
            return resp.status, resp.headers.get("ETag"), resp.headers.get("Last-Modified"), content


async def fetch_and_store_event_source(feed_id: int) -> List[int]:
    """Fetch events source (JSON or ICS) and upsert into items.

    For `feed.type=event_json`, expected payload format:
      {"events":[{"id":"...", "title":"...", "link":"...", "start_at":"ISO-8601"}]}
    or
      [{"id":"...", "title":"...", "link":"...", "start_at":"ISO-8601"}]

    For `feed.type=event_ics`, expected payload is a standard iCalendar (.ics).
    """
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or not feed.enabled:
            return []
        feed_type = (feed.type or "").strip().lower()

    status, etag, last_modified, content = await fetch_feed_http(feed)
    with session_scope() as s:
        f = s.get(Feed, feed_id)
        f.http_etag = etag or f.http_etag
        f.http_last_modified = last_modified or f.http_last_modified
        f.last_poll_at = datetime.now(timezone.utc)
    if status == 304 or not content:
        return []
    if status != 200:
        return []

    settings = Settings()
    default_tz = ZoneInfo(settings.TZ or "UTC")
    events: list[dict[str, Any]]
    if feed_type == "event_ics":
        events = _normalized_ics_event_rows(content, default_tz, fallback_link=feed.url)
    else:
        try:
            payload = json.loads(content.decode("utf-8"))
        except Exception:
            return []
        events = _normalized_event_rows(payload, default_tz)
    if not events:
        return []

    created_ids: list[int] = []
    with session_scope() as s:
        f = s.get(Feed, feed_id)
        for event in events:
            event_summary_hash = hashlib.sha1(
                f"{event['title']}\n{event['link']}\n{event['published_at'].isoformat()}".encode("utf-8")
            ).hexdigest()
            existing = (
                s.query(Item)
                .filter(Item.feed_id == f.id, Item.external_id == event["external_id"])
                .first()
            )
            # Some ICS providers mutate UID between polls for the same event.
            # Fall back to a stable event fingerprint to avoid duplicate items.
            if not existing and feed_type == "event_ics":
                existing = (
                    s.query(Item)
                    .filter(Item.feed_id == f.id, Item.summary_hash == event_summary_hash)
                    .first()
                )
            if existing:
                existing.title = event["title"]
                existing.link = event["link"]
                existing.published_at = event["published_at"]
                existing.summary_hash = event_summary_hash
                continue
            it = Item(
                feed_id=f.id,
                external_id=event["external_id"],
                title=event["title"],
                link=event["link"],
                published_at=event["published_at"],
                categories=["event_start"],
                summary_hash=event_summary_hash,
            )
            s.add(it)
            s.flush()
            created_ids.append(it.id)
    return created_ids


async def fetch_and_store_feed(feed_id: int) -> List[int]:
    """Fetches a feed by id, stores new items, returns list of new Item IDs."""
    from .db import Feed as FeedModel  # avoid circular import

    with session_scope() as s:
        feed: Optional[FeedModel] = s.get(FeedModel, feed_id)
        if not feed or not feed.enabled:
            return []

    status, etag, last_modified, content = await fetch_feed_http(feed)

    new_ids: List[int] = []
    if status == 304:
        # no changes
        with session_scope() as s:
            f = s.get(Feed, feed_id)
            f.http_etag = etag or f.http_etag
            f.http_last_modified = last_modified or f.http_last_modified
            f.last_poll_at = datetime.now(timezone.utc)
        return []

    if status != 200 or not content:
        with session_scope() as s:
            f = s.get(Feed, feed_id)
            f.last_poll_at = datetime.now(timezone.utc)
        return []

    parsed = feedparser.parse(content)
    entries = parsed.get("entries", [])

    with session_scope() as s:
        f = s.get(Feed, feed_id)
        f.http_etag = etag or f.http_etag
        f.http_last_modified = last_modified or f.http_last_modified
        f.last_poll_at = datetime.now(timezone.utc)
        # Update feed name from parsed metadata if available
        try:
            feed_meta = parsed.get("feed")
            feed_title = feed_meta.get("title") if isinstance(feed_meta, dict) else None
            if feed_title:
                f.name = feed_title
        except Exception:
            pass

        for e in entries:
            vid = _extract_video_id(e) or (e.get("id") or "").strip()
            if not vid:
                continue
            exists = (
                s.query(Item.id).filter(Item.feed_id == f.id, Item.external_id == vid).first()
            )
            if exists:
                continue
            it = Item(
                feed_id=f.id,
                external_id=vid,
                title=e.get("title"),
                link=e.get("link"),
                author=(e.get("author") or (e.get("author_detail") or {}).get("name")),
                published_at=_published_at(e),
                categories=[t.get("term") if isinstance(t, dict) else t for t in (e.get("tags") or [])],
                summary_hash=_summary_hash(e),
            )
            s.add(it)
            s.flush()
            new_ids.append(it.id)

    return new_ids


async def fetch_and_store_latest_item(feed_id: int) -> Optional[int]:
    """Fetch the feed once and store only the latest item. Updates ETag/Last-Modified.

    Returns created Item ID or None if nothing stored.
    """
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or not feed.enabled:
            return None

    status, etag, last_modified, content = await fetch_feed_http(feed)
    if status == 304 or not content:
        with session_scope() as s:
            f = s.get(Feed, feed_id)
            f.http_etag = etag or f.http_etag
            f.http_last_modified = last_modified or f.http_last_modified
            f.last_poll_at = datetime.now(timezone.utc)
        return None

    parsed = feedparser.parse(content)
    entries = parsed.get("entries", [])
    if not entries:
        with session_scope() as s:
            f = s.get(Feed, feed_id)
            f.http_etag = etag or f.http_etag
            f.http_last_modified = last_modified or f.http_last_modified
            f.last_poll_at = datetime.now(timezone.utc)
        return None

    # Select entry with max published/updated time; fallback to first
    def entry_dt(e: feedparser.FeedParserDict) -> datetime:
        dt = _published_at(e)
        return dt or datetime.fromtimestamp(0, tz=timezone.utc)

    latest = max(entries, key=entry_dt)
    vid = _extract_video_id(latest) or (latest.get("id") or "").strip()
    if not vid:
        # can't identify id; still update headers
        with session_scope() as s:
            f = s.get(Feed, feed_id)
            f.http_etag = etag or f.http_etag
            f.http_last_modified = last_modified or f.http_last_modified
            f.last_poll_at = datetime.now(timezone.utc)
        return None

    with session_scope() as s:
        f = s.get(Feed, feed_id)
        f.http_etag = etag or f.http_etag
        f.http_last_modified = last_modified or f.http_last_modified
        f.last_poll_at = datetime.now(timezone.utc)
        try:
            feed_meta = parsed.get("feed")
            feed_title = feed_meta.get("title") if isinstance(feed_meta, dict) else None
            if feed_title:
                f.name = feed_title
        except Exception:
            pass

        exists = (
            s.query(Item.id).filter(Item.feed_id == f.id, Item.external_id == vid).first()
        )
        if exists:
            return None
        it = Item(
            feed_id=f.id,
            external_id=vid,
            title=latest.get("title"),
            link=latest.get("link"),
            author=(latest.get("author") or (latest.get("author_detail") or {}).get("name")),
            published_at=_published_at(latest),
            categories=[
                t.get("term") if isinstance(t, dict) else t for t in (latest.get("tags") or [])
            ],
            summary_hash=_summary_hash(latest),
        )
        s.add(it)
        s.flush()
        return it.id


async def fetch_and_store_recent(feed_id: int, limit: int) -> List[int]:
    """Fetch feed and store up to `limit` newest unseen entries. Returns created item IDs.

    This updates ETag/Last-Modified and last_poll_at like the normal fetch.
    """
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or not feed.enabled:
            return []

    status, etag, last_modified, content = await fetch_feed_http(feed)
    new_ids: List[int] = []
    # Even on 304 update timestamps
    with session_scope() as s:
        f = s.get(Feed, feed_id)
        f.http_etag = etag or f.http_etag
        f.http_last_modified = last_modified or f.http_last_modified
        f.last_poll_at = datetime.now(timezone.utc)
    if status == 304 or not content:
        return []

    parsed = feedparser.parse(content)
    entries = parsed.get("entries", [])

    # Order entries by published desc (fallback to input order)
    def entry_dt(e: feedparser.FeedParserDict) -> datetime:
        dt = _published_at(e)
        return dt or datetime.fromtimestamp(0, tz=timezone.utc)

    try:
        entries_sorted = sorted(entries, key=entry_dt, reverse=True)
    except Exception:
        entries_sorted = entries

    count = 0
    with session_scope() as s:
        f = s.get(Feed, feed_id)
        for e in entries_sorted:
            if count >= max(0, limit):
                break
            vid = _extract_video_id(e) or (e.get("id") or "").strip()
            if not vid:
                continue
            exists = (
                s.query(Item.id).filter(Item.feed_id == f.id, Item.external_id == vid).first()
            )
            if exists:
                continue
            it = Item(
                feed_id=f.id,
                external_id=vid,
                title=e.get("title"),
                link=e.get("link"),
                author=(e.get("author") or (e.get("author_detail") or {}).get("name")),
                published_at=_published_at(e),
                categories=[t.get("term") if isinstance(t, dict) else t for t in (e.get("tags") or [])],
                summary_hash=_summary_hash(e),
            )
            s.add(it)
            s.flush()
            new_ids.append(it.id)
            count += 1

    return new_ids


def compute_available_at(title: str, published_at: Optional[datetime]) -> Optional[datetime]:
    """Infer when the video should be considered available for delivery.

    Heuristics:
    - If title contains a date like DD.MM[.YYYY] with optional time HH:MM, use that as
      the availability moment in the configured TZ (Settings.TZ). If year is omitted,
      assume current year.
    - Otherwise, fall back to RSS `published_at`.
    """
    settings = Settings()
    tz = ZoneInfo(settings.TZ or "UTC")
    now = datetime.now(tz)
    published_utc: Optional[datetime] = None
    if published_at is not None:
        if published_at.tzinfo is None:
            published_utc = published_at.replace(tzinfo=timezone.utc)
        else:
            published_utc = published_at.astimezone(timezone.utc)

    m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?:\D{1,3}(\d{1,2}):(\d{2}))?", title)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year_str = m.group(3)
        hour_str = m.group(4)
        minute_str = m.group(5)
        year = now.year if not year_str else (2000 + int(year_str) if len(year_str) == 2 else int(year_str))
        hour = int(hour_str) if hour_str is not None else 0
        minute = int(minute_str) if minute_str is not None else 0
        try:
            dt_local = datetime(year, month, day, hour, minute, tzinfo=tz)
            dt_utc = dt_local.astimezone(timezone.utc)
            # If computed availability is earlier than published_at, keep published_at
            if published_utc and dt_utc <= published_utc:
                return published_utc
            return dt_utc
        except Exception:
            pass
    return published_utc
