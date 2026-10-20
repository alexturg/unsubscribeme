import asyncio
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from rssbot import bot
from rssbot import ai_summarizer
from rssbot import youtube_context
from rssbot.db import CardEntry, CardMessage, Feed, FeedRule, User, init_engine, session_scope


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_context, "fetch_video_duration", lambda *_a, **_k: None)
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        user_id = user.id
    monkeypatch.setattr(bot, "_ensure_user_id", lambda _message: user_id)
    monkeypatch.setattr(bot, "_is_allowed", lambda _chat_id: True)
    monkeypatch.setattr(bot, "fetch_video_title", lambda *_args, **_kwargs: "Page <Title> & more")
    monkeypatch.setattr(
        bot, "fetch_video_metadata",
        lambda *_args, **_kwargs: youtube_context.VideoMetadata("Page <Title> & more"),
    )
    monkeypatch.setattr(
        bot,
        "fetch_webpage_content",
        lambda *_args, **_kwargs: SimpleNamespace(title="Page <Title> & more"),
    )
    return user_id


def _message(text, *, send_fails=False):
    sent = SimpleNamespace(message_id=200)
    answer = AsyncMock(side_effect=RuntimeError("send failed") if send_fails else None)
    if not send_fails:
        answer.return_value = sent
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=77),
        message_id=100,
        answer=answer,
        delete=AsyncMock(),
    )


