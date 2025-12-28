from __future__ import annotations
import json
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
        # Need to fetch page to get channel_id
        try:
            channel_url = f"https://www.youtube.com/@{handle}"
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(channel_url) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        # Look for channel_id in various places in HTML
                        # Pattern 1: "channelId":"UC..."
                        match = re.search(r'"channelId"\s*:\s*"([^"]+)"', html)
                        if match:
                            return match.group(1)
                        # Pattern 2: <link rel="canonical" href="https://www.youtube.com/channel/UC...">
                        match = re.search(r'youtube\.com/channel/([a-zA-Z0-9_-]+)"', html)
                        if match:
                            return match.group(1)
                        # Pattern 3: /channel/UC... in various meta tags
                        match = re.search(r'/channel/([a-zA-Z0-9_-]{24})', html)
                        if match:
                            return match.group(1)
        except Exception:
            pass
    
    # Handle /c/ format: /c/CHANNEL_NAME
    match = re.search(r"youtube\.com/c/([a-zA-Z0-9_-]+)", url_clean)
    if match:
        channel_name = match.group(1)
        try:
            channel_url = f"https://www.youtube.com/c/{channel_name}"
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(channel_url) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        # Look for channel_id in HTML
                        match = re.search(r'"channelId"\s*:\s*"([^"]+)"', html)
                        if match:
                            return match.group(1)
                        match = re.search(r'youtube\.com/channel/([a-zA-Z0-9_-]+)"', html)
                        if match:
                            return match.group(1)
                        match = re.search(r'/channel/([a-zA-Z0-9_-]{24})', html)
                        if match:
                            return match.group(1)
        except Exception:
            pass
    
    # Handle /user/ format: /user/USERNAME
    match = re.search(r"youtube\.com/user/([a-zA-Z0-9_-]+)", url_clean)
    if match:
        username = match.group(1)
        try:
            channel_url = f"https://www.youtube.com/user/{username}"
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(channel_url) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        # Look for channel_id in HTML
                        match = re.search(r'"channelId"\s*:\s*"([^"]+)"', html)
                        if match:
                            return match.group(1)
                        match = re.search(r'youtube\.com/channel/([a-zA-Z0-9_-]+)"', html)
                        if match:
                            return match.group(1)
                        match = re.search(r'/channel/([a-zA-Z0-9_-]{24})', html)
                        if match:
                            return match.group(1)
        except Exception:
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
    channel_id = await _extract_youtube_channel_id(youtube_url)
    
    if not channel_id:
        await message.answer(
            f"Не удалось определить channel_id из ссылки: {youtube_url}\n"
            "Убедитесь, что ссылка корректна, или используйте /channel с прямым channel_id."
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
