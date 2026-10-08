import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from rssbot import bot


def test_digest_usage_escapes_html(monkeypatch):
    message = SimpleNamespace(text="/digest", answer=AsyncMock())
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: 1)

    asyncio.run(bot.cmd_digest(message))

    message.answer.assert_awaited_once_with(
        "Использование: /digest &lt;feed_id|all&gt;"
    )
