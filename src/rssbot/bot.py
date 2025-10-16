from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from .config import Settings
from .db import Delivery, Feed, FeedBaseline, FeedRule, Item, Session, User, session_scope
from .scheduler import BotScheduler
from .rss import fetch_and_store_latest_item, fetch_and_store_recent


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
    await message.answer(
        "Привет! Я буду присылать новые видео из твоих YouTube RSS.\n"
        "Команды: /addfeed, /channel, /playlist, /list, /remove, /setmode, /setfilter, /digest, /latest, /mute, /unmute, /dedupe."
    )


@router.message(Command("addfeed"))
async def cmd_addfeed(message: Message) -> None:
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return

    text = (message.text or "").strip().split(maxsplit=1)
    if len(text) < 2:
        await message.answer(
            "Использование: /addfeed <url> [mode=immediate|digest|on_demand] [label=...] [interval=10] [time=HH:MM]"
        )
        return
    args = text[1].split()
    url = None
    mode = "immediate"
    label = None
    interval = DEPS.settings.DEFAULT_POLL_INTERVAL_MIN
    digest_time = DEPS.settings.DIGEST_DEFAULT_TIME

    for a in args:
        if a.startswith("mode="):
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
        elif not url:
            url = a

    if not url:
        await message.answer("Не указан URL ленты.")
        return

    await _create_feed_and_seed_reply(
        message=message,
        user_id=user_id,
        url=url,
        mode=mode,
        label=label,
        interval=interval,
        digest_time=digest_time,
    )


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
        if a.startswith("mode="):
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
        if a.startswith("mode="):
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


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    with session_scope() as s:
        feeds = s.query(Feed).filter(Feed.user_id == user_id).order_by(Feed.id.asc()).all()
    if not feeds:
        await message.answer("Список лент пуст.")
        return
    lines = ["Ленты:"]
    for f in feeds:
        lines.append(
            f"#{f.id}: {f.label or f.url} | mode={f.mode} | enabled={f.enabled} | interval={f.poll_interval_min} | time={f.digest_time_local or '-'}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("remove"))
async def cmd_remove(message: Message) -> None:
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
        feed.enabled = False
    DEPS.scheduler.unschedule_feed_poll(feed_id)
    await message.answer("Лента отключена.")


@router.message(Command("mute"))
async def cmd_mute(message: Message) -> None:
    await _toggle_enabled(message, False)


@router.message(Command("unmute"))
async def cmd_unmute(message: Message) -> None:
    await _toggle_enabled(message, True)


async def _toggle_enabled(message: Message, value: bool) -> None:
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(f"Использование: /{'unmute' if value else 'mute'} <feed_id>")
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
        feed.enabled = value
        interval = feed.poll_interval_min
    if value:
        DEPS.scheduler.schedule_feed_poll(feed_id, interval)
    else:
        DEPS.scheduler.unschedule_feed_poll(feed_id)
    await message.answer("Готово.")


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
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    text = (message.text or "").split(maxsplit=2)
    if len(text) < 3:
        await message.answer(
            "Использование: /setfilter <feed_id> <json>, например: {\"include_keywords\":[\"обзор\"]}"
        )
        return
    try:
        feed_id = int(text[1])
    except Exception:
        await message.answer("Неверный id.")
        return
    try:
        payload = json.loads(text[2])
    except Exception:
        await message.answer("Неверный JSON.")
        return
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or feed.user_id != user_id:
            await message.answer("Лента не найдена.")
            return
        rules = feed.rules or FeedRule(feed_id=feed.id)
        for key in (
            "include_keywords",
            "exclude_keywords",
            "include_regex",
            "exclude_regex",
            "require_all",
            "case_sensitive",
            "categories",
            "min_duration_sec",
            "max_duration_sec",
        ):
            if key in payload:
                setattr(rules, key, payload[key])
        s.add(rules)
    await message.answer("Фильтры обновлены.")


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    target = parts[1] if len(parts) > 1 else "all"
    if target == "all":
        with session_scope() as s:
            feeds = s.query(Feed).filter(Feed.user_id == user_id).all()
        count = 0
        for f in feeds:
            await DEPS.scheduler._send_digest_for_feed(f.id)
            count += 1
        await message.answer(f"Дайджест отправлен для {count} лент.")
    else:
        try:
            feed_id = int(target)
        except Exception:
            await message.answer("Неверный аргумент. Используйте 'all' или id ленты.")
            return
        await DEPS.scheduler._send_digest_for_feed(feed_id)
        await message.answer("Дайджест отправлен.")


@router.message(Command("latest"))
async def cmd_latest(message: Message) -> None:
    """Вывести N последних видео.

    Формат:
    - /latest N — показать N последних по всем вашим лентам
    - /latest N <feed_id> — показать N последних только для указанной ленты
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /latest <N> [feed_id]")
        return
    try:
        n = int(parts[1])
        if n <= 0:
            raise ValueError
    except Exception:
        await message.answer("N должно быть положительным числом.")
        return
    feed_id = None
    if len(parts) >= 3:
        try:
            feed_id = int(parts[2])
        except Exception:
            await message.answer("feed_id должно быть числом.")
            return

    # Query items ignoring deliveries/baseline/filters
    with session_scope() as s:
        if feed_id is not None:
            feed = s.get(Feed, feed_id)
            if not feed or feed.user_id != user_id:
                await message.answer("Лента не найдена.")
                return
            items = (
                s.query(Item)
                .filter(Item.feed_id == feed.id)
                .order_by(Item.published_at.desc().nullslast(), Item.id.desc())
                .limit(n)
                .all()
            )
            lines = [f"Последние {len(items)} видео для ленты #{feed.id} ({feed.label or feed.url}):"]
            for it in items:
                when = (
                    it.published_at.strftime("%Y-%m-%d")
                    if it.published_at
                    else (it.created_at.strftime("%Y-%m-%d") if it.created_at else "")
                )
                lines.append(f"• {it.title or '(без названия)'} ({when})\n{it.link or ''}")
            text = "\n\n".join(lines) if items else "Нет элементов."
            await message.answer(text)
            return
        # All feeds of user
        rows = (
            s.query(Item, Feed)
            .join(Feed, Item.feed_id == Feed.id)
            .filter(Feed.user_id == user_id)
            .order_by(Item.published_at.desc().nullslast(), Item.id.desc())
            .limit(n)
            .all()
        )
        if not rows:
            await message.answer("Нет элементов.")
            return
        lines = [f"Последние {n} видео по всем лентам (найдено {len(rows)}):"]
        for it, f in rows:
            when = (
                it.published_at.strftime("%Y-%m-%d")
                if it.published_at
                else (it.created_at.strftime("%Y-%m-%d") if it.created_at else "")
            )
            lines.append(
                f"• [{f.label or f.url}] {it.title or '(без названия)'} ({when})\n{it.link or ''}"
            )
        await message.answer("\n\n".join(lines))


@router.message(Command("backfill"))
async def cmd_backfill(message: Message) -> None:
    """Загрузить последние N элементов в БД без отправки сообщений.

    Формат: /backfill <feed_id> [N=10]
    """
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /backfill <feed_id> [N=10]")
        return
    try:
        feed_id = int(parts[1])
    except Exception:
        await message.answer("feed_id должно быть числом.")
        return
    n = 10
    if len(parts) >= 3:
        try:
            n = int(parts[2])
        except Exception:
            pass
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed or feed.user_id != user_id:
            await message.answer("Лента не найдена.")
            return
    try:
        created_ids = await fetch_and_store_recent(feed_id, n)
        await message.answer(
            f"Загружено новых элементов: {len(created_ids)}. Теперь можно использовать /latest {n} {feed_id}."
        )
    except Exception as e:
        await message.answer(f"Ошибка backfill: {str(e)[:200]}")


@router.message(Command("dedupe"))
async def cmd_dedupe(message: Message) -> None:
    user_id = _ensure_user_id(message)
    if not user_id:
        await message.answer("Доступ запрещен.")
        return
    removed = _dedupe_user_feeds(user_id)
    await message.answer(f"Удалено дублей: {removed}.")
