from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from .config import Settings
from .db import Delivery, Feed, FeedBaseline, FeedRule, Item, Session, User, session_scope
from .scheduler import BotScheduler
from .rss import fetch_and_store_latest_item


router = Router()


@dataclass
class BotDeps:
    settings: Settings
    scheduler: BotScheduler


DEPS: Optional[BotDeps] = None


def set_deps(settings: Settings, scheduler: BotScheduler) -> None:
    global DEPS
    DEPS = BotDeps(settings=settings, scheduler=scheduler)


def _is_allowed(chat_id: int) -> bool:
    assert DEPS is not None
    allowed = DEPS.settings.allowed_chat_ids()
    return True if not allowed else chat_id in allowed


def _ensure_user_id(message: Message) -> Optional[int]:
    if not _is_allowed(message.chat.id):
        return None
    with session_scope() as s:
        user = s.query(User).filter(User.chat_id == message.chat.id).first()
        if user:
            return user.id
        # Auto-register on first interaction
        user = User(chat_id=message.chat.id, tz=DEPS.settings.TZ)
        s.add(user)
        s.flush()
        return user.id


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _is_allowed(message.chat.id):
        await message.answer("Доступ запрещен.")
        return
    with session_scope() as s:
        user = s.query(User).filter(User.chat_id == message.chat.id).first()
        if not user:
            user = User(chat_id=message.chat.id, tz=DEPS.settings.TZ)
            s.add(user)
            s.flush()
    web_link = f"http://{DEPS.settings.WEB_HOST}:{DEPS.settings.WEB_PORT}/u/{message.chat.id}"
    await message.answer(
        "Привет! Настраивать ленты теперь удобнее в веб-интерфейсе.\n"
        f"Открой: {web_link}\n\n"
        "Команды: /addfeed, /youtube, /channel, /playlist, /list, /remove, /setmode, /setfilter, /digest, /mute, /unmute"
    )


# NOTE: Bot trimmed to minimal commands per request


async def _create_feed_and_seed_reply(
    message: Message,
    user_id: int,
    url: str,
    mode: str,
    label: Optional[str],
    interval: int,
    digest_time: Optional[str],
) -> None:
    # Remove duplicates (same URL) for this user before adding/reusing
    removed = _dedupe_user_feeds(user_id)

    with session_scope() as s:
        existing = (
            s.query(Feed)
            .filter(Feed.user_id == user_id, Feed.url == url)
            .order_by(Feed.id.asc())
            .first()
        )
        if existing:
            existing.enabled = True
            existing.mode = mode
            existing.label = label
            existing.poll_interval_min = interval
            existing.digest_time_local = digest_time if mode == "digest" else None
            s.flush()
            feed_id = existing.id
            already_exists = True
        else:
            feed = Feed(
                user_id=user_id,
                url=url,
                type="youtube",
                label=label,
                mode=mode,
                poll_interval_min=interval,
                digest_time_local=digest_time if mode == "digest" else None,
                enabled=True,
            )
            s.add(feed)
            s.flush()
            feed_id = feed.id
            already_exists = False

    DEPS.scheduler.schedule_feed_poll(feed_id, interval)

    notify = ""
    try:
        latest_item_id = await fetch_and_store_latest_item(feed_id)
        # Set baseline on first setup if absent
        with session_scope() as s:
            if s.get(FeedBaseline, feed_id) is None:
                if latest_item_id:
                    it = s.get(Item, latest_item_id)
                    s.add(
                        FeedBaseline(
                            feed_id=feed_id,
                            baseline_item_external_id=it.external_id,
                            baseline_published_at=it.published_at,
                        )
                    )
                else:
                    s.add(FeedBaseline(feed_id=feed_id))
        if latest_item_id:
            delivered, reason = await DEPS.scheduler._send_item_once_ignore_mode(latest_item_id)
            if delivered:
                notify = " Последняя запись отправлена."
            else:
                notify = f" Последняя запись не отправлена: {reason}."
        else:
            notify = " Последняя запись не найдена в RSS."
    except Exception as e:
        notify = f" Не удалось получить последнюю запись: {str(e)[:120]}"

    msg = (
        f"Лента {'уже существовала' if already_exists else 'добавлена'} (id={feed_id}), режим: {mode}.{notify}"
    )
    if removed:
        msg += f" Удалено дублей: {removed}."
    await message.answer(msg)


