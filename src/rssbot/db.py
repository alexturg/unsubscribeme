from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    tz: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    locale: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    feeds: Mapped[Iterable["Feed"]] = relationship("Feed", back_populates="user")


class Feed(Base):
    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default="youtube")
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    mode: Mapped[str] = mapped_column(
        Enum("immediate", "digest", "on_demand", name="feed_mode"), nullable=False, default="immediate"
    )
    digest_time_local: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    poll_interval_min: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    http_etag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    http_last_modified: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    last_poll_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_digest_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship("User", back_populates="feeds")
    rules: Mapped[Optional["FeedRule"]] = relationship("FeedRule", back_populates="feed", uselist=False)
    items: Mapped[Iterable["Item"]] = relationship("Item", back_populates="feed")


class FeedRule(Base):
    __tablename__ = "feed_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"), nullable=False, unique=True)

    include_keywords: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    exclude_keywords: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    include_regex: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    exclude_regex: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    require_all: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    case_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    categories: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    min_duration_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_duration_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    feed: Mapped[Feed] = relationship("Feed", back_populates="rules")


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("feed_id", "external_id", name="uq_feed_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)

    title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    link: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    author: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    categories: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    summary_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    duration_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    feed: Mapped[Feed] = relationship("Feed", back_populates="items")


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    channel: Mapped[str] = mapped_column(
        Enum("immediate", "digest", "on_demand", name="delivery_channel"), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(Enum("ok", "fail", name="delivery_status"), default="ok")
    error_message: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)


class CardEntry(Base):
    __tablename__ = "card_entries"
    __table_args__ = (UniqueConstraint("user_id", "source_key", name="uq_card_user_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    link: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    status_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    chosen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    someday_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class CardMessage(Base):
    __tablename__ = "card_messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_card_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)


class SomedayView(Base):
    __tablename__ = "someday_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    top_card_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    anchors: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=lambda: [0])
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeedBaseline(Base):
    __tablename__ = "feed_baselines"

    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"), primary_key=True)
    baseline_item_external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    baseline_published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    baseline_set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    feed: Mapped[Feed] = relationship("Feed")


class IcsContentState(Base):
    __tablename__ = "ics_content_states"

    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"), primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)


_SessionLocal: Optional[sessionmaker[Session]] = None
_engine: Optional[Engine] = None


def init_engine(db_path: Path) -> Engine:
    global _engine, _SessionLocal
    # Ensure directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    _migrate_legacy_deliveries(engine)
    _engine = engine
    _SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
        expire_on_commit=False,
    )
    return engine


def _migrate_legacy_deliveries(engine: Engine) -> None:
    """One-time, additive backfill. A successful old delivery is presumed checked."""
    version = "card_entries_v1_legacy_deliveries"
    with Session(engine) as session, session.begin():
        if session.get(SchemaMigration, version) is not None:
            return
        rows = (
            session.query(Delivery, Item, Feed.type)
            .outerjoin(Item, Delivery.item_id == Item.id)
            .outerjoin(Feed, Delivery.feed_id == Feed.id)
            .filter(Delivery.status == "ok")
            .order_by(Delivery.id.asc())
            .all()
        )
        seen: set[tuple[int, int]] = set()
        for delivery, item, feed_type in rows:
            identity = (delivery.user_id, delivery.item_id)
            if identity in seen:
                continue
            seen.add(identity)
            source_key = f"item:{delivery.item_id}"
            existing = session.query(CardEntry.id).filter(
                CardEntry.user_id == delivery.user_id, CardEntry.source_key == source_key
            ).first()
            if existing:
                continue
            is_event = (feed_type or "").startswith("event_")
            session.add(
                CardEntry(
                    user_id=delivery.user_id,
                    source_key=source_key,
                    kind="event" if is_event else "feed",
                    title=(item.title if item else None) or "(удалённая запись)",
                    link=item.link if item else None,
                    status="done",
                    status_source="legacy_delivery",
                    chosen_at=None,
                )
            )
        session.add(SchemaMigration(version=version))


@contextmanager
def session_scope() -> Iterable[Session]:
    if _SessionLocal is None:
        raise RuntimeError("DB session factory not initialized; call init_engine first")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
