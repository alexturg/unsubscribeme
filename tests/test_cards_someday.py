from datetime import datetime, timedelta, timezone
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from rssbot.cards import (
    advance_someday_view,
    choose_card,
    create_ai_card,
    ensure_item_card,
    get_someday_page,
    register_card_message,
    start_someday_view,
)
from rssbot import bot as bot_module
from rssbot.db import (
    Base,
    CardEntry,
    Delivery,
    Feed,
    Item,
    SchemaMigration,
    User,
    init_engine,
    session_scope,
)


def test_legacy_delivery_backfill_is_once_and_preserves_existing_rows(tmp_path):
    path = tmp_path / "existing.sqlite"
    old_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(
        old_engine, tables=[User.__table__, Feed.__table__, Item.__table__, Delivery.__table__]
    )
    with Session(old_engine) as session, session.begin():
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        feed = Feed(user_id=user.id, url="https://example.com/feed")
        session.add(feed)
        session.flush()
        for number in range(3):
            item = Item(feed_id=feed.id, external_id=str(number), title=f"Video {number}")
            session.add(item)
            session.flush()
            session.add(
                Delivery(
                    user_id=user.id,
                    feed_id=feed.id,
                    item_id=item.id,
                    channel="immediate",
                    status="fail" if number == 2 else "ok",
                )
            )
    old_engine.dispose()

    init_engine(path)
    with session_scope() as session:
        assert session.query(User).count() == 1
        assert session.query(Feed).count() == 1
        assert session.query(Item).count() == 3
        assert session.query(Delivery).count() == 3
        cards = session.query(CardEntry).order_by(CardEntry.id).all()
        assert [card.title for card in cards] == ["Video 0", "Video 1"]
        assert all(card.status == "done" and card.status_source == "legacy_delivery" for card in cards)
        assert session.query(SchemaMigration).count() == 1
        item = session.query(Item).filter(Item.external_id == "2").one()
        session.add(Delivery(user_id=1, feed_id=1, item_id=item.id, channel="immediate", status="ok"))

    init_engine(path)
    with session_scope() as session:
        assert session.query(CardEntry).count() == 2
        assert session.query(Delivery).count() == 4