def _dedupe_user_feeds(user_id: int) -> int:
    """Remove duplicate feeds (same URL) for a user.

    Keeps the oldest feed, reassigns items and deliveries, merges rules when possible,
    and unschedules duplicates. Returns number of removed duplicate feeds.
    """
    removed_ids: list[int] = []
    with session_scope() as s:
        feeds = s.query(Feed).filter(Feed.user_id == user_id).order_by(Feed.id.asc()).all()
        by_url: dict[str, list[Feed]] = {}
        for f in feeds:
            by_url.setdefault(f.url, []).append(f)

        for url, same in by_url.items():
            if len(same) <= 1:
                continue
            # Prefer an enabled feed; if multiple, prefer the newest id; else newest id overall
            keep = sorted(same, key=lambda x: (not x.enabled, -x.id))[0]
            existing_ext = {
                ext for (ext,) in s.query(Item.external_id).filter(Item.feed_id == keep.id).all()
            }
            for dup in same[1:]:
                if dup.id == keep.id:
                    continue
                # Move/dedup items
                for it in s.query(Item).filter(Item.feed_id == dup.id).all():
                    if it.external_id in existing_ext:
                        s.query(Delivery).filter(Delivery.item_id == it.id).delete(
                            synchronize_session=False
                        )
                        s.delete(it)
                    else:
                        it.feed_id = keep.id
                        existing_ext.add(it.external_id)
                # Reassign deliveries to kept feed
                s.query(Delivery).filter(Delivery.feed_id == dup.id).update(
                    {Delivery.feed_id: keep.id}, synchronize_session=False
                )
                # Merge or drop rules
                if keep.rules is None and dup.rules is not None:
                    dup.rules.feed_id = keep.id
                elif dup.rules is not None:
                    s.delete(dup.rules)
                removed_ids.append(dup.id)
                s.delete(dup)

    # Unschedule removed duplicates
    for fid in removed_ids:
        try:
            DEPS.scheduler.unschedule_feed_poll(fid)
        except Exception:
            pass
    return len(removed_ids)


