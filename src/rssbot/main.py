from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .bot import router, set_deps
from .config import Settings, ensure_data_dir
from .db import Feed, init_engine, session_scope
from .scheduler import BotScheduler
from .rss import fetch_and_store_recent


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def app() -> None:
    settings = Settings()
    ensure_data_dir(settings.DB_PATH)
    init_engine(settings.DB_PATH)

    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    scheduler = BotScheduler(bot)
    set_deps(settings, scheduler)

    dp.include_router(router)

    # Start scheduler
    scheduler.start()

    # Schedule polling for existing enabled feeds
    with session_scope() as s:
        feed_ids = [f.id for f in s.query(Feed).filter(Feed.enabled == True).all()]
        for fid in feed_ids:
            feed = s.get(Feed, fid)
            scheduler.schedule_feed_poll(feed.id, feed.poll_interval_min)

    # Optional backfill on startup (store last N unseen items without sending)
    backfill_n = settings.BACKFILL_ON_START_N
    if backfill_n and backfill_n > 0:
        async def _backfill_all(ids: list[int]) -> None:
            sem = asyncio.Semaphore(3)

            async def worker(fid: int) -> None:
                async with sem:
                    try:
                        await fetch_and_store_recent(fid, backfill_n)
                    except Exception:
                        pass

            await asyncio.gather(*(worker(i) for i in ids))

        await _backfill_all(feed_ids)

    # Run bot
    await dp.start_polling(bot)


def main() -> None:
    try:
        asyncio.run(app())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
