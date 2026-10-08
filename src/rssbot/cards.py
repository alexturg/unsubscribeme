from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, or_

from .db import CardEntry, CardMessage, Feed, Item, SomedayView, User, session_scope


def choice_keyboard(
    card_id: int, rows: list[list[InlineKeyboardButton]] | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *(rows or []),
            [
                InlineKeyboardButton(text="✓", callback_data=f"card:done:{card_id}"),
                InlineKeyboardButton(text="Skip", callback_data=f"card:skipped:{card_id}"),
                InlineKeyboardButton(text="Someday", callback_data=f"card:someday:{card_id}"),
            ],
        ]
    )


def ensure_item_card(chat_id: int, item_id: int) -> int:
    with session_scope() as session:
        user = session.query(User).filter(User.chat_id == chat_id).first()
        item = session.get(Item, item_id)
        if user is None or item is None:
            raise ValueError("Card item or user not found")
        feed = session.get(Feed, item.feed_id)
        if feed is None or feed.user_id != user.id:
            raise ValueError("Card item belongs to another user")
        source_key = f"item:{item.id}"
        card = (
            session.query(CardEntry)
            .filter(CardEntry.user_id == user.id, CardEntry.source_key == source_key)
            .first()
        )
        if card is None:
            card = CardEntry(
                user_id=user.id,
                source_key=source_key,
                kind="event" if (feed.type or "").startswith("event_") else "feed",
                title=item.title or "(без названия)",
                link=item.link,
            )
            session.add(card)
            session.flush()
        return card.id


def create_ai_card(chat_id: int, title: str, link: str, body: str) -> int:
    with session_scope() as session:
        user = session.query(User).filter(User.chat_id == chat_id).first()
        if user is None:
            raise ValueError("Card user not found")
        card = CardEntry(
            user_id=user.id,
            source_key=f"ai:{uuid4().hex}",
            kind="ai",
            title=title[:500],
            link=link[:1000],
            body=body,
        )
        session.add(card)
        session.flush()
        return card.id


def register_card_message(card_id: int, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    with session_scope() as session:
        session.add(CardMessage(card_id=card_id, chat_id=chat_id, message_id=message_id))


def choose_card(chat_id: int, card_id: int, status: str) -> tuple[bool, list[int]]:
    if status not in {"done", "skipped", "someday"}:
        raise ValueError("Invalid card status")
    with session_scope() as session:
        user = session.query(User).filter(User.chat_id == chat_id).first()
        card = session.get(CardEntry, card_id)
        if user is None or card is None or card.user_id != user.id:
            return False, []
        now = datetime.now(timezone.utc)
        card.status = status
        card.status_source = "button"
        card.chosen_at = now
        if status == "someday":
            card.someday_at = now
        message_ids = [
            row[0]
            for row in session.query(CardMessage.message_id)
            .filter(CardMessage.card_id == card_id, CardMessage.chat_id == chat_id)
            .all()
        ]
        return True, message_ids


def _at_or_before(card: CardEntry):
    return or_(
        CardEntry.someday_at < card.someday_at,
        and_(CardEntry.someday_at == card.someday_at, CardEntry.id <= card.id),
    )


def _before(card: CardEntry):
    return or_(
        CardEntry.someday_at < card.someday_at,
        and_(CardEntry.someday_at == card.someday_at, CardEntry.id < card.id),
    )


def start_someday_view(user_id: int) -> int:
    with session_scope() as session:
        session.query(SomedayView).filter(SomedayView.user_id == user_id).delete(
            synchronize_session=False
        )
        top = (
            session.query(CardEntry)
            .filter(CardEntry.user_id == user_id, CardEntry.status == "someday")
            .order_by(CardEntry.someday_at.desc(), CardEntry.id.desc())
            .first()
        )
        view = SomedayView(user_id=user_id, top_card_id=top.id if top else None, anchors=[0])
        session.add(view)
        session.flush()
        return view.id


def get_someday_page(user_id: int, view_id: int, page: int) -> tuple[list[CardEntry], bool] | None:
    with session_scope() as session:
        view = session.get(SomedayView, view_id)
        if view is None or view.user_id != user_id or page < 1 or page > len(view.anchors):
            return None
        if view.top_card_id is None:
            return [], False
        top = session.get(CardEntry, view.top_card_id)
        if top is None:
            return None
        query = session.query(CardEntry).filter(
            CardEntry.user_id == user_id,
            CardEntry.status == "someday",
            _at_or_before(top),
        )
        anchor_id = view.anchors[page - 1]
        if anchor_id:
            anchor = session.get(CardEntry, anchor_id)
            if anchor is None or anchor.user_id != user_id:
                return None
            query = query.filter(_before(anchor))
        rows = query.order_by(CardEntry.someday_at.desc(), CardEntry.id.desc()).limit(11).all()
        return rows[:10], len(rows) > 10


def advance_someday_view(user_id: int, view_id: int, page: int, last_id: int) -> bool:
    with session_scope() as session:
        view = session.get(SomedayView, view_id)
        if view is None or view.user_id != user_id or page < 1 or page > len(view.anchors):
            return False
        current = get_someday_page(user_id, view_id, page)
        if current is None or not current[1] or not current[0] or current[0][-1].id != last_id:
            return False
        view.anchors = [*view.anchors[:page], last_id]
        return True