async def _extract_youtube_channel_id(url: str) -> Optional[str]:
    """Extract YouTube channel_id from various URL formats.
    
    Supports:
    - https://www.youtube.com/channel/CHANNEL_ID
    - https://youtube.com/channel/CHANNEL_ID
    - https://www.youtube.com/c/CHANNEL_NAME
    - https://www.youtube.com/@CHANNEL_HANDLE
    - https://www.youtube.com/user/USERNAME
    
    Returns channel_id or None if extraction fails.
    """
    # Normalize URL
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    
    # Remove query parameters and fragments for parsing
    url_clean = url.split("?")[0].split("#")[0]
    
    # Direct channel_id format: /channel/CHANNEL_ID
    match = re.search(r"youtube\.com/channel/([a-zA-Z0-9_-]+)", url_clean)
    if match:
        return match.group(1)
    
    # Handle @handle format: /@CHANNEL_HANDLE
    match = re.search(r"youtube\.com/@([a-zA-Z0-9_-]+)", url_clean)
    if match:
        handle = match.group(1)
        logging.info(f"Attempting to extract channel_id for handle: @{handle}")
        
        # Method 0: Try YouTube oEmbed API (might work for some channels)
        try:
            oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/@{handle}&format=json"
            logging.info(f"Trying oEmbed API: {oembed_url}")
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(oembed_url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # oEmbed doesn't directly give channel_id, but we can try to extract from author_url
                        author_url = data.get('author_url', '')
                        if author_url:
                            channel_match = re.search(r'/channel/([a-zA-Z0-9_-]+)', author_url)
                            if channel_match:
                                channel_id = channel_match.group(1)
                                if channel_id.startswith("UC") and len(channel_id) == 24:
                                    logging.info(f"✓ Found channel_id via oEmbed: {channel_id}")
                                    return channel_id
        except Exception as e:
            logging.info(f"oEmbed method failed: {type(e).__name__}: {str(e)[:100]}")
        
        # Method 1: Try RSS feed - different formats
        # Note: @handle format might not work directly, but we try anyway
        rss_formats = [
            f"https://www.youtube.com/feeds/videos.xml?user=@{handle}",
            # Alternative: try without @ symbol (sometimes works)
            f"https://www.youtube.com/feeds/videos.xml?user={handle}",
        ]
        for rss_url in rss_formats:
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(rss_url, allow_redirects=True) as resp:
                        logging.info(f"RSS feed attempt: {rss_url} -> status {resp.status}, final URL: {resp.url}")
                        final_url = str(resp.url)
                        # Check if redirected to channel_id format
                        url_match = re.search(r'channel_id=([a-zA-Z0-9_-]+)', final_url)
                        if url_match:
                            channel_id = url_match.group(1)
                            if channel_id.startswith("UC") and len(channel_id) == 24:
                                logging.info(f"✓ Found channel_id via RSS redirect: {channel_id}")
                                return channel_id
                        if resp.status == 200:
                            content = await resp.text()
                            logging.info(f"RSS content length: {len(content)}")
                            # Check content for channel_id in links
                            content_match = re.search(r'channel_id=([a-zA-Z0-9_-]+)', content)
                            if content_match:
                                channel_id = content_match.group(1)
                                if channel_id.startswith("UC") and len(channel_id) == 24:
                                    logging.info(f"✓ Found channel_id in RSS content: {channel_id}")
                                    return channel_id
                            # Also try to find in atom:link
                            link_match = re.search(r'<link[^>]*href="[^"]*channel_id=([a-zA-Z0-9_-]+)"', content)
                            if link_match:
                                channel_id = link_match.group(1)
                                if channel_id.startswith("UC") and len(channel_id) == 24:
                                    logging.info(f"✓ Found channel_id in RSS link: {channel_id}")
                                    return channel_id
                        else:
                            logging.info(f"RSS feed returned status {resp.status}, not 200")
            except Exception as e:
                logging.info(f"✗ RSS feed method failed for {rss_url}: {type(e).__name__}: {str(e)[:150]}")
                continue
        
        # Method 2: Try to access channel page and check redirect
        try:
            channel_url = f"https://www.youtube.com/@{handle}"
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(channel_url, allow_redirects=True) as resp:
                    final_url = str(resp.url)
                    logging.info(f"Channel page redirect: {channel_url} -> {final_url}")
                    # Check if redirected to /channel/ID format
                    channel_match = re.search(r'youtube\.com/channel/([a-zA-Z0-9_-]+)', final_url)
                    if channel_match:
                        channel_id = channel_match.group(1)
                        if channel_id.startswith("UC") and len(channel_id) == 24:
                            logging.info(f"Found channel_id via redirect: {channel_id}")
                            return channel_id
        except Exception as e:
            logging.info(f"Redirect method failed: {type(e).__name__}: {str(e)[:100]}")
            pass
        
        # Method 3: Parse HTML from channel page
        try:
            channel_url = f"https://www.youtube.com/@{handle}"
            timeout = aiohttp.ClientTimeout(total=20)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(channel_url, allow_redirects=True) as resp:
                    logging.info(f"HTML fetch: {channel_url} -> status {resp.status}")
                    if resp.status == 200:
                        html = await resp.text()
                        logging.info(f"HTML length: {len(html)}")
                        # Check if we got consent page instead of actual page
                        if 'consent.youtube.com' in html or 'ConsentUi' in html:
                            logging.info("Got YouTube consent page, trying to bypass...")
                            # Try with cookies or different approach
                            # For now, try to extract from consent page redirect or use alternative method
                            # Try RSS feed method which might work better
                            pass
                        # Log first 500 chars to see what we got
                        if len(html) > 0 and 'consent.youtube.com' not in html:
                            logging.info(f"HTML preview (first 500 chars): {html[:500]}")
                        # Check if we got consent page - try to extract channel_id from redirect URL in consent page
                        if 'consent.youtube.com' in html:
                            logging.info("Detected consent page, trying to extract channel_id from redirect URLs...")
                            # Consent page might have redirect URL with channel_id - try multiple patterns
                            patterns = [
                                r'"(https?://[^"]*youtube\.com/[^"]*channel[^"]*)"',
                                r'continue=([^"&]*youtube\.com[^"&]*channel[^"&]*)',
                                r'url=([^"&]*youtube\.com[^"&]*channel[^"&]*)',
                                r'/channel/([a-zA-Z0-9_-]{24})',
                            ]
                            for pattern in patterns:
                                matches = re.finditer(pattern, html)
                                for match in matches:
                                    url_or_id = match.group(1) if match.lastindex else match.group(0)
                                    # Check if it's a full URL or just channel_id
                                    if url_or_id.startswith('http'):
                                        channel_match = re.search(r'/channel/([a-zA-Z0-9_-]+)', url_or_id)
                                        if channel_match:
                                            channel_id = channel_match.group(1)
                                            if channel_id.startswith("UC") and len(channel_id) == 24:
                                                logging.info(f"✓ Found channel_id in consent page redirect: {channel_id}")
                                                return channel_id
                                    elif len(url_or_id) == 24 and url_or_id.startswith("UC"):
                                        logging.info(f"✓ Found channel_id directly in consent page: {url_or_id}")
                                        return url_or_id
                            
                            # Also try to find any UC* pattern in the HTML (more aggressive)
                            all_uc_ids = re.findall(r'\b(UC[a-zA-Z0-9_-]{22})\b', html)
                            for potential_id in set(all_uc_ids):
                                if len(potential_id) == 24:
                                    # Check context around this ID
                                    idx = html.find(potential_id)
                                    context = html[max(0, idx-100):min(len(html), idx+124)].lower()
                                    if any(kw in context for kw in ['channel', 'youtube', 'redirect', 'continue']):
                                        logging.info(f"✓ Found channel_id in consent page context: {potential_id}")
                                        return potential_id
                        
                        # Pattern 1: "channelId":"UC..." (most common in JSON)
                        match = re.search(r'"channelId"\s*:\s*"([^"]+)"', html)
                        if match:
                            channel_id = match.group(1)
                            # Validate it looks like a channel ID (starts with UC and is 24 chars)
                            if channel_id.startswith("UC") and len(channel_id) == 24:
                                logging.info(f"✓ Found channel_id via Pattern 1: {channel_id}")
                                return channel_id
                        
                        # Pattern 2: "externalId":"UC..." (alternative JSON field)
                        match = re.search(r'"externalId"\s*:\s*"([^"]+)"', html)
                        if match:
                            channel_id = match.group(1)
                            if channel_id.startswith("UC") and len(channel_id) == 24:
                                return channel_id
                        
                        # Pattern 3: <link rel="canonical" href="https://www.youtube.com/channel/UC...">
                        match = re.search(r'youtube\.com/channel/([a-zA-Z0-9_-]{24})', html)
                        if match:
                            channel_id = match.group(1)
                            if channel_id.startswith("UC"):
                                return channel_id
                        
                        # Pattern 4: /channel/UC... in various places (more flexible)
                        match = re.search(r'/channel/(UC[a-zA-Z0-9_-]{22})', html)
                        if match:
                            return match.group(1)
                        
                        # Pattern 5: Search in JSON-LD structured data
                        match = re.search(r'"@type"\s*:\s*"Person"[^}]*"identifier"\s*:\s*"([^"]+)"', html)
                        if match:
                            identifier = match.group(1)
                            if identifier.startswith("UC") and len(identifier) == 24:
                                return identifier
                        
                        # Pattern 6: Look for var ytInitialData or window["ytInitialData"] (improved)
                        # Try multiple patterns for ytInitialData
                        patterns = [
                            r'var\s+ytInitialData\s*=\s*({.+?});',
                            r'window\[["\']ytInitialData["\']]\s*=\s*({.+?});',
                            r'ytInitialData\s*=\s*({.+?});\s*</script>',
                        ]
                        for pattern in patterns:
                            match = re.search(pattern, html, re.DOTALL)
                            if match:
                                json_str = match.group(1)
                                # Try multiple nested paths in the JSON
                                channel_patterns = [
                                    r'"channelId"\s*:\s*"([^"]+)"',
                                    r'"externalId"\s*:\s*"([^"]+)"',
                                    r'"browseId"\s*:\s*"([^"]+)"',  # Sometimes channelId is here
                                ]
                                for cp in channel_patterns:
                                    channel_match = re.search(cp, json_str)
                                    if channel_match:
                                        channel_id = channel_match.group(1)
                                        if channel_id.startswith("UC") and len(channel_id) == 24:
                                            return channel_id
                        
                        # Pattern 7: Try to find in meta tags
                        match = re.search(r'<meta\s+property="og:url"\s+content="https://www\.youtube\.com/channel/([^"]+)"', html)
                        if match:
                            channel_id = match.group(1)
                            if channel_id.startswith("UC") and len(channel_id) == 24:
                                return channel_id
                        
                        # Pattern 8: Search for any UC followed by 22 alphanumeric chars (standard channel ID format)
                        matches = re.findall(r'(UC[a-zA-Z0-9_-]{22})', html)
                        for potential_id in matches:
                            # Validate it's likely a channel ID (not part of another string)
                            if len(potential_id) == 24:
                                # Check if it's in a context that suggests it's a channel ID
                                idx = html.find(potential_id)
                                context_before = html[max(0, idx-50):idx]
                                context_after = html[idx:min(len(html), idx+74)]
                                if any(keyword in context_before.lower() or keyword in context_after.lower() 
                                       for keyword in ['channel', 'channelid', 'externalid', 'browseid']):
                                    return potential_id
                        
                        # Pattern 9: More aggressive search - find all UC* patterns and check context
                        # Look for patterns like "UC..." in various JSON structures
                        all_uc_matches = re.finditer(r'["\']?([UC][a-zA-Z0-9_-]{23})["\']?', html)
                        for match_obj in all_uc_matches:
                            potential_id = match_obj.group(1)
                            if potential_id.startswith("UC") and len(potential_id) == 24:
                                # Get wider context
                                start = max(0, match_obj.start() - 100)
                                end = min(len(html), match_obj.end() + 100)
                                context = html[start:end].lower()
                                # Check for channel-related keywords
                                if any(kw in context for kw in ['channel', 'id', 'browse', 'external', 'canonical', 'url']):
                                    return potential_id
                        
                        # Pattern 10: Try to find in ytcfg (YouTube config object)
                        match = re.search(r'ytcfg\.set\(({[^}]+})', html, re.DOTALL)
                        if match:
                            config_str = match.group(1)
                            channel_match = re.search(r'"CHANNEL_ID"\s*:\s*"([^"]+)"', config_str)
                            if channel_match:
                                channel_id = channel_match.group(1)
                                if channel_id.startswith("UC") and len(channel_id) == 24:
                                    return channel_id
                        
                        # Pattern 11: Look in window.ytInitialPlayerResponse
                        match = re.search(r'window\.ytInitialPlayerResponse\s*=\s*({.+?});', html, re.DOTALL)
                        if match:
                            player_data = match.group(1)
                            channel_match = re.search(r'"channelId"\s*:\s*"([^"]+)"', player_data)
                            if channel_match:
                                channel_id = channel_match.group(1)
                                if channel_id.startswith("UC") and len(channel_id) == 24:
                                    return channel_id
                        
                        # Pattern 12: Last resort - find any valid-looking UC* ID near channel-related text
                        # This is more permissive but should catch edge cases
                        channel_id_pattern = r'\b(UC[a-zA-Z0-9_-]{22})\b'
                        all_ids = re.findall(channel_id_pattern, html)
                        # Filter by context - look for IDs that appear near channel-related content
                        for cid in set(all_ids):  # Use set to avoid duplicates
                            # Count occurrences and check if it appears in channel-related contexts
                            occurrences = list(re.finditer(re.escape(cid), html))
                            for occ in occurrences[:5]:  # Check first 5 occurrences
                                start = max(0, occ.start() - 200)
                                end = min(len(html), occ.end() + 200)
                                context = html[start:end].lower()
                                # If it appears near channel-related terms, it's likely the right one
                                if any(term in context for term in ['channel', 'youtube.com/channel', 'channelid', 'browseid']):
                                    return cid
        except Exception as e:
            # Log error for debugging but don't fail silently
            logging.warning(f"Failed to extract channel_id from @handle {handle}: {type(e).__name__}: {e}", exc_info=True)
            pass
        
        logging.warning(f"All methods failed to extract channel_id for @{handle}")
    
    # Handle /c/ format: /c/CHANNEL_NAME
    match = re.search(r"youtube\.com/c/([a-zA-Z0-9_-]+)", url_clean)
    if match:
        channel_name = match.group(1)
        try:
            channel_url = f"https://www.youtube.com/c/{channel_name}"
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(channel_url) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        # Pattern 1: "channelId":"UC..."
                        match = re.search(r'"channelId"\s*:\s*"([^"]+)"', html)
                        if match:
                            channel_id = match.group(1)
                            if channel_id.startswith("UC") and len(channel_id) == 24:
                                return channel_id
                        # Pattern 2: "externalId":"UC..."
                        match = re.search(r'"externalId"\s*:\s*"([^"]+)"', html)
                        if match:
                            channel_id = match.group(1)
                            if channel_id.startswith("UC") and len(channel_id) == 24:
                                return channel_id
                        # Pattern 3: canonical link
                        match = re.search(r'youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})', html)
                        if match:
                            return match.group(1)
                        # Pattern 4: /channel/UC...
                        match = re.search(r'/channel/(UC[a-zA-Z0-9_-]{22})', html)
                        if match:
                            return match.group(1)
        except Exception as e:
            logging.warning(f"Failed to extract channel_id from /c/ format: {e}")
            pass
    
    # Handle /user/ format: /user/USERNAME
    match = re.search(r"youtube\.com/user/([a-zA-Z0-9_-]+)", url_clean)
    if match:
        username = match.group(1)
        try:
            channel_url = f"https://www.youtube.com/user/{username}"
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(channel_url) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        # Pattern 1: "channelId":"UC..."
                        match = re.search(r'"channelId"\s*:\s*"([^"]+)"', html)
                        if match:
                            channel_id = match.group(1)
                            if channel_id.startswith("UC") and len(channel_id) == 24:
                                return channel_id
                        # Pattern 2: "externalId":"UC..."
                        match = re.search(r'"externalId"\s*:\s*"([^"]+)"', html)
                        if match:
                            channel_id = match.group(1)
                            if channel_id.startswith("UC") and len(channel_id) == 24:
                                return channel_id
                        # Pattern 3: canonical link
                        match = re.search(r'youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})', html)
                        if match:
                            return match.group(1)
                        # Pattern 4: /channel/UC...
                        match = re.search(r'/channel/(UC[a-zA-Z0-9_-]{22})', html)
                        if match:
                            return match.group(1)
        except Exception as e:
            logging.warning(f"Failed to extract channel_id from /user/ format: {e}")
            pass
    
    return None


@router.message(Command("youtube"))
async def cmd_youtube(message: Message) -> None:
    """Добавить ленту YouTube по ссылке на канал.
    
    Автоматически определяет channel_id из различных форматов ссылок:
    - https://www.youtube.com/channel/CHANNEL_ID
    - https://www.youtube.com/@CHANNEL_HANDLE
    - https://www.youtube.com/c/CHANNEL_NAME
    - https://www.youtube.com/user/USERNAME
    
    Формат: /youtube <youtube_link> [mode=...] [label=...] [interval=10] [time=HH:MM]
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: /youtube <youtube_link> [mode=immediate|digest|on_demand] [label=...] [interval=10] [time=HH:MM]\n\n"
            "Поддерживаемые форматы ссылок:\n"
            "- https://www.youtube.com/channel/CHANNEL_ID\n"
            "- https://www.youtube.com/@CHANNEL_HANDLE\n"
            "- https://www.youtube.com/c/CHANNEL_NAME\n"
            "- https://www.youtube.com/user/USERNAME"
        )
        return
    
    # Extract URL and parse additional arguments
    rest = parts[1]
    # Split by spaces, URL is first part (may contain query params)
    url_parts = rest.split()
    youtube_url = url_parts[0]
    
    # Parse additional arguments
    mode = "immediate"
    label = None
    interval = DEPS.settings.DEFAULT_POLL_INTERVAL_MIN
    digest_time = DEPS.settings.DIGEST_DEFAULT_TIME
    for a in url_parts[1:]:
        aval = a.strip().lower()
        if aval in ("immediate", "digest", "on_demand"):
            mode = aval
        elif a.startswith("mode="):
            mode = a.split("=", 1)[1]
        elif a.startswith("label="):
            label = a.split("=", 1)[1]
        elif a.startswith("interval="):
            try:
                interval = int(a.split("=", 1)[1])
            except Exception:
                pass
        elif a.startswith("time="):
            digest_time = a.split("=", 1)[1]
    
    # Extract channel_id
    await message.answer("Определяю channel_id...")
    try:
        channel_id = await _extract_youtube_channel_id(youtube_url)
    except Exception as e:
        logging.error(f"Error extracting channel_id from {youtube_url}: {e}", exc_info=True)
        await message.answer(
            f"Ошибка при определении channel_id: {str(e)[:100]}\n"
            "Попробуйте использовать /channel с прямым channel_id."
        )
        return
    
    if not channel_id:
        logging.warning(f"Could not extract channel_id from URL: {youtube_url}")
        await message.answer(
            f"Не удалось определить channel_id из ссылки: {youtube_url}\n"
            "Убедитесь, что ссылка корректна, или используйте /channel с прямым channel_id.\n"
            "Проверьте логи сервера для деталей."
        )
        return
    
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    await _create_feed_and_seed_reply(message, user_id, url, mode, label, interval, digest_time)


@router.message(Command("channel"))
async def cmd_channel(message: Message) -> None:
    """Добавить ленту YouTube по channel_id.

    Формат: /channel <channel_id> [mode=...] [label=...] [interval=10] [time=HH:MM]
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Использование: /channel <channel_id> [mode=immediate|digest|on_demand] [label=...] [interval=10] [time=HH:MM]"
        )
        return
    channel_id = parts[1]
    mode = "immediate"
    label = None
    interval = DEPS.settings.DEFAULT_POLL_INTERVAL_MIN
    digest_time = DEPS.settings.DIGEST_DEFAULT_TIME
    for a in parts[2:]:
        aval = a.strip().lower()
        if aval in ("immediate", "digest", "on_demand"):
            mode = aval
        elif a.startswith("mode="):
            mode = a.split("=", 1)[1]
        elif a.startswith("label="):
            label = a.split("=", 1)[1]
        elif a.startswith("interval="):
            try:
                interval = int(a.split("=", 1)[1])
            except Exception:
                pass
        elif a.startswith("time="):
            digest_time = a.split("=", 1)[1]
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    await _create_feed_and_seed_reply(message, user_id, url, mode, label, interval, digest_time)


