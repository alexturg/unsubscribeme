# Telegram-бот для YouTube RSS: архитектура и план реализации

## Цели

- Получать новые видео из RSS-лент YouTube (каналы/плейлисты).
- Гибкая фильтрация: исключать/включать по правилам (ключевые слова, regex и пр.).
- Разные режимы доставки по ленте: сразу, вечерний дайджест, по запросу.
- Надёжная дедупликация и состояние между перезапусками.
- Безопасность (ограничение на конкретные чаты/пользователей), конфигурируемость, наблюдаемость.

---

## Высокоуровневая архитектура

Компоненты (Python, asyncio):

1) Telegram Bot (aiogram)
   - Обрабатывает команды пользователя (чат-бот).
   - Отправляет сообщения мгновенно и по расписанию (дайджесты).
   - Примитивная авторизация по whitelist chat_id.

2) Feed Poller (планировщик)
   - Периодически опрашивает ленты (APScheduler/aiocron). Разные интервалы на ленту.
   - Использует HTTP ETag/Last-Modified для экономии трафика.
   - Парсит RSS (feedparser) и сохраняет новые элементы.

3) Rule Engine (движок правил)
   - Применяет include/exclude по title/description/categories, regex, чувствительность к регистру.
   - Опционально: ограничения по длительности (при наличии метаданных), дата/время, автор/плейлист.

4) State & Dedup Store (SQLite + SQLAlchemy)
   - Схема БД для пользователей, лент, правил, элементов, доставок, состояний.
   - Гарантирует, что одно и то же видео не отправляется повторно не по делу.

5) Digest Composer
   - Собирает за период все новые элементы, прошедшие фильтры, формирует одно/несколько сообщений.
   - Настройки: время отправки, группировка по лентам, лимиты на объём.

6) Config & Secrets
   - `.env` для `TELEGRAM_BOT_TOKEN`, whitelist chat_ids, часовой пояс.
   - Конфиги лент/правил управляются командами бота и/или CLI.

7) Observability
   - Логи (structured), алерты на фатальные ошибки, метрики (опционально).

---

## Модель данных (SQLite)

- users
  - id (PK), chat_id (unique), tz, locale, created_at
- feeds
  - id (PK), user_id (FK), url, type (youtube|generic), name, label,
    mode (immediate|digest|on_demand), digest_time_local (HH:MM),
    poll_interval_min, enabled, created_at, http_etag, http_last_modified
- feed_rules
  - id (PK), feed_id (FK),
    include_keywords (json[]), exclude_keywords (json[]),
    include_regex (json[]), exclude_regex (json[]),
    require_all (bool), case_sensitive (bool), categories (json[]),
    min_duration_sec (int|null), max_duration_sec (int|null),
    created_at
- items
  - id (PK), feed_id (FK), external_id (например, YouTube videoId),
    title, link, author, published_at, categories (json[]), summary_hash,
    duration_sec (int|null), created_at
  - unique(feed_id, external_id)
- deliveries
  - id (PK), item_id (FK), feed_id (FK), user_id (FK),
    channel (immediate|digest|on_demand), sent_at, status (ok|fail),
    error_message (nullable)
- states (опционально)
  - id (PK), feed_id (FK), last_poll_at, last_digest_at

Заметки:
- Для YouTube RSS `external_id` — это videoId из link/entry.id.
- `summary_hash` помогает отлавливать обновления содержания.
- HTTP-кэш: поля `http_etag`, `http_last_modified` в `feeds`.

---

## Потоки данных

1) Опрашивание
- Планировщик вызывает fetch для каждой включенной ленты.
- HTTP запрос с ETag/Last-Modified; при 304 — пропускаем парсинг.
- Парсинг новых записей → upsert в `items` (по unique(feed_id, external_id)).

2) Фильтрация
- На этапе доставки (и/или при индексации) применяется Rule Engine.
- Порядок: сначала exclude, затем include. Режимы:
  - "черный список" (exclude имеет приоритет, include необязателен)
  - "белый список" (если include задан — пропускаем только подходящие)

3) Доставка
- immediate: при записи нового подходящего элемента — отправка сразу.
- digest: запись помечается как «ожидает дайджеста»; раз в день в `digest_time_local` формируется итог.
- on_demand: запись сохраняется; отправка только по команде.

4) Дедупликация
- Перед отправкой проверяем `deliveries`/`items` на наличие уже отправленного combination (user_id, feed_id, item_id, channel).

---

## Фильтры: примеры

- include_keywords: ["обзор", "release"], require_all=false
- exclude_keywords: ["стрим", "короткое"]
- include_regex: ["(?i)python\\s+tips"], case_sensitive=false
- categories: ["Music", "Education"]
- duration: min=60, max=3600 (если есть метаданные)

---

## Команды Telegram

- /start — регистрация пользователя, помощь и краткая инструкция.
- /addfeed <url> [mode=immediate|digest|on_demand] [label=...] — добавить ленту.
- /list — показать ленты: id, label, mode, статус, время дайджеста.
- /remove <feed_id> — удалить ленту.
- /setmode <feed_id> <mode> [time=HH:MM] — сменить режим, при digest задать локальное время.
- /setfilter <feed_id> — интерактивный мастер: include/exclude/regex/categories/duration.
- /digest [feed_id|all] — прислать дайджест сейчас.
- /mute <feed_id> | /unmute <feed_id> — временно отключить/включить ленту.
- /help — справка.

Безопасность: принимать команды только от chat_id из whitelist (env) или от пользователей, зарегистрированных ранее как владельцы.