def test_plain_youtube_link_becomes_card_and_deletes_original(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    message = _message(" https://youtu.be/dQw4w9WgXcQ ")
    asyncio.run(bot.cmd_plain_link(message))
    message.delete.assert_awaited_once()
    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert '<a href="https://youtu.be/dQw4w9WgXcQ">Page &lt;Title&gt; &amp; more</a>' in text
    preview = message.answer.await_args.kwargs["link_preview_options"]
    assert preview.is_disabled is False
    assert preview.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert preview.prefer_large_media is True
    assert [button.text for button in markup.inline_keyboard[0]] == ["Сделать /ai"]
    assert [button.text for button in markup.inline_keyboard[1]] == ["✓", "Skip", "Someday"]
    with session_scope() as session:
        card = session.query(CardEntry).one()
        assert card.status is None
        assert card.link == "https://youtu.be/dQw4w9WgXcQ"
        assert card.title == "Page <Title> & more"
        assert session.query(CardMessage).one().message_id == 200
        assert markup.inline_keyboard[0][0].callback_data == f"ai:link:{card.id}"


def test_plain_web_link_has_ai_button_and_linked_title(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    message = _message("https://example.com/article?a=1&b=2")
    asyncio.run(bot.cmd_plain_link(message))
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert [button.text for button in markup.inline_keyboard[0]] == ["Сделать /ai"]
    assert "&amp;" in message.answer.await_args.args[0]
    message.delete.assert_awaited_once()


@pytest.mark.parametrize("url", [
    "https://youtu.be/y7d0SUg6mhY?si=JYnAODcWVDS03JeB",
    "https://www.youtube.com/watch?v=y7d0SUg6mhY&feature=share",
    "https://www.youtube.com/shorts/y7d0SUg6mhY",
])
def test_video_card_preview_uses_canonical_url(tmp_path, monkeypatch, url):
    _setup(tmp_path, monkeypatch)
    message = _message(url)
    asyncio.run(bot.cmd_plain_link(message))
    preview = message.answer.await_args.kwargs["link_preview_options"]
    assert preview.url == "https://www.youtube.com/watch?v=y7d0SUg6mhY"
    assert preview.is_disabled is False
    with session_scope() as session:
        assert session.query(CardEntry).one().link == url


def test_video_duration_is_shown_and_saved_for_someday(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(youtube_context, "fetch_video_duration", lambda *_a, **_k: 3754)
    message = _message("https://youtu.be/dQw4w9WgXcQ")
    asyncio.run(bot.cmd_plain_link(message))
    assert "</a> (1:02:34)" in message.answer.await_args.args[0]
    with session_scope() as session:
        card = session.query(CardEntry).one()
        assert card.duration_sec == 3754
        assert "</a> (1:02:34)" in bot._someday_text(1, [card], False)


def test_non_link_or_failed_send_keeps_original(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    for text in ("hello", "Read https://example.com", "https://example.com one more"):
        message = _message(text)
        asyncio.run(bot.cmd_plain_link(message))
        message.answer.assert_not_awaited()
        message.delete.assert_not_awaited()
    failed = _message("https://example.com", send_fails=True)
    asyncio.run(bot.cmd_plain_link(failed))
    failed.delete.assert_not_awaited()


@pytest.mark.parametrize("rate_limits", [1, 2])
def test_link_card_retries_after_telegram_rate_limit(tmp_path, monkeypatch, rate_limits):
    _setup(tmp_path, monkeypatch)
    sleep = AsyncMock()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    message = _message("https://youtu.be/y7d0SUg6mhY")

    async def delayed_answer(*_a, **_k):
        if message.answer.await_count <= rate_limits:
            message.delete.assert_not_awaited()
            raise TelegramRetryAfter(
                method=SendMessage(chat_id=77, text="card"), message="Flood control", retry_after=8,
            )
        return SimpleNamespace(message_id=200)

    message.answer.side_effect = delayed_answer
    asyncio.run(bot.cmd_plain_link(message))
    assert message.answer.await_count == rate_limits + 1
    assert sleep.await_count == rate_limits
    sleep.assert_awaited_with(8)
    assert message.answer.await_args_list[0].kwargs["link_preview_options"].is_disabled is False
    for call in message.answer.await_args_list[1:]:
        assert call.kwargs["link_preview_options"].is_disabled is True
        assert call.kwargs["link_preview_options"].url is None
        assert call.args == message.answer.await_args_list[0].args
        assert call.kwargs["reply_markup"] == message.answer.await_args_list[0].kwargs["reply_markup"]
    message.delete.assert_awaited_once()
    with session_scope() as session:
        assert session.query(CardEntry).count() == 1
        assert session.query(CardMessage).one().message_id == 200


def test_persistent_rate_limit_is_bounded_and_keeps_original(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    sleep = AsyncMock()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    message = _message("https://youtu.be/y7d0SUg6mhY")
    message.answer.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=77, text="card"), message="Flood control", retry_after=8,
    )
    asyncio.run(bot.cmd_plain_link(message))
    assert message.answer.await_count == 3
    assert sleep.await_count == 2
    message.delete.assert_not_awaited()
    with session_scope() as session:
        assert session.query(CardMessage).count() == 0


def test_ai_button_uses_saved_link_for_its_owner(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    message = _message("https://example.com/article")
    asyncio.run(bot.cmd_plain_link(message))
    with session_scope() as session:
        card_id = session.query(CardEntry.id).scalar()
    run_ai = AsyncMock()
    monkeypatch.setattr(bot, "_run_ai_summary", run_ai)
    callback = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=77)),
        data=f"ai:link:{card_id}",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    asyncio.run(bot.cb_ai_link(callback))
    assert run_ai.await_args.args[:2] == (77, "https://example.com/article")
    assert run_ai.await_args.kwargs["card_message"] is callback.message
    assert run_ai.await_args.kwargs["existing_card_id"] == card_id

    callback.message.chat.id = 88
    asyncio.run(bot.cb_ai_link(callback))
    assert run_ai.await_count == 1


def test_title_lookup_failure_falls_back_to_host(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(bot, "fetch_webpage_content", unavailable)
    message = _message("https://example.com/article")
    asyncio.run(bot.cmd_plain_link(message))
    assert '>example.com</a>' in message.answer.await_args.args[0]
    message.delete.assert_awaited_once()


def test_reddit_title_uses_same_source_budget_and_cache_as_summary(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        AI_SUMMARIZER_MODE="extractive", AI_SUMMARIZER_WEB_FETCH_TIMEOUT_SEC=15,
        AI_SUMMARIZER_WEB_MAX_RESPONSE_BYTES=4_000_000,
        AI_SUMMARIZER_WEB_MAX_EXTRACTED_WORDS=20_000,
        AI_SUMMARIZER_OUTPUT_DIR=tmp_path,
    )
    monkeypatch.setattr(bot, "DEPS", SimpleNamespace(settings=settings))
    calls = []
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(title="Post", source_url=url, cleaned_text="Complete post text.")
    monkeypatch.setattr(bot, "fetch_webpage_content", fetch)
    monkeypatch.setattr(ai_summarizer, "fetch_webpage_content", fetch)
    monkeypatch.setattr(ai_summarizer, "_summarize_source_text_by_mode", lambda *a, **k: "Summary")
    url = "https://www.reddit.com/r/boardgames/s/RktBVVUaTw"
    assert asyncio.run(bot._page_title_for_link(url)) == "Post"
    ai_summarizer._summarize_web_sync(settings, page_url=url, custom_prompt=None)
    for option in ("max_bytes", "max_words", "cache_dir"):
        assert calls[0][1][option] == calls[1][1][option]
    assert calls[0][1]["max_words"] == 20_000


@pytest.mark.parametrize("url", [
    "https://youtu.be/ia2QItAmyE8?si=shared",
    "https://www.youtube.com/watch?v=ia2QItAmyE8&pp=tracking",
    "https://www.youtube.com/shorts/ia2QItAmyE8",
])
def test_youtube_title_lookup_uses_video_id(monkeypatch, url):
    calls = []

    def title(video_id, **kwargs):
        calls.append(video_id)
        return "Название видео"

    monkeypatch.setattr(bot, "fetch_video_title", title)
    assert asyncio.run(bot._page_title_for_link(url)) == "Название видео"
    assert calls == ["ia2QItAmyE8"]


def test_youtube_title_failure_tries_canonical_watch_page(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("oEmbed unavailable")

    calls = []

    def page(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(title="Название со страницы")

    monkeypatch.setattr(bot, "fetch_video_title", unavailable)
    monkeypatch.setattr(bot, "fetch_webpage_content", page)
    assert asyncio.run(bot._page_title_for_link("https://youtu.be/ia2QItAmyE8?si=x")) == "Название со страницы"
    assert calls == ["https://www.youtube.com/watch?v=ia2QItAmyE8"]


def test_youtube_oembed_title_reads_metadata_and_normalizes_spaces(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == 32_000
            return json.dumps({"title": "  Название\nвидео & текст  "}).encode()

    def urlopen(request, timeout):
        url = urlparse(request.full_url)
        assert url.hostname == "www.youtube.com"
        assert url.path == "/oembed"
        assert parse_qs(url.query) == {
            "url": ["https://www.youtube.com/watch?v=ia2QItAmyE8"], "format": ["json"],
        }
        assert timeout == 3
        return Response()

    monkeypatch.setattr(youtube_context.urllib.request, "urlopen", urlopen)
    assert youtube_context.fetch_video_title("ia2QItAmyE8") == "Название видео & текст"


@pytest.mark.parametrize("failure", ["401", "missing_title", "timeout"])
def test_video_title_falls_back_to_watch_player(tmp_path, monkeypatch, failure):
    import urllib.error

    _setup(tmp_path, monkeypatch)
    expected = 'Почему вы не говорите бегло по-английски? <Секрет> & "BREAKING BAD"'
    channel_id = "UC" + "a" * 22
    calls = []

    class Response:
        def __init__(self, content):
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _limit):
            return self.content.encode()

    def urlopen(request, timeout):
        path = urlparse(request.full_url).path
        calls.append(path)
        if path == "/oembed":
            if failure == "401":
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
            if failure == "timeout":
                raise TimeoutError()
            return Response("{}")
        assert path == "/watch"
        return Response("var ytInitialPlayerResponse = " + json.dumps({
            "videoDetails": {"title": expected, "channelId": channel_id, "lengthSeconds": "1844"},
        }) + ";")

    monkeypatch.setattr(youtube_context.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bot, "fetch_video_metadata", youtube_context.fetch_video_metadata)
    monkeypatch.setattr(bot, "get_channel_id", lambda *_a, **_k: channel_id)
    monkeypatch.setattr(youtube_context, "fetch_video_duration", lambda *_a, **_k: 1844)
    message = _message("https://www.youtube.com/watch?v=Wo6oRqVtPmw")
    asyncio.run(bot.cmd_plain_link(message))
    text = message.answer.await_args.args[0]
    assert "Почему вы не говорите бегло по-английски?" in text
    assert "&lt;Секрет&gt; &amp;" in text
    assert "</a> (30:44)" in text
    assert calls == ["/oembed", "/watch"]
    with session_scope() as session:
        card = session.query(CardEntry).one()
        assert card.title == expected
        assert card.youtube_channel_id == channel_id


CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"


def _with_channel(monkeypatch):
    monkeypatch.setattr(
        bot, "fetch_video_metadata",
        lambda *_a, **_k: youtube_context.VideoMetadata(
            "Video", f"https://www.youtube.com/channel/{CHANNEL_ID}"
        ),
    )


@pytest.mark.parametrize("subscribed,enabled", [(False, False), (True, True), (True, False)])
def test_video_card_subscription_button_depends_on_channel(tmp_path, monkeypatch, subscribed, enabled):
    user_id = _setup(tmp_path, monkeypatch)
    _with_channel(monkeypatch)
    if subscribed:
        with session_scope() as session:
            session.add(Feed(
                user_id=user_id, enabled=enabled,
                url=f"https://youtube.com/feeds/videos.xml?x=1&channel_id={CHANNEL_ID}",
            ))
    message = _message("https://youtu.be/dQw4w9WgXcQ")
    asyncio.run(bot.cmd_plain_link(message))
    markup = message.answer.await_args.kwargs["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(b.text == "Подписаться" for b in buttons) is (not subscribed or not enabled)
    assert any(b.text == "Сделать /ai" for b in buttons)
    with session_scope() as session:
        assert session.query(CardEntry).one().youtube_channel_id == CHANNEL_ID


def _subscription_callback(message, card_id):
    return SimpleNamespace(
        message=message, data=f"subscribe:card:{card_id}", answer=AsyncMock(),
    )


@pytest.mark.parametrize("stale_channel", [False, True])
def test_subscribe_button_creates_and_schedules_channel_once(tmp_path, monkeypatch, stale_channel):
    _setup(tmp_path, monkeypatch)
    _with_channel(monkeypatch)
    scheduler = SimpleNamespace(schedule_feed_poll=Mock())
    monkeypatch.setattr(bot, "DEPS", SimpleNamespace(
        settings=SimpleNamespace(DEFAULT_POLL_INTERVAL_MIN=10, DIGEST_DEFAULT_TIME="21:30"),
        scheduler=scheduler,
    ))
    monkeypatch.setattr(bot, "fetch_and_store_latest_item", AsyncMock(return_value=None))
    message = _message("https://youtu.be/dQw4w9WgXcQ")
    asyncio.run(bot.cmd_plain_link(message))
    message.reply_markup = message.answer.await_args.kwargs["reply_markup"]
    message.edit_reply_markup = AsyncMock()
    with session_scope() as session:
        card_id = session.query(CardEntry.id).scalar()
        if stale_channel:
            session.get(CardEntry, card_id).youtube_channel_id = "UCXYvxL4zw64tEcCY7iYMeyQ"
    callback = _subscription_callback(message, card_id)
    asyncio.run(bot.cb_subscribe_card(callback))
    asyncio.run(bot.cb_subscribe_card(callback))
    with session_scope() as session:
        feed = session.query(Feed).one()
        assert session.get(CardEntry, card_id).youtube_channel_id == CHANNEL_ID
        assert feed.url == f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
        assert feed.enabled
        assert feed.mode == "digest"
        assert feed.digest_time_local == "21:30"
    scheduler.schedule_feed_poll.assert_called_once()
    remaining = message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    assert all(b.text != "Подписаться" for row in remaining for b in row)
    assert remaining[0][0].text == "Сделать /ai"


def test_subscribe_reenables_muted_feed_without_changing_settings(tmp_path, monkeypatch):
    user_id = _setup(tmp_path, monkeypatch)
    _with_channel(monkeypatch)
    card_id = bot.ensure_link_card(
        77, 100, "https://youtu.be/dQw4w9WgXcQ", "Video", CHANNEL_ID,
    )
    with session_scope() as session:
        feed = Feed(
            user_id=user_id, url=f"https://youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}",
            enabled=False, mode="digest", digest_time_local="18:00", digest_weekday=2,
            poll_interval_min=30,
        )
        session.add(feed)
        session.flush()
        session.add(FeedRule(feed_id=feed.id, exclude_shorts=True))
    scheduler = SimpleNamespace(schedule_feed_poll=Mock())
    monkeypatch.setattr(bot, "DEPS", SimpleNamespace(scheduler=scheduler))
    message = _message("")
    message.reply_markup = None
    message.edit_reply_markup = AsyncMock()
    asyncio.run(bot.cb_subscribe_card(_subscription_callback(message, card_id)))
    with session_scope() as session:
        feed = session.query(Feed).one()
        assert feed.enabled
        assert (feed.mode, feed.digest_time_local, feed.digest_weekday) == ("digest", "18:00", 2)
        assert feed.rules.exclude_shorts
    scheduler.schedule_feed_poll.assert_called_once_with(feed.id, 30)


def test_subscribe_callback_rejects_another_users_card(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    card_id = bot.ensure_link_card(77, 100, "https://youtu.be/dQw4w9WgXcQ", "Video", CHANNEL_ID)
    message = _message("")
    message.chat.id = 88
    callback = _subscription_callback(message, card_id)
    asyncio.run(bot.cb_subscribe_card(callback))
    callback.answer.assert_awaited_once_with("Запись недоступна.", show_alert=True)
    with session_scope() as session:
        assert session.query(Feed).count() == 0


def test_subscribe_does_not_use_stored_channel_when_verification_fails(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    card_id = bot.ensure_link_card(77, 100, "https://youtu.be/dQw4w9WgXcQ", "Video", CHANNEL_ID)
    monkeypatch.setattr(bot, "_link_card_details", AsyncMock(return_value=("Video", None)))
    callback = _subscription_callback(_message(""), card_id)
    asyncio.run(bot.cb_subscribe_card(callback))
    callback.answer.assert_awaited_once_with("Не удалось проверить канал. Попробуй ещё раз.", show_alert=True)
    with session_scope() as session:
        assert session.query(Feed).count() == 0


def test_unknown_channel_still_creates_video_card(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _with_channel(monkeypatch)
    monkeypatch.setattr(bot, "get_channel_id", Mock(side_effect=RuntimeError("offline")))
    message = _message("https://youtu.be/dQw4w9WgXcQ")
    asyncio.run(bot.cmd_plain_link(message))
    assert ">Video</a>" in message.answer.await_args.args[0]
    message.delete.assert_awaited_once()
    with session_scope() as session:
        assert session.query(CardEntry).one().youtube_channel_id is None


@pytest.mark.parametrize("author_url,expected", [
    (f"https://www.youtube.com/channel/{CHANNEL_ID}", f"https://www.youtube.com/channel/{CHANNEL_ID}"),
    ("https://www.youtube.com/@Example", "https://www.youtube.com/@Example"),
    ("https://youtube.com.example.com/@Example", None),
    ("https://example.com/@Example", None),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
    (None, None),
])
def test_video_metadata_uses_only_author_channel_urls(monkeypatch, author_url, expected):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"title": " Video ", "author_url": author_url}).encode()

    monkeypatch.setattr(youtube_context.urllib.request, "urlopen", lambda *_a, **_k: Response())
    metadata = youtube_context.fetch_video_metadata("dQw4w9WgXcQ")
    assert metadata.title == "Video"
    assert metadata.channel_url == expected


def test_existing_cards_gain_channel_column_without_losing_data(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE card_entries (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
            source_key VARCHAR(128) NOT NULL, kind VARCHAR(32) NOT NULL,
            title VARCHAR(500) NOT NULL, link VARCHAR(1000), body TEXT,
            status VARCHAR(16), status_source VARCHAR(32), chosen_at DATETIME,
            someday_at DATETIME, UNIQUE(user_id, source_key)
        )""")
        connection.execute("""INSERT INTO card_entries
            (user_id, source_key, kind, title, status)
            VALUES (1, 'link:100', 'link', 'Saved title', 'someday')""")
    init_engine(path)
    init_engine(path)
    with session_scope() as session:
        card = session.query(CardEntry).one()
        assert (card.title, card.status, card.youtube_channel_id) == ("Saved title", "someday", None)