@router.message(Command("playlist"))
async def cmd_playlist(message: Message) -> None:
    """Добавить ленту YouTube по playlist_id.

    Формат: /playlist <playlist_id> [mode=...] [label=...] [interval=10] [time=HH:MM]
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Использование: /playlist <playlist_id> [mode=immediate|digest|on_demand] [label=...] [interval=10] [time=HH:MM]"
        )
        return
    playlist_id = parts[1]
    mode = "immediate"
    label = None
    interval = DEPS.settings.DEFAULT_POLL_INTERVAL_MIN
    digest_time = DEPS.settings.DIGEST_DEFAULT_TIME
    for a in parts[2:]:
        aval = a.strip().lower()
        if aval in ("immediate", "digest", "on_demand"):
            mode = aval
        elif a.startswith("mode="):
            mode = a.split("=", 1)[1]
        elif a.startswith("label="):
            label = a.split("=", 1)[1]
        elif a.startswith("interval="):
            try:
                interval = int(a.split("=", 1)[1])
            except Exception:
                pass
        elif a.startswith("time="):
            digest_time = a.split("=", 1)[1]
    url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"
    await _create_feed_and_seed_reply(message, user_id, url, mode, label, interval, digest_time)


@router.message(Command("addfeed"))
async def cmd_addfeed(message: Message) -> None:
    """Добавить ленту по URL.

    Формат: /addfeed <url> [mode=immediate|digest|on_demand] [label=...] [interval=10] [time=HH:MM]
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Использование: /addfeed <url> [mode=immediate|digest|on_demand] [label=...] [interval=10] [time=HH:MM]"
        )
        return
    url = parts[1]
    mode = "immediate"
    label = None
    interval = DEPS.settings.DEFAULT_POLL_INTERVAL_MIN
    digest_time = DEPS.settings.DIGEST_DEFAULT_TIME
    for a in parts[2:]:
        aval = a.strip().lower()
        if aval in ("immediate", "digest", "on_demand"):
            mode = aval
        elif a.startswith("mode="):
            mode = a.split("=", 1)[1]
        elif a.startswith("label="):
            label = a.split("=", 1)[1]
        elif a.startswith("interval="):
            try:
                interval = int(a.split("=", 1)[1])
            except Exception:
                pass
        elif a.startswith("time="):
            digest_time = a.split("=", 1)[1]
    await _create_feed_and_seed_reply(message, user_id, url, mode, label, interval, digest_time)


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    """Показать список лент пользователя."""
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    with session_scope() as s:
        feeds = s.query(Feed).filter(Feed.user_id == user_id).order_by(Feed.id.asc()).all()
        if not feeds:
            await message.answer("У вас нет лент. Используйте /addfeed, /channel или /playlist для добавления.")
            return
        lines = ["Ваши ленты:"]
        for f in feeds:
            status = "✓" if f.enabled else "✗"
            display_name = f.label or f.name or f.url[:50]
            time_part = f" в {f.digest_time_local}" if f.digest_time_local else ""
            lines.append(
                f"{status} {f.id}: {display_name} — {f.mode}{time_part}"
            )
        await message.answer("\n".join(lines))


