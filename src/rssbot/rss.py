from __future__ import annotations

import asyncio
import hashlib
import calendar
from datetime import datetime, timezone
from typing import List, Optional, Tuple
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
            if published_at and dt_utc <= published_at:
                return published_at
            return dt_utc
        except Exception:
            pass
    return published_at