---

## Технологический стек

- Python 3.11+
- aiogram (Telegram bot)
- feedparser (RSS)
- aiohttp (HTTP, ETag/Last-Modified)
- APScheduler (планировщик; альтернатива — aiocron)
- SQLAlchemy + SQLite (персистентность), Alembic (миграции)
- pydantic-settings (конфиг), python-dotenv
- pytest (тесты), mypy/ruff (качество кода)

Опционально:
- Redis (кэш/локи), Sentry (трекинг ошибок), Prometheus (метрики)

---

## План реализации (итерациями)

1) Бутстрап проекта
- Инициализация репо (Poetry/uv/pip-tools), базовая структура src/, конфиг через `.env`.
- Подключение aiogram; команда /start; whitelist chat_id.

2) База данных и модели
- SQLAlchemy модели: users, feeds, feed_rules, items, deliveries.
- Первые миграции (Alembic).

3) Парсер и опрашивание лент
- Функция fetch_rss(feed): HTTP с ETag/Last-Modified, парсинг feedparser.
- Маппинг YouTube RSS → item fields (videoId, title, link, published_at, author, categories).
- Сохранение новых items; хранение http_etag/last_modified в feeds.
- Планировщик APScheduler: job per feed с индивидуальным poll_interval_min.

4) Движок правил (Rule Engine)
- Нормализация текста (регистры, trim, unicode-normalize).
- Проверка exclude → include; unit-тесты на корректность.

5) Доставка: immediate
- Триггер отправки после сохранения нового item, прошедшего фильтры.
- Дедупликация `deliveries`; обработка ошибок Telegram (429, timeouts, retry c backoff).

6) Доставка: digest
- Хранить «неотправленные подходящие items» до `digest_time_local`.
- Джоб, который по TZ пользователя формирует и отправляет дайджест.
- Форматирование: заголовок, список с кнопками-ссылками; ограничения по длине сообщения.

7) Доставка: on_demand
- Команда /digest <feed_id>: собрать подходящее за период (например, за n дней) и отправить.

8) Команды управления
- /addfeed, /list, /remove, /setmode, /setfilter (пошаговый wizard), /mute, /unmute.
- Валидация URL (YouTube канал/плейлист) и автозаполнение name/label при первом fetch.

9) Наблюдаемость и устойчивость
- Структурированные логи; алерты на перманентные ошибки.
- Повторы при сетевых ошибках с jitter; таймауты HTTP.
- Rate limiting отправки сообщений.

10) Деплой и эксплуатация
- Dockerfile + docker-compose (volumes: db, logs).
- Systemd unit (альтернатива Docker) с перезапуском.
- Переменные окружения: TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS, TZ, DB_PATH.

---

## Детали реализации

- Расписание
  - APScheduler: BackgroundScheduler + AsyncIOExecutor.
  - Per-feed job для polling; отдельный daily job для дайджестов в `HH:MM` по TZ пользователя.
  - Для разных таймзон пользователей — агрегировать по минутам и запускать пачками.

- Формат сообщений
  - Immediate: «Новый ролик: <title>» + кнопка «Открыть» (link), опционально превью.
  - Digest: заголовок «Дайджест за <дата>», список «• <feed.label>: <title> (<hh:mm | YYYY‑MM‑DD>)».
  - Пагинация при превышении лимита длины; максимум N пунктов в одном сообщении.

- Добавление ленты
  - По умолчанию помечать последние K (например, 5) как уже увиденные (без отправки), чтобы не заспамить при первом запуске.
  - Опция «backfill=true» для осознанной отправки последних M элементов при добавлении.

- Производительность
  - Поллинг батчами, настраиваемые интервалы (например, 5–30 мин).
  - Ограничение одновременных fetch по семафору (aiohttp TCPConnector limit).

- Надёжность
  - Сохранение контрольных дат (last_poll_at/last_digest_at); idempotency в доставке.
  - Обработка 410/404 (удаленная лента) → автоотключение и уведомление.

---

## Конфигурация (пример .env)

```
TELEGRAM_BOT_TOKEN=123456:ABC...
ALLOWED_CHAT_IDS=11111111,22222222
TZ=Europe/Moscow
DB_PATH=/data/bot.sqlite
DEFAULT_POLL_INTERVAL_MIN=10
DIGEST_DEFAULT_TIME=20:00
```

---

## Тестирование

- Unit: Rule Engine (все ветки include/exclude/regex), парсер YouTube RSS (фикстуры с образцами XML).
- Integration (локально): цепочка fetch → фильтр → immediate/digest (с заглушкой Telegram API).
- Regression: дедупликация `deliveries`, граничные случаи лимитов сообщений.

---

## Роадмап (опции)

- Invidious/Piped как альтернативный источник (устойчивее превью/метаданные).
- YouTube Data API для длительности/тегов (при необходимости и наличии ключа).
- Веб-панель управления (FastAPI + простая UI форма) для лент и правил.
- Групповые дайджесты (bundles): объединять несколько лент в один блок.
- Экспорт/импорт конфигурации лент в YAML/JSON.
- Мультиюзерность (разделение владельцев лент, отдельные правила и расписания).
- Кэш превью/картинок, гибкое форматирование Markdown/HTML.

---

## Итог

Предложенная архитектура покрывает ключевые сценарии: мгновенная доставка, вечерние дайджесты и выдача по запросу; гибкая фильтрация; устойчивый опрос с дедупликацией и сохранением состояния. План реализации разбит на итерации, каждая из которых даёт рабочую ценность и упрощает проверку/доработку.