@router.message(Command("remove"))
async def cmd_remove(message: Message) -> None:
    """Удалить ленту."""
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /remove <feed_id>")
        return
    try:
        feed_id = int(parts[1])
    except Exception:
        await message.answer("Неверный id.")
        return
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or feed.user_id != user_id:
            await message.answer("Лента не найдена.")
            return
        # Unschedule polling
        DEPS.scheduler.unschedule_feed_poll(feed_id)
        # Delete feed (cascade will handle related items/deliveries/rules)
        s.delete(feed)
    await message.answer(f"Лента {feed_id} удалена.")


@router.message(Command("setmode"))
async def cmd_setmode(message: Message) -> None:
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Использование: /setmode <feed_id> <mode> [time=HH:MM]")
        return
    try:
        feed_id = int(parts[1])
    except Exception:
        await message.answer("Неверный id.")
        return
    mode = parts[2]
    digest_time = None
    for p in parts[3:]:
        if p.startswith("time="):
            digest_time = p.split("=", 1)[1]
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or feed.user_id != user_id:
            await message.answer("Лента не найдена.")
            return
        feed.mode = mode
        if mode == "digest":
            feed.digest_time_local = digest_time or DEPS.settings.DIGEST_DEFAULT_TIME
        else:
            feed.digest_time_local = None
        interval = feed.poll_interval_min
    # Reschedule polling job (unchanged interval)
    DEPS.scheduler.schedule_feed_poll(feed_id, interval)
    await message.answer("Режим обновлён.")


