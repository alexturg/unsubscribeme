from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from aiohttp import web
from html import escape
from datetime import datetime, timezone

from .config import Settings
from .db import Feed, Session, User, Item, Delivery, FeedBaseline, FeedRule, session_scope
from .rules import Content, matches_rules
from .rss import compute_available_at
from .scheduler import BotScheduler


@dataclass
class WebDeps:
    settings: Settings
    scheduler: BotScheduler


DEPS: Optional[WebDeps] = None


def set_deps(settings: Settings, scheduler: BotScheduler) -> None:
    global DEPS
    DEPS = WebDeps(settings=settings, scheduler=scheduler)


def _ensure_user_by_chat_id(chat_id: int) -> int:
    assert DEPS is not None
    with session_scope() as s:
        user = s.query(User).filter(User.chat_id == chat_id).first()
        if user:
            return user.id
        user = User(chat_id=chat_id, tz=DEPS.settings.TZ)
        s.add(user)
        s.flush()
        return user.id


def _normalize_ics_url(url: str) -> str:
    value = (url or "").strip()
    if value.lower().startswith("webcal://"):
        return "https://" + value[len("webcal://") :]
    return value


def _html_page(title: str, body: str) -> web.Response:
    html = f"""
    <!doctype html>
    <html lang="ru">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{title}</title>
      <style>
        body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }}
        header {{ margin-bottom: 1.5rem; }}
        h1 {{ font-size: 1.4rem; margin: 0 0 .5rem; }}
        form {{ margin-bottom: 1rem; padding: .75rem; border: 1px solid #ddd; border-radius: 8px; }}
        label {{ display: inline-block; min-width: 140px; }}
        input[type=text], input[type=number] {{ padding: .25rem .4rem; }}
        select {{ padding: .25rem .4rem; }}
        .row {{ margin: .25rem 0; }}
        .feeds {{ margin-top: 1rem; }}
        .feed {{ padding: .6rem; border: 1px solid #eee; border-radius: 8px; margin: .5rem 0; }}
        .btn {{ padding: .35rem .6rem; background: #0d6efd; color: #fff; border: none; border-radius: 6px; cursor: pointer; }}
        .btn.gray {{ background: #6c757d; }}
        .btn.red {{ background: #dc3545; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: .5rem 1rem; }}
        small {{ color: #666; }}
        .feed.disabled {{ opacity: .65; background: #fafafa; }}
        .badge {{ display: inline-block; padding: 2px 6px; border-radius: 6px; font-size: .75rem; margin-left: .4rem; }}
        .badge.gray {{ background: #e9ecef; color: #333; }}
        ul.preview {{ margin: .3rem 0 .25rem 1.1rem; padding: 0; }}
        ul.preview li {{ margin: .15rem 0; }}
        .rules {{ margin-top: .5rem; padding-top: .5rem; border-top: 1px dashed #ddd; }}
        .rules .grid {{ grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
        .rules label {{ min-width: 160px; }}
      </style>
    </head>
    <body>
      <header>
        <h1>{title}</h1>
        <p><small>Подсказка: вставьте идентификатор канала/плейлиста YouTube или полный URL RSS, выберите режим и сохраните.</small></p>
      </header>
      {body}
    </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def index(request: web.Request) -> web.Response:
    chat_id = request.query.get("chat_id")
    if chat_id and chat_id.isdigit():
        raise web.HTTPFound(location=f"/u/{chat_id}")
    body = """
    <form method="get" action="/">
      <div class="row"><label>Ваш chat_id:</label><input type="text" name="chat_id" required></div>
      <button class="btn" type="submit">Открыть настройки</button>
    </form>
    """
    return _html_page("Настройки лент", body)


def _user_feeds(s: Session, user_id: int) -> list[Feed]:
    return s.query(Feed).filter(Feed.user_id == user_id).order_by(Feed.id.asc()).all()


async def user_page(request: web.Request) -> web.Response:
    assert DEPS is not None
    chat_id_str = request.match_info.get("chat_id")
    if not chat_id_str or not chat_id_str.isdigit():
        raise web.HTTPBadRequest(text="Invalid chat_id")
    chat_id = int(chat_id_str)
    user_id = _ensure_user_by_chat_id(chat_id)

    with session_scope() as s:
        feeds = _user_feeds(s, user_id)

    show_all = (request.query.get("show") == "all")
    if not show_all:
        feeds = [f for f in feeds if f.enabled]

    add_form = f"""
    <h2>Добавить ленту</h2>
    <form method="post" action="/u/{chat_id}/add">
      <div class="grid">
        <div><label>Тип</label>
          <select name="kind">
            <option value="channel">YouTube channel_id</option>
            <option value="playlist">YouTube playlist_id</option>
            <option value="url">URL RSS</option>
            <option value="ics">ICS calendar URL</option>
          </select>
        </div>
        <div><label>Значение</label><input type="text" name="value" required placeholder="UC... или PL... или https://..."></div>
        <div><label>Режим</label>
          <select name="mode">
            <option value="immediate">immediate</option>
            <option value="digest">digest</option>
            <option value="on_demand">on_demand</option>
          </select>
        </div>
        <div><label>Метка</label><input type="text" name="label" placeholder="опционально"></div>
        <div><label>Интервал (мин)</label><input type="number" name="interval" value="{DEPS.settings.DEFAULT_POLL_INTERVAL_MIN}" min="1"></div>
        <div><label>Время дайджеста</label><input type="text" name="time" placeholder="HH:MM" value="{DEPS.settings.DIGEST_DEFAULT_TIME}"></div>
      </div>
      <div class="row" style="margin-top:.5rem"><button class="btn" type="submit">Добавить</button></div>
    </form>
    """

    items_html: list[str] = []
    for f in feeds:
        safe_label = escape(f.label or "", quote=True)
        display_name = f.label or f.name or f.url
        safe_display = escape(display_name, quote=True)
        with session_scope() as s:
            its = (
                s.query(Item)
                .filter(Item.feed_id == f.id)
                .order_by(Item.published_at.desc().nullslast(), Item.id.desc())
                .limit(50)
                .all()
            )
            rule = s.query(FeedRule).filter(FeedRule.feed_id == f.id).first()
        preview_items: list[str] = []
        settings = Settings()
        now_utc = datetime.now(timezone.utc)
        for it in its:
            # Apply future-availability filter if enabled
            if settings.HIDE_FUTURE_VIDEOS:
                available_at = compute_available_at(it.title or "", it.published_at)
                if available_at and now_utc < available_at:
                    continue
            # Apply content rules
            content = Content(title=it.title or "", categories=it.categories, duration_sec=it.duration_sec)
            if not matches_rules(content, rule):
                continue
            t = escape(it.title or "(без названия)", quote=True)
            link = escape(it.link or "", quote=True)
            when = it.published_at.strftime("%Y-%m-%d") if it.published_at else ""
            preview_items.append(
                f"<li><a target=\"_blank\" rel=\"noopener\" href=\"{link}\">{t}</a> <small>{when}</small></li>"
            )
            if len(preview_items) >= 10:
                break

        if preview_items:
            preview_html = '<ul class="preview">' + ''.join(preview_items) + '</ul>'
        else:
            preview_html = '<ul class="preview"><li><small>Нет элементов</small></li></ul>'
        feed_cls = "feed disabled" if not f.enabled else "feed"
        status_badge = '<span class="badge gray">Отключено</span>' if not f.enabled else ""

        items_html.append(
            f"""
            <div class="{feed_cls}">
              <form method="post" action="/u/{chat_id}/feed/{f.id}/update">
                <div class="grid">
                  <div><label>ID</label><span>#{f.id}</span></div>
                  <div><label>Метка</label><input type="text" name="label" value="{safe_label}"></div>
                  <div><label>Mode</label>
                    <select name="mode">{_mode_options(f.mode)}</select>
                  </div>
                  <div><label>Интервал (мин)</label><input type="number" name="interval" value="{f.poll_interval_min}" min="1"></div>
                  <div><label>Дайджест время</label><input type="text" name="time" value="{f.digest_time_local or ''}" placeholder="HH:MM"></div>
                  <div><label>Включено</label><select name="enabled">{_bool_options(f.enabled)}</select></div>
                </div>
                <div class="row" style="margin-top:.5rem">
                  <button class="btn" type="submit">Сохранить</button>
                  <button class="btn gray" formaction="/u/{chat_id}/feed/{f.id}/toggle" formmethod="post" type="submit">{('Выключить' if f.enabled else 'Включить')}</button>
                  <button class="btn red" formaction="/u/{chat_id}/feed/{f.id}/remove" formmethod="post" type="submit" onclick="return confirm('Отключить ленту окончательно? Это действие необратимо.');">Отключить</button>
                </div>
                <div class="row"><strong>{safe_display}</strong>{status_badge}</div>
                {preview_html}
              </form>
              <form class="rules" method="post" action="/u/{chat_id}/feed/{f.id}/rules">
                <div class="grid">
                  <div><label>Включать ключевые</label><input type="text" name="include_keywords" value="{escape(', '.join(rule.include_keywords) if rule and rule.include_keywords else '', quote=True)}" placeholder="через запятую"></div>
                  <div><label>Исключать ключевые</label><input type="text" name="exclude_keywords" value="{escape(', '.join(rule.exclude_keywords) if rule and rule.exclude_keywords else '', quote=True)}"></div>
                  <div><label>Включать regex</label><input type="text" name="include_regex" value="{escape(', '.join(rule.include_regex) if rule and rule.include_regex else '', quote=True)}"></div>
                  <div><label>Исключать regex</label><input type="text" name="exclude_regex" value="{escape(', '.join(rule.exclude_regex) if rule and rule.exclude_regex else '', quote=True)}"></div>
                  <div><label>Категории</label><input type="text" name="categories" value="{escape(', '.join(rule.categories) if rule and rule.categories else '', quote=True)}"></div>
                  <div><label>Мин. длит., сек</label><input type="number" name="min_duration_sec" value="{(rule.min_duration_sec if rule and rule.min_duration_sec is not None else '') if rule else ''}" min="0"></div>
                  <div><label>Макс. длит., сек</label><input type="number" name="max_duration_sec" value="{(rule.max_duration_sec if rule and rule.max_duration_sec is not None else '') if rule else ''}" min="0"></div>
                  <div><label>Требовать все</label><input type="checkbox" name="require_all" {('checked' if rule and rule.require_all else '')}></div>
                  <div><label>Учитывать регистр</label><input type="checkbox" name="case_sensitive" {('checked' if rule and rule.case_sensitive else '')}></div>
                </div>
                <div class="row" style="margin-top:.5rem">
                  <button class="btn" type="submit">Сохранить фильтры</button>
                  <button class="btn gray" formaction="/u/{chat_id}/feed/{f.id}/rules/clear" formmethod="post" type="submit" onclick="return confirm('Очистить все правила для этой ленты?');">Сбросить</button>
                </div>
              </form>
            </div>
            """
        )

    toggle_link = f"/u/{chat_id}" + ("" if show_all else "?show=all")
    toggle_text = "Скрыть отключённые" if show_all else "Показать отключённые"
    toggle_btn = f"<div class=\"row\"><a class=\"btn gray\" href=\"{toggle_link}\">{toggle_text}</a></div>"
    body = add_form + ("<div class=\"feeds\"><h2>Мои ленты</h2>" + toggle_btn + "\n".join(items_html) + "</div>" if items_html else "<p>Лент пока нет.</p>")
    return _html_page("Настройки лент", body)


def _mode_options(selected: str) -> str:
    values = ["immediate", "digest", "on_demand"]
    return "\n".join(
        f"<option value=\"{v}\"{' selected' if v == selected else ''}>{v}</option>" for v in values
    )


def _bool_options(enabled: bool) -> str:
    return (
        ("<option value=\"true\" selected>True</option><option value=\"false\">False</option>")
        if enabled
        else ("<option value=\"true\">True</option><option value=\"false\" selected>False</option>")
    )


async def add_feed(request: web.Request) -> web.Response:
    assert DEPS is not None
    chat_id_str = request.match_info.get("chat_id")
    if not chat_id_str or not chat_id_str.isdigit():
        raise web.HTTPBadRequest(text="Invalid chat_id")
    chat_id = int(chat_id_str)
    user_id = _ensure_user_by_chat_id(chat_id)

    form = await request.post()
    kind = (form.get("kind") or "url").strip()
    value = (form.get("value") or "").strip()
    mode = (form.get("mode") or "immediate").strip()
    label = (form.get("label") or None) or None
    interval = form.get("interval") or str(DEPS.settings.DEFAULT_POLL_INTERVAL_MIN)
    digest_time = (form.get("time") or "").strip() or None
    try:
        interval_i = max(1, int(interval))
    except Exception:
        interval_i = DEPS.settings.DEFAULT_POLL_INTERVAL_MIN

    if not value:
        raise web.HTTPBadRequest(text="value is required")

    feed_type = "youtube"
    if kind == "channel":
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={value}"
    elif kind == "playlist":
        url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={value}"
    elif kind == "ics":
        url = _normalize_ics_url(value)
        feed_type = "event_ics"
        mode = "immediate"
        digest_time = None
    else:
        url = value

    # Upsert feed by URL per user
    with session_scope() as s:
        existing = (
            s.query(Feed).filter(Feed.user_id == user_id, Feed.url == url).order_by(Feed.id.asc()).first()
        )
        if existing:
            existing.enabled = True
            existing.mode = mode
            existing.type = feed_type
            existing.label = label
            existing.poll_interval_min = interval_i
            if mode == "digest":
                if digest_time:
                    existing.digest_time_local = digest_time
                elif not existing.digest_time_local:
                    existing.digest_time_local = DEPS.settings.DIGEST_DEFAULT_TIME
            else:
                existing.digest_time_local = None
            s.flush()
            feed_id = existing.id
        else:
            digest_time_local = None
            if mode == "digest":
                digest_time_local = digest_time or DEPS.settings.DIGEST_DEFAULT_TIME
            feed = Feed(
                user_id=user_id,
                url=url,
                type=feed_type,
                label=label,
                mode=mode,
                poll_interval_min=interval_i,
                digest_time_local=digest_time_local,
                enabled=True,
            )
            s.add(feed)
            s.flush()
            feed_id = feed.id

    # (Re)schedule
    DEPS.scheduler.schedule_feed_poll(feed_id, interval_i)
    raise web.HTTPFound(location=f"/u/{chat_id}")


async def update_feed(request: web.Request) -> web.Response:
    assert DEPS is not None
    chat_id_str = request.match_info.get("chat_id")
    feed_id_str = request.match_info.get("feed_id")
    if not chat_id_str or not chat_id_str.isdigit() or not feed_id_str or not feed_id_str.isdigit():
        raise web.HTTPBadRequest(text="Invalid params")
    chat_id = int(chat_id_str)
    feed_id = int(feed_id_str)
    form = await request.post()
    mode = (form.get("mode") or "immediate").strip()
    label = (form.get("label") or None) or None
    enabled_str = (form.get("enabled") or "true").lower()
    digest_time = (form.get("time") or "").strip() or None
    interval = form.get("interval") or "10"
    try:
        interval_i = max(1, int(interval))
    except Exception:
        interval_i = DEPS.settings.DEFAULT_POLL_INTERVAL_MIN

    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed:
            raise web.HTTPNotFound(text="Feed not found")
        # simple ownership check by chat_id
        user = s.get(User, feed.user_id)
        if not user or user.chat_id != int(chat_id):
            raise web.HTTPForbidden(text="Forbidden")
        feed.mode = mode
        feed.label = label
        feed.poll_interval_min = interval_i
        feed.enabled = enabled_str == "true"
        if mode == "digest":
            if digest_time:
                feed.digest_time_local = digest_time
            elif not feed.digest_time_local:
                feed.digest_time_local = DEPS.settings.DIGEST_DEFAULT_TIME
        else:
            feed.digest_time_local = None
        enabled = feed.enabled
    if enabled:
        DEPS.scheduler.schedule_feed_poll(feed_id, interval_i)
    else:
        DEPS.scheduler.unschedule_feed_poll(feed_id)
    raise web.HTTPFound(location=f"/u/{chat_id}")


async def toggle_feed(request: web.Request) -> web.Response:
    assert DEPS is not None
    chat_id_str = request.match_info.get("chat_id")
    feed_id_str = request.match_info.get("feed_id")
    if not chat_id_str or not chat_id_str.isdigit() or not feed_id_str or not feed_id_str.isdigit():
        raise web.HTTPBadRequest(text="Invalid params")
    chat_id = int(chat_id_str)
    feed_id = int(feed_id_str)
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed:
            raise web.HTTPNotFound(text="Feed not found")
        user = s.get(User, feed.user_id)
        if not user or user.chat_id != chat_id:
            raise web.HTTPForbidden(text="Forbidden")
        feed.enabled = not feed.enabled
        enabled = feed.enabled
        interval = feed.poll_interval_min
    if enabled:
        DEPS.scheduler.schedule_feed_poll(feed_id, interval)
    else:
        DEPS.scheduler.unschedule_feed_poll(feed_id)
    raise web.HTTPFound(location=f"/u/{chat_id}")


async def remove_feed(request: web.Request) -> web.Response:
    assert DEPS is not None
    chat_id_str = request.match_info.get("chat_id")
    feed_id_str = request.match_info.get("feed_id")
    if not chat_id_str or not chat_id_str.isdigit() or not feed_id_str or not feed_id_str.isdigit():
        raise web.HTTPBadRequest(text="Invalid params")
    chat_id = int(chat_id_str)
    feed_id = int(feed_id_str)
    # Fully delete feed and related data
    DEPS.scheduler.unschedule_feed_poll(feed_id)
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed:
            raise web.HTTPNotFound(text="Feed not found")
        user = s.get(User, feed.user_id)
        if not user or user.chat_id != chat_id:
            raise web.HTTPForbidden(text="Forbidden")
        # Remove rules and baseline
        if feed.rules is not None:
            s.delete(feed.rules)
        s.query(FeedBaseline).filter(FeedBaseline.feed_id == feed.id).delete(synchronize_session=False)
        # Remove deliveries and items
        s.query(Delivery).filter(Delivery.feed_id == feed.id).delete(synchronize_session=False)
        s.query(Item).filter(Item.feed_id == feed.id).delete(synchronize_session=False)
        # Finally remove feed
        s.delete(feed)
    raise web.HTTPFound(location=f"/u/{chat_id}")


def _parse_csv(val: Optional[str]) -> Optional[list[str]]:
    if not val:
        return None
    parts = [p.strip() for p in val.split(',')]
    parts = [p for p in parts if p]
    return parts or None


async def save_rules(request: web.Request) -> web.Response:
    assert DEPS is not None
    chat_id_str = request.match_info.get("chat_id")
    feed_id_str = request.match_info.get("feed_id")
    if not chat_id_str or not chat_id_str.isdigit() or not feed_id_str or not feed_id_str.isdigit():
        raise web.HTTPBadRequest(text="Invalid params")
    chat_id = int(chat_id_str)
    feed_id = int(feed_id_str)
    form = await request.post()

    include_keywords = _parse_csv(form.get('include_keywords'))
    exclude_keywords = _parse_csv(form.get('exclude_keywords'))
    include_regex = _parse_csv(form.get('include_regex'))
    exclude_regex = _parse_csv(form.get('exclude_regex'))
    categories = _parse_csv(form.get('categories'))
    min_duration = form.get('min_duration_sec')
    max_duration = form.get('max_duration_sec')
    try:
        min_duration_i = int(min_duration) if (min_duration and min_duration.strip() != '') else None
    except Exception:
        min_duration_i = None
    try:
        max_duration_i = int(max_duration) if (max_duration and max_duration.strip() != '') else None
    except Exception:
        max_duration_i = None
    require_all = form.get('require_all') is not None
    case_sensitive = form.get('case_sensitive') is not None

    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed:
            raise web.HTTPNotFound(text="Feed not found")
        user = s.get(User, feed.user_id)
        if not user or user.chat_id != chat_id:
            raise web.HTTPForbidden(text="Forbidden")
        rules = feed.rules or FeedRule(feed_id=feed.id)
        rules.include_keywords = include_keywords
        rules.exclude_keywords = exclude_keywords
        rules.include_regex = include_regex
        rules.exclude_regex = exclude_regex
        rules.categories = categories
        rules.min_duration_sec = min_duration_i
        rules.max_duration_sec = max_duration_i
        rules.require_all = require_all
        rules.case_sensitive = case_sensitive
        s.add(rules)

    raise web.HTTPFound(location=f"/u/{chat_id}")


async def clear_rules(request: web.Request) -> web.Response:
    assert DEPS is not None
    chat_id_str = request.match_info.get("chat_id")
    feed_id_str = request.match_info.get("feed_id")
    if not chat_id_str or not chat_id_str.isdigit() or not feed_id_str or not feed_id_str.isdigit():
        raise web.HTTPBadRequest(text="Invalid params")
    chat_id = int(chat_id_str)
    feed_id = int(feed_id_str)
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if not feed:
            raise web.HTTPNotFound(text="Feed not found")
        user = s.get(User, feed.user_id)
        if not user or user.chat_id != chat_id:
            raise web.HTTPForbidden(text="Forbidden")
        if feed.rules is not None:
            s.delete(feed.rules)
    raise web.HTTPFound(location=f"/u/{chat_id}")


def create_app(settings: Settings, scheduler: BotScheduler) -> web.Application:
    set_deps(settings, scheduler)
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/u/{chat_id}", user_page)
    app.router.add_post("/u/{chat_id}/add", add_feed)
    app.router.add_post("/u/{chat_id}/feed/{feed_id}/update", update_feed)
    app.router.add_post("/u/{chat_id}/feed/{feed_id}/toggle", toggle_feed)
    app.router.add_post("/u/{chat_id}/feed/{feed_id}/remove", remove_feed)
    app.router.add_post("/u/{chat_id}/feed/{feed_id}/rules", save_rules)
    app.router.add_post("/u/{chat_id}/feed/{feed_id}/rules/clear", clear_rules)
    return app
