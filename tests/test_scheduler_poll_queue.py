import asyncio
from types import SimpleNamespace

from rssbot.scheduler import BotScheduler, POLL_WORKERS


def test_poll_burst_keeps_every_feed_and_coalesces_duplicates(monkeypatch, caplog):
    async def run():
        scheduler = BotScheduler(bot=SimpleNamespace())
        workers_busy = asyncio.Event()
        release_workers = asyncio.Event()
        started = []
        running = set()

        async def poll(feed_id):
            started.append(feed_id)
            running.add(feed_id)
            assert len(running) <= POLL_WORKERS
            if len(running) == POLL_WORKERS:
                workers_busy.set()
            try:
                await release_workers.wait()
                if feed_id == 1:
                    raise ValueError("private URL")
            finally:
                running.remove(feed_id)

        monkeypatch.setattr(scheduler, "_poll_feed_job", poll)
        feed_ids = list(range(1, 24))
        scheduler.start()
        try:
            for feed_id in feed_ids:
                await scheduler._enqueue_feed_poll(feed_id)
            await asyncio.wait_for(workers_busy.wait(), timeout=2)
            assert started == feed_ids[:POLL_WORKERS]

            # A second tick covers both running feeds and feeds still waiting.
            for feed_id in feed_ids:
                await scheduler._enqueue_feed_poll(feed_id)
            release_workers.set()
            await asyncio.wait_for(scheduler._poll_queue.join(), timeout=2)
            assert started == feed_ids

            await scheduler._enqueue_feed_poll(1)
            await asyncio.wait_for(scheduler._poll_queue.join(), timeout=2)
            assert started == feed_ids + [1]
        finally:
            await scheduler.shutdown()

    asyncio.run(run())
    assert "Poll failed for feed 1 (ValueError)" in caplog.text
    assert "private URL" not in caplog.text
