from __future__ import annotations
import asyncio
import csv
import hashlib
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from .config import Settings
from .db import Delivery, Feed, FeedBaseline, FeedRule, Item, Session, User, session_scope
from .scheduler import BotScheduler
from .rss import fetch_and_store_event_source, fetch_and_store_latest_item
from .ai_summarizer import (
    AiSummarizerError,
    parse_ai_request_text,
    split_message_chunks,
    summarize_video,
)
from utils.yt_channel_id import get_channel_id


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
        "Команды: /ai, /addfeed, /addeventsource, /addevents, /youtube, /channel, /playlist, "
        "/list, /remove, /setmode, /setfilter, /digest, /mute, /unmute"
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
            if mode == "digest":
                if digest_time:
                    existing.digest_time_local = digest_time
                elif not existing.digest_time_local:
                    existing.digest_time_local = DEPS.settings.DIGEST_DEFAULT_TIME
            else:
                existing.digest_time_local = None
            s.flush()
            feed_id = existing.id
            already_exists = True
        else:
            digest_time_local = None
            if mode == "digest":
                digest_time_local = digest_time or DEPS.settings.DIGEST_DEFAULT_TIME
            feed = Feed(
                user_id=user_id,
                url=url,
                type="youtube",
                label=label,
                mode=mode,
                poll_interval_min=interval,
                digest_time_local=digest_time_local,
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


async def _create_event_source_feed_reply(
    message: Message,
    user_id: int,
    url: str,
    label: Optional[str],
    interval: int,
) -> None:
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
            existing.mode = "immediate"
            existing.type = "event_json"
            existing.label = label
            existing.poll_interval_min = interval
            existing.digest_time_local = None
            s.flush()
            feed_id = existing.id
            already_exists = True
        else:
            feed = Feed(
                user_id=user_id,
                url=url,
                type="event_json",
                label=label,
                mode="immediate",
                poll_interval_min=interval,
                enabled=True,
            )
            s.add(feed)
            s.flush()
            feed_id = feed.id
            already_exists = False

    DEPS.scheduler.schedule_feed_poll(feed_id, interval)

    loaded = 0
    delivered = 0
    error = None
    try:
        created_ids = await fetch_and_store_event_source(feed_id)
        loaded = len(created_ids)
        delivered = await DEPS.scheduler._deliver_due_event_starts(feed_id)
    except Exception as e:
        error = str(e)[:160]

    msg = (
        f"Источник {'уже существовал' if already_exists else 'добавлен'} (id={feed_id}), тип: event_json."
        f" Загружено новых событий: {loaded}. Отправлено старт-уведомлений: {delivered}."
    )
    if removed:
        msg += f" Удалено дублей: {removed}."
    if error:
        msg += f" Ошибка первичного опроса: {error}."
    await message.answer(msg)


def _parse_manual_event_datetime(raw: str, tz_name: str) -> Optional[datetime]:
    value = (raw or "").strip()
    if not value:
        return None
    tz = ZoneInfo(tz_name or "UTC")

    # ISO-8601 (recommended), including trailing Z
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # Common text formats in local TZ
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            dt_local = datetime.strptime(value, fmt).replace(tzinfo=tz)
            return dt_local.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def parse_bulk_events_text(block: str, tz_name: str) -> tuple[list[dict[str, object]], list[str]]:
    """Parse text lines into events.

    Supported row formats:
      1) start_at;title;link
    Delimiter: semicolon only.
    """
    items: list[dict[str, object]] = []
    errors: list[str] = []
    for idx, raw_line in enumerate((block or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if ";" not in line:
            errors.append(f"строка {idx}: используйте ';' как разделитель")
            continue
        parts = [p.strip() for p in next(csv.reader([line], delimiter=";"))]

        parts = [p for p in parts if p != ""]
        if len(parts) != 3:
            errors.append(f"строка {idx}: ожидается 3 колонки (start_at;title;link)")
            continue
        start_raw, title, link = parts

        start_at = _parse_manual_event_datetime(start_raw, tz_name)
        if not start_at:
            errors.append(f"строка {idx}: неверная дата/время '{start_raw}'")
            continue
        if not title.strip():
            errors.append(f"строка {idx}: пустой title")
            continue
        if not link.strip():
            errors.append(f"строка {idx}: пустой link")
            continue

        seed = f"{start_at.isoformat()}\n{title.strip()}\n{link.strip()}"
        ext_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()

        items.append(
            {
                "external_id": ext_id,
                "title": title.strip(),
                "link": link.strip(),
                "published_at": start_at,
            }
        )
    return items, errors


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
    """Extract YouTube channel_id from a URL using utils.yt_channel_id."""
    try:
        return await asyncio.to_thread(get_channel_id, url)
    except Exception as exc:
        logging.warning(
            "Failed to extract channel_id from %s via utils.yt_channel_id: %s",
            url,
            exc,
            exc_info=True,
        )
        return None


@router.message(Command("ai"))
async def cmd_ai(message: Message) -> None:
    """Суммаризировать YouTube-видео через внутренний AI-инструмент.

    Формат: /ai <youtube_url_or_video_id> [что именно вас интересует]
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return

    try:
        request = parse_ai_request_text(message.text or "")
    except ValueError:
        await message.answer(
            "Использование: /ai <youtube_url_or_video_id> [дополнительный фокус]\n"
            "Пример: /ai https://www.youtube.com/watch?v=dQw4w9WgXcQ "
            "Выдели только практические выводы и риски.",
            parse_mode=None,
        )
        return

    await message.answer(
        "Запускаю суммаризацию видео. Это может занять 20-90 секунд.",
        parse_mode=None,
    )

    try:
        result = await summarize_video(
            DEPS.settings,
            chat_id=message.chat.id,
            video_url=request.video_url,
            custom_prompt=request.custom_prompt,
        )
    except AiSummarizerError as exc:
        await message.answer(f"Не удалось сделать суммаризацию: {str(exc)}", parse_mode=None)
        return
    except Exception as exc:
        logging.error("Unexpected /ai failure for chat_id=%s: %s", message.chat.id, exc, exc_info=True)
        await message.answer("Внутренняя ошибка при суммаризации.", parse_mode=None)
        return

    focus_text = ""
    if request.custom_prompt:
        focus_text = f"\nФокус: {request.custom_prompt}"

    response_text = (
        f"Суммаризация готова.\nВидео: {request.video_url}{focus_text}\n\n{result.summary_text}"
    )
    for chunk in split_message_chunks(response_text):
        await message.answer(chunk, parse_mode=None)


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
    digest_time = None
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
            digest_time = a.split("=", 1)[1] or None
    
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
    digest_time = None
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
            digest_time = a.split("=", 1)[1] or None
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
    digest_time = None
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
            digest_time = a.split("=", 1)[1] or None
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
    digest_time = None
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
            digest_time = a.split("=", 1)[1] or None
    await _create_feed_and_seed_reply(message, user_id, url, mode, label, interval, digest_time)


@router.message(Command("addeventsource"))
async def cmd_addeventsource(message: Message) -> None:
    """Добавить JSON-источник событий.

    Формат: /addeventsource <url> [label=...] [interval=1]

    Ожидаемый формат JSON:
    {"events":[{"id":"...", "title":"...", "link":"...", "start_at":"2026-02-08T19:30:00+03:00"}]}
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Использование: /addeventsource <url> [label=...] [interval=1]\n"
            "JSON должен быть массивом событий или объектом с полем events.\n"
            "Каждое событие: id (рекомендуется), title, link/url, start_at."
        )
        return
    url = parts[1]
    label = None
    interval = 1
    for a in parts[2:]:
        if a.startswith("label="):
            label = a.split("=", 1)[1]
        elif a.startswith("interval="):
            try:
                interval = max(1, int(a.split("=", 1)[1]))
            except Exception:
                pass
    await _create_event_source_feed_reply(message, user_id, url, label, interval)


@router.message(Command("addevents"))
async def cmd_addevents(message: Message) -> None:
    """Массовое добавление событий прямо из текста в Telegram.

    Формат:
      /addevents [feed=<id>|<id>] [label=...] [interval=1]
      <start_at>;<title>;<link>
      <start_at>;<title>;<link>
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return

    text = message.text or ""
    head, sep, body = text.partition("\n")
    parts = head.split()

    # If no block provided - show short help.
    if not sep or not body.strip():
        await message.answer(
            "Использование:\n"
            "/addevents [feed=<id>|<id>] [label=...] [interval=1]\\n"
            "2026-02-10T19:30:00+03:00;Женщины. Короткая программа;https://...\\n"
            "2026-02-10 21:00;Мужчины. Короткая программа;https://...\n\n"
            "Разделитель только ';'. Если в названии есть ';', берите поле в двойные кавычки."
        )
        return

    feed_id: Optional[int] = None
    label: Optional[str] = None
    interval_opt: Optional[int] = None
    bad_args: list[str] = []
    for tok in parts[1:]:
        if tok.isdigit() and feed_id is None:
            feed_id = int(tok)
        elif tok.startswith("feed="):
            try:
                feed_id = int(tok.split("=", 1)[1])
            except Exception:
                bad_args.append(tok)
        elif tok.startswith("label="):
            label = tok.split("=", 1)[1]
        elif tok.startswith("interval="):
            try:
                interval_opt = max(1, int(tok.split("=", 1)[1]))
            except Exception:
                bad_args.append(tok)
        else:
            bad_args.append(tok)
    if bad_args:
        await message.answer("Не понял параметры: " + " ".join(bad_args))
        return

    with session_scope() as s:
        if feed_id is not None:
            feed = s.get(Feed, feed_id)
            if not feed or feed.user_id != user_id:
                await message.answer("Лента не найдена.")
                return
            if (feed.type or "").strip().lower() not in ("event_json", "event_manual"):
                await message.answer(
                    "Эта лента не подходит для событий. Укажите event-ленту или не указывайте feed, "
                    "тогда будет создана новая."
                )
                return
            feed.enabled = True
            feed.mode = "immediate"
            if label is not None:
                feed.label = label
            if interval_opt is not None:
                feed.poll_interval_min = interval_opt
            elif not feed.poll_interval_min:
                feed.poll_interval_min = 1
            poll_interval = feed.poll_interval_min
        else:
            poll_interval = interval_opt or 1
            manual_url = (
                f"manual://events/{user_id}/{int(datetime.now(timezone.utc).timestamp() * 1000)}"
            )
            feed = Feed(
                user_id=user_id,
                url=manual_url,
                type="event_manual",
                label=label or "manual events",
                mode="immediate",
                poll_interval_min=poll_interval,
                enabled=True,
            )
            s.add(feed)
            s.flush()
            feed_id = feed.id

    assert feed_id is not None

    settings = Settings()
    parsed_items, errors = parse_bulk_events_text(body, settings.TZ)
    if not parsed_items:
        err_preview = "\n".join(errors[:8]) if errors else "Нет корректных строк."
        await message.answer(f"События не добавлены.\n{err_preview}")
        return

    created = 0
    updated = 0
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        for event in parsed_items:
            ext_id = str(event["external_id"])
            existing = (
                s.query(Item).filter(Item.feed_id == feed_id, Item.external_id == ext_id).first()
            )
            if existing:
                existing.title = str(event["title"])
                existing.link = str(event["link"])
                existing.published_at = event["published_at"]  # type: ignore[assignment]
                updated += 1
                continue
            it = Item(
                feed_id=feed_id,
                external_id=ext_id,
                title=str(event["title"]),
                link=str(event["link"]),
                published_at=event["published_at"],  # type: ignore[arg-type]
                categories=["event_start"],
                summary_hash=hashlib.sha1(
                    f"{event['title']}\n{event['link']}\n{event['published_at']}".encode("utf-8")
                ).hexdigest(),
            )
            s.add(it)
            created += 1
        # Make sure this feed is treated as manual events if newly adapted.
        if (feed.type or "").strip().lower() != "event_json":
            feed.type = "event_manual"

    DEPS.scheduler.schedule_feed_poll(feed_id, poll_interval)
    delivered = await DEPS.scheduler._deliver_due_event_starts(feed_id)

    msg = (
        f"Готово. Лента id={feed_id}. Добавлено: {created}, обновлено: {updated}, "
        f"отправлено старт-уведомлений: {delivered}."
    )
    if errors:
        msg += "\nОшибки парсинга (первые):\n" + "\n".join(errors[:8])
    await message.answer(msg)


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
            type_part = f" [{f.type or 'unknown'}]"
            lines.append(
                f"{status} {f.id}: {display_name} — {f.mode}{time_part}{type_part}"
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
        await message.answer("Использование: /setmode <feed_id> <mode> [HH:MM]")
        return
    try:
        feed_id = int(parts[1])
    except Exception:
        await message.answer("Неверный id.")
        return

    mode = parts[2].strip().lower()
    allowed_modes = {"immediate", "digest", "on_demand"}
    if mode not in allowed_modes:
        await message.answer("Неверный режим. Доступные: immediate, digest, on_demand.")
        return

    def _normalize_hhmm(value: str) -> Optional[str]:
        if ":" not in value:
            return None
        hh, mm = value.split(":", 1)
        if not (hh.isdigit() and mm.isdigit()):
            return None
        if len(mm) != 2:
            return None
        try:
            h = int(hh)
            m = int(mm)
        except Exception:
            return None
        if h < 0 or h > 23 or m < 0 or m > 59:
            return None
        return f"{h:02d}:{m:02d}"

    digest_time_raw = None
    digest_time_provided = False
    extra_parts: list[str] = []
    for p in parts[3:]:
        if p.startswith("time="):
            digest_time_provided = True
            digest_time_raw = p.split("=", 1)[1].strip()
        else:
            normalized = _normalize_hhmm(p.strip())
            if normalized:
                if digest_time_provided:
                    extra_parts.append(p)
                else:
                    digest_time_provided = True
                    digest_time_raw = p.strip()
            else:
                extra_parts.append(p)

    if extra_parts:
        await message.answer(
            "Неверные параметры: " + " ".join(extra_parts) + ". Формат: /setmode <feed_id> <mode> [HH:MM]"
        )
        return

    digest_time = None
    if digest_time_provided:
        digest_time = _normalize_hhmm(digest_time_raw or "")
        if not digest_time:
            await message.answer("Неверный формат времени. Используйте HH:MM, например 23:00.")
            return
        if mode != "digest":
            await message.answer("Время можно задавать только для режима digest.")
            return
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or feed.user_id != user_id:
            await message.answer("Лента не найдена.")
            return
        feed.mode = mode
        if mode == "digest":
            if digest_time_provided:
                feed.digest_time_local = digest_time
            elif not feed.digest_time_local:
                feed.digest_time_local = DEPS.settings.DIGEST_DEFAULT_TIME
        else:
            feed.digest_time_local = None
        interval = feed.poll_interval_min
        new_mode = feed.mode
        new_time = feed.digest_time_local
    # Reschedule polling job (unchanged interval)
    DEPS.scheduler.schedule_feed_poll(feed_id, interval)
    if new_mode == "digest":
        await message.answer(f"Режим обновлён для ленты {feed_id}: digest, время {new_time}.")
    else:
        await message.answer(f"Режим обновлён для ленты {feed_id}: {new_mode}.")


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
        await DEPS.scheduler._send_digest_for_feed(fid, update_last_digest_at=False)
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