@router.message(Command("setfilter"))
async def cmd_setfilter(message: Message) -> None:
    """Установить фильтры для ленты.

    Формат: /setfilter <feed_id> <json>
    Пример: /setfilter 1 {"include_keywords": ["обзор"], "exclude_keywords": ["стрим"]}
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            'Использование: /setfilter <feed_id> <json>\n'
            'Пример: /setfilter 1 {"include_keywords": ["обзор"], "exclude_keywords": ["стрим"]}'
        )
        return
    try:
        feed_id = int(parts[1])
    except Exception:
        await message.answer("Неверный id.")
        return
    try:
        filter_data = json.loads(parts[2])
    except json.JSONDecodeError as e:
        await message.answer(f"Неверный JSON: {str(e)}")
        return
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or feed.user_id != user_id:
            await message.answer("Лента не найдена.")
            return
        # Get or create rules
        rules = feed.rules
        if not rules:
            rules = FeedRule(feed_id=feed_id)
            s.add(rules)
        # Update rules from JSON
        if "include_keywords" in filter_data:
            rules.include_keywords = filter_data["include_keywords"]
        if "exclude_keywords" in filter_data:
            rules.exclude_keywords = filter_data["exclude_keywords"]
        if "include_regex" in filter_data:
            rules.include_regex = filter_data["include_regex"]
        if "exclude_regex" in filter_data:
            rules.exclude_regex = filter_data["exclude_regex"]
        if "require_all" in filter_data:
            rules.require_all = bool(filter_data["require_all"])
        if "case_sensitive" in filter_data:
            rules.case_sensitive = bool(filter_data["case_sensitive"])
        if "categories" in filter_data:
            rules.categories = filter_data["categories"]
        if "min_duration_sec" in filter_data:
            rules.min_duration_sec = filter_data["min_duration_sec"]
        if "max_duration_sec" in filter_data:
            rules.max_duration_sec = filter_data["max_duration_sec"]
    await message.answer("Фильтры обновлены.")


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    """Запустить дайджест вручную.

    Формат: /digest [feed_id|all]
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /digest <feed_id|all>")
        return
    arg = parts[1].lower()
    with session_scope() as s:
        if arg == "all":
            feeds = s.query(Feed).filter(Feed.user_id == user_id, Feed.enabled == True).all()
            feed_ids = [f.id for f in feeds]
        else:
            try:
                feed_id = int(arg)
            except Exception:
                await message.answer("Неверный id.")
                return
            feed = s.get(Feed, feed_id)
            if not feed or feed.user_id != user_id:
                await message.answer("Лента не найдена.")
                return
            feed_ids = [feed_id]
    if not feed_ids:
        await message.answer("Нет активных лент для дайджеста.")
        return
    # Send digest for each feed
    for fid in feed_ids:
        await DEPS.scheduler._send_digest_for_feed(fid)
    await message.answer(f"Дайджест отправлен для {len(feed_ids)} лент.")


@router.message(Command("mute"))
async def cmd_mute(message: Message) -> None:
    """Отключить ленту (временно)."""
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /mute <feed_id>")
        return
    try:
        feed_id = int(parts[1])
    except Exception:
        await message.answer("Неверный id.")
        return
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or feed.user_id != user_id:
            await message.answer("Лента не найдена.")
            return
        feed.enabled = False
        # Unschedule polling
        DEPS.scheduler.unschedule_feed_poll(feed_id)
    await message.answer(f"Лента {feed_id} отключена.")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message) -> None:
    """Включить ленту."""
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /unmute <feed_id>")
        return
    try:
        feed_id = int(parts[1])
    except Exception:
        await message.answer("Неверный id.")
        return
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or feed.user_id != user_id:
            await message.answer("Лента не найдена.")
            return
        feed.enabled = True
        # Reschedule polling
        DEPS.scheduler.schedule_feed_poll(feed_id, feed.poll_interval_min)
    await message.answer(f"Лента {feed_id} включена.")


# Removed latest inspector


# Removed backfill utility


# Removed dedupe command; duplicates are handled on add
