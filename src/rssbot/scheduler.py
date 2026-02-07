from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .db import Delivery, Feed, Item, User, session_scope, FeedBaseline
from .rules import Content, matches_rules
from .rss import fetch_and_store_event_source, fetch_and_store_feed, compute_available_at
from .config import Settings


@dataclass
class BotContext:
    bot: Bot


class BotScheduler:
    def __init__(self, bot: Bot) -> None:
        self.ctx = BotContext(bot=bot)
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        self.scheduler.start()
        # Digest scanner runs every minute
        self.scheduler.add_job(self._digest_scan_tick, "cron", second=0, id="digest-scan")

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    def schedule_feed_poll(self, feed_id: int, interval_min: int) -> None:
        job_id = f"poll:{feed_id}"
        # Replace existing job if present
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass
        self.scheduler.add_job(
            self._poll_feed_job,
            trigger="interval",
            minutes=max(1, interval_min),
            id=job_id,
            args=[feed_id],
            coalesce=True,
            max_instances=1,
        )

    def unschedule_feed_poll(self, feed_id: int) -> None:
        job_id = f"poll:{feed_id}"
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

    async def _poll_feed_job(self, feed_id: int) -> None:
        with session_scope() as s:
            feed = s.get(Feed, feed_id)
            if not feed or not feed.enabled:
                return
            feed_type = (feed.type or "").strip().lower()

        if feed_type in {"event_json", "event_ics", "event_manual"}:
            try:
                if feed_type in {"event_json", "event_ics"}:
                    await fetch_and_store_event_source(feed_id)
                await self._deliver_due_event_starts(feed_id)
            except Exception:
                return
            return

        try:
            new_ids = await fetch_and_store_feed(feed_id)
        except Exception:
            return

        if not new_ids:
            return

        # handle deliveries for immediate mode
        for item_id in new_ids:
            await self._maybe_deliver_immediate(item_id)

    async def _send_event_start_message(
        self, chat_id: int, title: str, link: str
    ) -> tuple[str, Optional[str]]:
        text = f"Старт трансляции: {title}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть", url=link)]])
        try:
            await self.ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            return "ok", None
        except Exception as e:
            return "fail", str(e)[:1000]

    async def _send_video_message(
        self, chat_id: int, title: str, link: str, feed_name: Optional[str] = None
    ) -> tuple[str, Optional[str]]:
        normalized_feed_name = (feed_name or "").strip()
        feed_name_text = normalized_feed_name or "без названия ленты"
        text = f"Новый ролик: {title} [{feed_name_text}]"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть", url=link)]])
        try:
            await self.ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            return "ok", None
        except Exception as e:
            return "fail", str(e)[:1000]

    async def _maybe_deliver_immediate(self, item_id: int) -> None:
        with session_scope() as s:
            item = s.get(Item, item_id)
            if not item:
                return
            feed = s.get(Feed, item.feed_id)
            if not feed or not feed.enabled or feed.mode != "immediate":
                return
            user = s.get(User, feed.user_id)
            rules = feed.rules

            content = Content(
                title=item.title or "",
                description="",
                categories=item.categories,
                duration_sec=item.duration_sec,
            )
            # Skip future items (scheduled/premieres) until available_at
            settings = Settings()
            if settings.HIDE_FUTURE_VIDEOS:
                available_at = compute_available_at(item.title or "", item.published_at)
                if available_at and datetime.now(timezone.utc) < available_at:
                    return
            if not matches_rules(content, rules):
                return

            # Check deduplication
            d = (
                s.query(Delivery.id)
                .filter(
                    Delivery.item_id == item.id,
                    Delivery.feed_id == feed.id,
                    Delivery.user_id == user.id,
                    Delivery.channel == "immediate",
                )
                .first()
            )
            if d:
                return

            # snapshot values
            chat_id = user.chat_id
            item_id_v = item.id
            feed_id_v = feed.id
            title = item.title or "(без названия)"
            link = item.link or ""
            feed_name = (feed.label or feed.name or "").strip()

        # Send message outside of transaction
        status, error = await self._send_video_message(chat_id, title, link, feed_name)

        with session_scope() as s:
            s.add(
                Delivery(
                    item_id=item_id_v,
                    feed_id=feed_id_v,
                    user_id=s.query(User.id).filter(User.chat_id == chat_id).scalar(),
                    channel="immediate",
                    status=status,
                    error_message=error,
                )
            )

    async def _deliver_due_event_starts(self, feed_id: int) -> int:
        now_utc = datetime.now(timezone.utc)
        with session_scope() as s:
            feed = s.get(Feed, feed_id)
            if not feed or not feed.enabled:
                return 0
            feed_type = (feed.type or "").strip().lower()
            if feed_type not in {"event_json", "event_ics", "event_manual"}:
                return 0
            user = s.get(User, feed.user_id)
            if not user:
                return 0
            due_item_ids = [
                row[0]
                for row in (
                    s.query(Item.id)
                    .filter(
                        Item.feed_id == feed_id,
                        Item.published_at.isnot(None),
                        Item.published_at <= now_utc,
                    )
                    .order_by(Item.published_at.asc(), Item.id.asc())
                    .all()
                )
            ]
            user_id = user.id

        sent = 0
        for item_id in due_item_ids:
            with session_scope() as s:
                item = s.get(Item, item_id)
                if not item:
                    continue
                feed = s.get(Feed, item.feed_id)
                if not feed or not feed.enabled:
                    continue
                user = s.get(User, feed.user_id)
                if not user or user.id != user_id:
                    continue
                if not item.published_at or item.published_at > datetime.now(timezone.utc):
                    continue

                delivered = (
                    s.query(Delivery.id)
                    .filter(
                        Delivery.item_id == item.id,
                        Delivery.feed_id == feed.id,
                        Delivery.user_id == user.id,
                        Delivery.channel == "immediate",
                    )
                    .first()
                )
                if delivered:
                    continue

                content = Content(
                    title=item.title or "",
                    description="",
                    categories=item.categories,
                    duration_sec=item.duration_sec,
                )
                if not matches_rules(content, feed.rules):
                    continue

                chat_id = user.chat_id
                item_id_v = item.id
                feed_id_v = feed.id
                user_id_v = user.id
                title = item.title or "(без названия)"
                link = item.link or ""

            status, error = await self._send_event_start_message(chat_id, title, link)
            with session_scope() as s:
                s.add(
                    Delivery(
                        item_id=item_id_v,
                        feed_id=feed_id_v,
                        user_id=user_id_v,
                        channel="immediate",
                        status=status,
                        error_message=error,
                    )
                )
            if status == "ok":
                sent += 1
        return sent

    async def _digest_scan_tick(self) -> None:
        # For each feed in digest mode, if it's time in user's tz and not sent today, send digest
        with session_scope() as s:
            rows = (
                s.query(Feed, User)
                .join(User, Feed.user_id == User.id)
                .filter(Feed.enabled == True, Feed.mode == "digest")
                .all()
            )

        now_utc = datetime.now(timezone.utc)
        for feed, user in rows:
            if not feed.digest_time_local:
                continue
            try:
                hh, mm = [int(x) for x in feed.digest_time_local.split(":", 1)]
            except Exception:
                continue
            tz = ZoneInfo(user.tz or "UTC")
            now_local = now_utc.astimezone(tz)
            want = time(hour=hh, minute=mm)
            if now_local.hour != want.hour or now_local.minute != want.minute:
                continue
            # Check if already sent today
            if feed.last_digest_at:
                last_local = feed.last_digest_at.astimezone(tz)
                if last_local.date() == now_local.date():
                    continue
            await self._send_digest_for_feed(feed.id)

    async def _send_digest_for_feed(
        self, feed_id: int, *, update_last_digest_at: bool = True
    ) -> None:
        with session_scope() as s:
            feed = s.get(Feed, feed_id)
            if not feed:
                return
            user = s.get(User, feed.user_id)
            rules = feed.rules
            baseline = s.get(FeedBaseline, feed.id)

            delivered_item_ids = {
                r[0]
                for r in s.query(Delivery.item_id)
                .filter(Delivery.feed_id == feed.id, Delivery.user_id == user.id)
                .all()
            }
            items = (
                s.query(Item)
                .filter(Item.feed_id == feed.id)
                .order_by(Item.published_at.desc().nullslast(), Item.id.desc())
                .all()
            )

            def after_baseline(it: Item) -> bool:
                if not baseline:
                    return True
                # Exclude the baseline item itself
                if baseline.baseline_item_external_id and it.external_id == baseline.baseline_item_external_id:
                    return False
                if baseline.baseline_published_at and it.published_at:
                    return it.published_at > baseline.baseline_published_at
                # Fallback to creation time cutoff
                ref = baseline.baseline_set_at
                # created_at could be None for legacy rows
                return (it.created_at or ref) > ref

            kept_info = []
            settings = Settings()
            now_utc = datetime.now(timezone.utc)
            for it in items:
                if it.id in delivered_item_ids:
                    continue
                content = Content(
                    title=it.title or "",
                    categories=it.categories,
                    duration_sec=it.duration_sec,
                )
                if settings.HIDE_FUTURE_VIDEOS:
                    available_at = compute_available_at(it.title or "", it.published_at)
                    if available_at and now_utc < available_at:
                        continue
                if after_baseline(it) and matches_rules(content, rules):
                    kept_info.append(
                        {
                            "id": it.id,
                            "title": it.title or "(без названия)",
                            "link": it.link or "",
                            "published_at": it.published_at,
                        }
                    )

            chat_id = user.chat_id
            feed_id_v = feed.id
            feed_name = (feed.label or feed.name or "").strip()

        if not kept_info:
            if update_last_digest_at:
                with session_scope() as s:
                    f = s.get(Feed, feed_id)
                    if f:
                        f.last_digest_at = datetime.now(timezone.utc)
            return

        kept_info = kept_info[:20]
        send_results = []
        for info in kept_info:
            status, error = await self._send_video_message(
                chat_id, info["title"], info["link"], feed_name
            )
            send_results.append(
                {
                    "id": info["id"],
                    "status": status,
                    "error": error,
                    "sent_at": datetime.now(timezone.utc),
                }
            )

        now = datetime.now(timezone.utc)
        with session_scope() as s:
            # find user id for chat_id
            user_id_v = s.query(User.id).filter(User.chat_id == chat_id).scalar()
            for result in send_results:
                s.add(
                    Delivery(
                        item_id=result["id"],
                        feed_id=feed_id_v,
                        user_id=user_id_v,
                        channel="digest",
                        status=result["status"],
                        error_message=result["error"],
                        sent_at=result["sent_at"],
                    )
                )
            if update_last_digest_at:
                f = s.get(Feed, feed_id)
                if f:
                    f.last_digest_at = now

    async def _send_item_once_ignore_mode(self, item_id: int) -> tuple[bool, str]:
        """Send a single item immediately regardless of feed mode.

        Also marks digest delivery for digest feeds to avoid duplicating in the next digest.
        Returns (delivered, reason).
        """
        with session_scope() as s:
            item = s.get(Item, item_id)
            if not item:
                return False, "item_not_found"
            feed = s.get(Feed, item.feed_id)
            if not feed or not feed.enabled:
                return False, "feed_disabled"
            user = s.get(User, feed.user_id)
            rules = feed.rules

            content = Content(
                title=item.title or "",
                description="",
                categories=item.categories,
                duration_sec=item.duration_sec,
            )
            # Skip future items
            settings = Settings()
            if settings.HIDE_FUTURE_VIDEOS:
                available_at = compute_available_at(item.title or "", item.published_at)
                if available_at and datetime.now(timezone.utc) < available_at:
                    return False, "not_available_yet"
            if not matches_rules(content, rules):
                return False, "filtered"

            chat_id = user.chat_id
            user_id_v = user.id
            feed_id_v = feed.id
            title = item.title or "(без названия)"
            feed_name = (feed.label or feed.name or "").strip()
        link = item.link or ""
        is_digest_mode = feed.mode == "digest"

        status, error = await self._send_video_message(chat_id, title, link, feed_name)

        now = datetime.now(timezone.utc)
        with session_scope() as s:
            s.add(
                Delivery(
                    item_id=item_id,
                    feed_id=feed_id_v,
                    user_id=user_id_v,
                    channel="immediate",
                    status=status,
                    error_message=error,
                    sent_at=now,
                )
            )
            if is_digest_mode:
                s.add(
                    Delivery(
                        item_id=item_id,
                        feed_id=feed_id_v,
                        user_id=user_id_v,
                        channel="digest",
                        status="ok" if status == "ok" else "fail",
                        error_message=error,
                        sent_at=now,
                    )
                )
        return (status == "ok"), ("ok" if status == "ok" else (error or "send_failed"))