def test_new_cards_stay_undecided_until_button_and_ai_group_is_one_entry(tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        feed = Feed(user_id=user.id, url="https://example.com/feed")
        session.add(feed)
        session.flush()
        item = Item(feed_id=feed.id, external_id="new", title="New video")
        session.add(item)
        session.flush()
        item_id = item.id
    card_id = ensure_item_card(77, item_id)
    with session_scope() as session:
        assert session.get(CardEntry, card_id).status is None
    found, _ = choose_card(77, card_id, "skipped")
    assert found
    with session_scope() as session:
        assert session.get(CardEntry, card_id).status == "skipped"

    ai_id = create_ai_card(77, "AI answer", "https://example.com", "A" * 9000)
    register_card_message(ai_id, 77, 10)
    register_card_message(ai_id, 77, 11)
    found, message_ids = choose_card(77, ai_id, "someday")
    assert found and message_ids == [10, 11]
    with session_scope() as session:
        ai = session.get(CardEntry, ai_id)
        assert ai.body == "A" * 9000
        assert ai.status == "someday"
    assert choose_card(88, ai_id, "done") == (False, [])


def test_someday_snapshot_survives_source_deletion(tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        user_id = user.id
        feed = Feed(user_id=user_id, url="https://example.com/feed")
        session.add(feed)
        session.flush()
        feed_id = feed.id
        item = Item(feed_id=feed_id, external_id="one", title="Saved title", link="https://example.com/one")
        session.add(item)
        session.flush()
        item_id = item.id
    card_id = ensure_item_card(77, item_id)
    choose_card(77, card_id, "someday")
    with session_scope() as session:
        session.query(Item).filter(Item.id == item_id).delete()
        session.query(Feed).filter(Feed.id == feed_id).delete()
    view_id = start_someday_view(user_id)
    page, more = get_someday_page(user_id, view_id, 1)
    assert not more
    assert [(card.title, card.link) for card in page] == [("Saved title", "https://example.com/one")]


def test_someday_more_back_and_status_change_do_not_skip_rows(tmp_path):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        user_id = user.id
        base = datetime.now(timezone.utc) - timedelta(days=1)
        for number in range(23):
            session.add(
                CardEntry(
                    user_id=user_id,
                    source_key=f"ai:{number}",
                    kind="ai",
                    title=f"Entry {number}",
                    status="someday",
                    someday_at=base + timedelta(minutes=number),
                )
            )
    view_id = start_someday_view(user_id)
    first, more = get_someday_page(user_id, view_id, 1)
    assert len(first) == 10 and more
    assert advance_someday_view(user_id, view_id, 1, first[-1].id)
    second, more = get_someday_page(user_id, view_id, 2)
    assert len(second) == 10 and more
    removed_id = second[0].id
    assert choose_card(77, removed_id, "done")[0]
    refreshed, more = get_someday_page(user_id, view_id, 2)
    assert len(refreshed) == 10 and more
    assert removed_id not in {card.id for card in refreshed}
    assert advance_someday_view(user_id, view_id, 2, refreshed[-1].id)
    third, more = get_someday_page(user_id, view_id, 3)
    assert len(third) == 2 and not more
    assert not ({card.id for card in first} & {card.id for card in refreshed})
    assert not ({card.id for card in refreshed} & {card.id for card in third})
    back, _ = get_someday_page(user_id, view_id, 1)
    assert [card.id for card in back] == [card.id for card in first]


def test_someday_command_more_back_and_finish_in_one_message(tmp_path, monkeypatch):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        user = User(chat_id=77)
        session.add(user)
        session.flush()
        user_id = user.id
        for number in range(12):
            session.add(
                CardEntry(
                    user_id=user_id,
                    source_key=f"ai:{number}",
                    kind="ai",
                    title=f"Entry {number}",
                    body="Long answer",
                    status="someday",
                    someday_at=datetime.now(timezone.utc) + timedelta(minutes=number),
                )
            )
    monkeypatch.setattr(bot_module, "_ensure_user_id", lambda _message: user_id)
    monkeypatch.setattr(bot_module, "_is_allowed", lambda _chat_id: True)

    class Message:
        chat = SimpleNamespace(id=77)

        def __init__(self):
            self.text = ""
            self.markup = None
            self.answer = AsyncMock(side_effect=self._answer)
            self.edit_text = AsyncMock(side_effect=self._edit)

        async def _answer(self, text, reply_markup):
            self.text, self.markup = text, reply_markup

        async def _edit(self, text, reply_markup):
            self.text, self.markup = text, reply_markup

    class Callback:
        def __init__(self, message, data):
            self.message = message
            self.data = data
            self.bot = SimpleNamespace(send_message=AsyncMock())
            self.answer = AsyncMock()

    message = Message()
    asyncio.run(bot_module.cmd_someday(message))
    assert "страница 1" in message.text
    more = message.markup.inline_keyboard[-1][-1].callback_data
    assert more.startswith("sm:m:")
    asyncio.run(bot_module.cb_someday(Callback(message, more)))
    assert "страница 2" in message.text
    assert len(message.markup.inline_keyboard) == 3  # two cards and navigation
    back = message.markup.inline_keyboard[-1][0].callback_data
    asyncio.run(bot_module.cb_someday(Callback(message, back)))
    assert "страница 1" in message.text
    first_action = message.markup.inline_keyboard[0][0].callback_data
    asyncio.run(bot_module.cb_someday(Callback(message, first_action)))
    assert "страница 1" in message.text
    with session_scope() as session:
        assert session.query(CardEntry).filter(CardEntry.status == "done").count() == 1


def test_card_choice_saves_before_deleting_all_ai_messages(tmp_path, monkeypatch):
    init_engine(tmp_path / "bot.sqlite")
    with session_scope() as session:
        session.add(User(chat_id=77))
    card_id = create_ai_card(77, "AI", "https://example.com", "Long answer")
    register_card_message(card_id, 77, 10)
    register_card_message(card_id, 77, 11)
    monkeypatch.setattr(bot_module, "_is_allowed", lambda _chat_id: True)
    message = SimpleNamespace(chat=SimpleNamespace(id=77), message_id=10, delete=AsyncMock())
    bot = SimpleNamespace(delete_message=AsyncMock())
    callback = SimpleNamespace(
        message=message,
        data=f"card:someday:{card_id}",
        bot=bot,
        answer=AsyncMock(),
    )
    asyncio.run(bot_module.cb_card_choice(callback))
    with session_scope() as session:
        assert session.get(CardEntry, card_id).status == "someday"
    message.delete.assert_awaited_once()
    bot.delete_message.assert_awaited_once_with(chat_id=77, message_id=11)
