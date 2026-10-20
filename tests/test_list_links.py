from rssbot.bot import _format_feed_list_line, _resolve_feed_display_url
from rssbot.db import Feed


def test_resolve_feed_display_url_for_channel():
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert _resolve_feed_display_url(url) == "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw/videos"


def test_resolve_feed_display_url_for_playlist():
    url = "https://www.youtube.com/feeds/videos.xml?playlist_id=PL590L5WQmH8fJ54Fglz5rT4f8LL2Si3xK"
    assert _resolve_feed_display_url(url) == "https://www.youtube.com/playlist?list=PL590L5WQmH8fJ54Fglz5rT4f8LL2Si3xK"


def test_resolve_feed_display_url_for_non_youtube():
    assert _resolve_feed_display_url("https://example.com/rss.xml") is None


def test_format_feed_list_line_adds_clickable_title_for_youtube():
    feed = Feed(
        id=7,
        user_id=1,
        url="https://www.youtube.com/feeds/videos.xml?channel_id=UC123",
        type="youtube",
        name=None,
        label="Best <Channel>",
        mode="digest",
        digest_time_local="08:30",
        poll_interval_min=10,
        enabled=True,
    )

    line = _format_feed_list_line(feed)
    assert line.startswith("✓ 7:")
    assert '<a href="https://www.youtube.com/channel/UC123/videos">Best &lt;Channel&gt;</a>' in line
    assert "</a> — digest в <code>08:30</code> [youtube]" in line


def test_list_keeps_digest_times_separate_from_channel_links(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from rssbot import bot
    from rssbot.db import User, init_engine, session_scope

    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        user_id = user.id
        for number, time in enumerate(("08:30", "20:00")):
            session.add(Feed(
                user_id=user_id,
                url=f"https://www.youtube.com/feeds/videos.xml?channel_id=UC{number}",
                name=f"Channel {number}", type="youtube", mode="digest", digest_time_local=time,
            ))
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: user_id)
    message = SimpleNamespace(text="/list", answer=AsyncMock(), delete=AsyncMock())
    asyncio.run(bot.cmd_list(message))
    text = message.answer.await_args.args[0]
    for number, time in enumerate(("08:30", "20:00")):
        assert f'<a href="https://www.youtube.com/channel/UC{number}/videos">Channel {number}</a> — digest в <code>{time}</code>' in text
    assert message.answer.await_args.kwargs["parse_mode"] == "HTML"
    assert message.answer.await_args.kwargs["disable_web_page_preview"] is True
    message.delete.assert_awaited_once()


def test_format_feed_list_line_keeps_plain_title_for_other_feeds():
    feed = Feed(
        id=8,
        user_id=1,
        url="https://example.com/rss.xml?x=1&y=2",
        type="event_json",
        name=None,
        label="Site <Feed>",
        mode="immediate",
        digest_time_local=None,
        poll_interval_min=10,
        enabled=False,
    )

    line = _format_feed_list_line(feed)
    assert line.startswith("✗ 8:")
    assert "<a href=" not in line
    assert "Site &lt;Feed&gt;" in line
    assert " — immediate [event_json]" in line
