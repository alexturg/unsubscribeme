# UnsubscribeMe

Telegram-бот для мониторинга YouTube RSS и событийных источников (JSON/ICS) с фильтрацией, режимами доставки и AI-функциями.

## Содержание

- [Что умеет бот](#что-умеет-бот)
- [Как это работает](#как-это-работает)
- [Быстрый старт](#быстрый-старт)
- [Конфигурация `.env`](#конфигурация-env)
- [Режимы доставки](#режимы-доставки)
- [Команды Telegram](#команды-telegram)
- [Форматы источников событий](#форматы-источников-событий)
- [Веб-интерфейс](#веб-интерфейс)
- [Запуск в Docker](#запуск-в-docker)
- [Тестирование](#тестирование)
- [Диагностика](#диагностика)
- [Безопасность и приватность](#безопасность-и-приватность)
- [Структура проекта](#структура-проекта)
- [Документация](#документация)
- [Лицензия](#лицензия)

## Что умеет бот

- Отслеживает YouTube-каналы, плейлисты и произвольные RSS-ленты.
- Поддерживает режимы доставки: `immediate`, `digest`, `on_demand`.
- Применяет фильтры по ключевым словам, regex, категориям и длительности.
- Работает с событиями из JSON/ICS и с ручным вводом (`/addevents`).
- Делает AI-суммаризацию:
  - YouTube-видео (`/ai`)
  - обычных веб-страниц (`/ai`)
  - экспорт аудио (`/audio`)
  - транскрипт в `.txt` (`/transcribe`, при необходимости через Whisper).
- Делает канал-скрининг на кликбейт/сомнительные тезисы (`/bullshit`) по shortlist последних видео.
- Имеет встроенный веб-интерфейс управления лентами.

## Как это работает

Бот запускает polling Telegram + планировщик задач:

1. Читает источники по расписанию.
2. Сохраняет элементы в SQLite.
3. Применяет правила фильтрации.
4. Отправляет уведомления в Telegram согласно режиму ленты.

Данные хранятся в SQLite (по умолчанию `data/bot.sqlite`).

## Быстрый старт

### 1) Требования

- Python 3.9+
- Telegram Bot Token
- Для AI/Whisper: OpenAI API key
- Для Whisper-потока: `yt-dlp` (или укажите путь в `AI_SUMMARIZER_WHISPER_YTDLP_BINARY`)

### 2) Установка

```bash
git clone https://github.com/alexturg/unsubscribeme.git
cd unsubscribeme
pip install .
```

### 3) Настройка окружения

```bash
cp .env.example .env
```

Заполните минимум:

```dotenv
TELEGRAM_BOT_TOKEN=...
ALLOWED_CHAT_IDS=123456789
TZ=Asia/Almaty
OPENAI_API_KEY=...   # если используете /ai, /audio, /transcribe, /bullshit
```

### 4) Запуск

```bash
unsubscribeme
```

### 5) Первый вход

1. Напишите боту `/start`.
2. Добавьте источник через Telegram-команду или веб-интерфейс.
3. Проверьте список лент командой `/list`.

## Конфигурация `.env`

### Обязательные переменные

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `ALLOWED_CHAT_IDS` | Список разрешённых `chat_id` через запятую |

### Базовые параметры

| Переменная | По умолчанию | Описание |
|---|---:|---|
| `TZ` | `UTC` | Часовой пояс по умолчанию |
| `DB_PATH` | `data/bot.sqlite` | Путь к SQLite БД |
| `DEFAULT_POLL_INTERVAL_MIN` | `10` | Интервал опроса лент (мин) |
| `DIGEST_DEFAULT_TIME` | `20:00` | Время дайджеста по умолчанию |
| `BACKFILL_ON_START_N` | `10` | Backfill N последних записей на старте |
| `HIDE_FUTURE_VIDEOS` | `false` | Скрывать видео с будущим временем публикации |
| `WEB_HOST` | `127.0.0.1` | Хост встроенного веб-интерфейса |
| `WEB_PORT` | `8080` | Порт встроенного веб-интерфейса |

### AI и суммаризация

| Переменная | По умолчанию | Описание |
|---|---:|---|
| `OPENAI_API_KEY` | `None` | Ключ OpenAI |
| `AI_SUMMARIZER_MODE` | `openai` | Режим суммаризации: `openai` или `extractive` |
| `AI_SUMMARIZER_OPENAI_MODEL` | `gpt-4.1-mini` | Модель OpenAI |
| `AI_SUMMARIZER_LANGUAGES` | `ru,en` | Приоритет языков субтитров |
| `AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_URLS` | `` | Список прокси (через запятую/новые строки) для `youtube-transcript-api` |
| `AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_URL` | `` | URL plain-text списка прокси (например `fresh-proxy-list/http.txt`) |
| `AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_TIMEOUT_SEC` | `8` | Таймаут скачивания списка прокси |
| `AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_MAX_TRIES` | `6` | Сколько прокси пробовать после неудачной прямой попытки |
| `AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_REQUEST_TIMEOUT_SEC` | `8` | Таймаут одного HTTP-запроса к `youtube-transcript-api` |
| `AI_SUMMARIZER_MAX_SENTENCES` | `7` | Макс. число предложений в summary |
| `AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS` | `0` | Лимит слов входа (`0` = без лимита) |
| `AI_SUMMARIZER_SAVE_OUTPUT_FILES` | `false` | Сохранять артефакты summary на диск |
| `AI_SUMMARIZER_TIMEOUT_SEC` | `600` | Таймаут суммаризации |
| `AI_SUMMARIZER_OUTPUT_DIR` | `data/ai_summaries` | Папка для артефактов |
| `AI_BULLSHIT_PROMPT_PATH` | `data/prompts/bullshit_detector_v2.txt` | Путь к системному промпту `/bullshit` |
| `AI_BULLSHIT_OPENAI_MODEL` | `gpt-4.1-mini` | Модель OpenAI для `/bullshit` |
| `AI_BULLSHIT_MAX_VIDEOS` | `15` | Сколько последних видео сканировать перед shortlist |
| `AI_BULLSHIT_TOP_K` | `5` | Сколько подозрительных видео анализировать глубоко |
| `AI_BULLSHIT_FETCH_TIMEOUT_SEC` | `20` | Таймаут получения YouTube RSS для `/bullshit` |
| `AI_BULLSHIT_SUMMARY_SENTENCES` | `10` | Лимит пунктов суммаризации на видео в `/bullshit` |
| `AI_BULLSHIT_SUMMARY_MAX_INPUT_WORDS` | `1600` | Бюджет слов на входе суммаризации видео |
| `AI_BULLSHIT_OPENAI_MAX_OUTPUT_TOKENS` | `2200` | Макс. токенов финального отчёта `/bullshit` |

### Веб-страницы в `/ai`

| Переменная | По умолчанию | Описание |
|---|---:|---|
| `AI_SUMMARIZER_WEB_OPENAI_MAX_INPUT_WORDS` | `1400` | Бюджет слов для OpenAI по веб-страницам |
| `AI_SUMMARIZER_WEB_FETCH_TIMEOUT_SEC` | `15` | Таймаут загрузки страницы |
| `AI_SUMMARIZER_WEB_MAX_RESPONSE_BYTES` | `2000000` | Макс. размер загружаемой страницы |
| `AI_SUMMARIZER_WEB_MAX_EXTRACTED_WORDS` | `4500` | Лимит слов после очистки HTML |

### YouTube fallback + Whisper

| Переменная | По умолчанию | Описание |
|---|---:|---|
| `AI_SUMMARIZER_YOUTUBE_CONTEXT_FETCH_TIMEOUT_SEC` | `15` | Таймаут загрузки YouTube-страницы |
| `AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_HTML_BYTES` | `2500000` | Макс. размер HTML для fallback |
| `AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_DESCRIPTION_WORDS` | `220` | Лимит слов описания |
| `AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_COMMENTS` | `12` | Макс. комментариев |
| `AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_COMMENT_WORDS` | `36` | Лимит слов на комментарий |
| `AI_SUMMARIZER_YOUTUBE_CONTEXT_OPENAI_MAX_INPUT_WORDS` | `900` | Бюджет слов в fallback-суммаризации |
| `AI_SUMMARIZER_WHISPER_MODEL` | `whisper-1` | Модель транскрипции |
| `AI_SUMMARIZER_WHISPER_MAX_AUDIO_MB` | `24` | Лимит размера аудио для Whisper |
| `AI_SUMMARIZER_WHISPER_DOWNLOAD_TIMEOUT_SEC` | `240` | Таймаут загрузки аудио |
| `AI_SUMMARIZER_WHISPER_YTDLP_BINARY` | `yt-dlp` | Бинарник `yt-dlp` |
| `AI_AUDIO_EXPORT_MAX_BYTES` | `50331648` | Лимит размера файла для `/audio` |

Пример для авто-ротации публичных HTTP-прокси:

```env
AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_URL=https://vakhov.github.io/fresh-proxy-list/http.txt
AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_MAX_TRIES=2
AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_REQUEST_TIMEOUT_SEC=6
```

## Режимы доставки

- `immediate`  
  Новые элементы отправляются сразу после обнаружения.
- `digest`  
  Элементы копятся и отправляются пачкой в заданное время (`digest_time_local`).
- `on_demand`  
  Элементы сохраняются, но не отправляются автоматически. Используйте `/digest <feed_id|all>`.

## Команды Telegram

| Команда | Формат | Назначение |
|---|---|---|
| `/start` | `/start` | Регистрация и ссылка на веб-интерфейс |
| `/ai` | `/ai <youtube_url_or_video_id_or_page_url> [фокус]` | AI-суммаризация видео или веб-страницы |
| `/audio` | `/audio <youtube_url_or_video_id>` | Экспорт аудио YouTube-видео |
| `/transcribe` | `/transcribe <youtube_url_or_video_id>` | Транскрипт в `.txt` (с Whisper при необходимости) |
| `/bullshit` | `/bullshit <youtube_channel_url_or_channel_id> [videos=15] [top=5]` | Детектор кликбейта/сомнительных заявлений по shortlist последних видео канала |
| `/youtube` | `/youtube <youtube_link> [mode=...] [label=...] [interval=10] [time=HH:MM]` | Добавление YouTube-канала по ссылке |
| `/channel` | `/channel <channel_id> [mode=...] [label=...] [interval=10] [time=HH:MM]` | Добавление канала по `channel_id` |
| `/playlist` | `/playlist <playlist_id> [mode=...] [label=...] [interval=10] [time=HH:MM]` | Добавление плейлиста по `playlist_id` |
| `/addfeed` | `/addfeed <url> [mode=...] [label=...] [interval=10] [time=HH:MM]` | Добавление RSS URL |
| `/addeventsource` | `/addeventsource <url> [type=json\|ics] [label=...] [interval=1]` | Источник событий JSON/ICS |
| `/addics` | `/addics <url> [label=...] [interval=1]` | Быстрое добавление ICS |
| `/addevents` | `/addevents [feed=<id>\|<id>] [label=...] [interval=1]` + строки событий | Массовый импорт событий из текста |
| `/list` | `/list` | Список лент |
| `/remove` | `/remove <feed_id>` | Полное удаление ленты |
| `/setmode` | `/setmode <feed_id> <mode> [HH:MM]` | Смена режима ленты |
| `/setfilter` | `/setfilter <feed_id> <json>` | Установка фильтров |
| `/digest` | `/digest <feed_id\|all>` | Ручной запуск дайджеста |
| `/mute` | `/mute <feed_id>` | Временно отключить ленту |
| `/unmute` | `/unmute <feed_id>` | Включить ленту обратно |

Пример фильтра:

```text
/setfilter 1 {"include_keywords":["обзор"],"exclude_keywords":["стрим"]}
```

## Форматы источников событий

### JSON (`/addeventsource ... type=json`)

Допустимы:

- массив событий `[]`
- объект с полем `events: []`

Обязательные поля события:

- `title`
- `link` или `url`
- `start_at`

Рекомендуется:

- `id` (стабильный уникальный идентификатор)

Пример:

```json
{
  "events": [
    {
      "id": "okko-2026-02-10-women-short",
      "title": "Фигурное катание. Женщины, короткая программа",
      "link": "https://okko.sport/some/stream",
      "start_at": "2026-02-10T19:30:00+03:00"
    }
  ]
}
```

### ICS (`/addics` или `/addeventsource ... type=ics`)

- Поддерживается стандартный `.ics` с `VEVENT`.
- Время старта: `DTSTART`.
- Заголовок: `SUMMARY`.
- Ссылка: `URL`, либо первая ссылка из `DESCRIPTION`, иначе URL самой ленты.
- `webcal://` автоматически нормализуется в `https://`.

### Ручной импорт (`/addevents`)

Разделитель фиксированный: `;`

```text
/addevents label=okko interval=1
2026-02-10T19:30:00+03:00;Женщины. Короткая программа;https://okko.sport/...
2026-02-10 21:00;Мужчины. Короткая программа;https://okko.sport/...
```

## Веб-интерфейс

После `/start` бот присылает ссылку вида:

```text
http://<WEB_HOST>:<WEB_PORT>/u/<chat_id>
```

Возможности UI:

- Добавление/редактирование/отключение лент.
- Просмотр последних элементов.
- Настройка правил фильтрации.

Важно:

- доступ к странице основан на `chat_id` в URL;
- не публикуйте ссылку и не открывайте UI наружу без реверс-прокси/ограничений доступа;
- в продакшене задавайте безопасный сетевой контур (например, private network + VPN).

## Запуск в Docker

В проекте есть `Dockerfile`.

Пример:

```bash
docker build -t unsubscribeme:latest .
docker run --rm \
  --name unsubscribeme \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  unsubscribeme:latest
```

## Тестирование

```bash
pip install .[test]
pytest
```

## Диагностика

- `Доступ запрещен.`  
  Проверьте `ALLOWED_CHAT_IDS` и ваш реальный `chat_id`.
- `/youtube` не смог определить `channel_id`  
  Используйте `/channel <channel_id>` напрямую.
- `/ai` или `/transcribe` не работают  
  Проверьте `OPENAI_API_KEY` и доступность `yt-dlp` для Whisper-потока.
- `RequestBlocked` / `IpBlocked` при субтитрах YouTube  
  Включите ротацию прокси через `AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_URL` (или `..._PROXY_URLS`) и увеличьте `AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_MAX_TRIES`.
- Веб-интерфейс не открывается  
  Проверьте `WEB_HOST`/`WEB_PORT` и сетевую доступность хоста.
- Бот не стартует после деплоя  
  Смотрите логи: `journalctl -u unsubscribeme -f`.

## Безопасность и приватность

- Локальный `.env` не должен попадать в Git.
- Рабочая БД `data/bot.sqlite` содержит пользовательские данные и не должна коммититься.
- В репозитории хранится только демо-БД: `data/bot.demo.sqlite` (синтетические данные).
- Перед публикацией форков/архивов проверяйте репозиторий на секреты и персональные данные.
- Для `/ai` по веб-страницам запрещены приватные/локальные адреса; принимаются только `http/https`.

## Структура проекта

```text
src/rssbot/            # Логика бота, scheduler, RSS, веб, AI
src/utils/             # Вспомогательные утилиты
tests/                 # Автотесты
data/                  # Локальные данные SQLite (runtime)
deploy/systemd/        # Unit-файл для systemd
docs/                  # Дополнительная документация
```

## Документация

- Архитектура: `docs/telegram_youtube_rss_bot_architecture.md`
- Деплой через systemd: `docs/DEPLOY_SYSTEMD.md`
- Unit-файл: `deploy/systemd/unsubscribeme.service`

## Лицензия

Проект распространяется под лицензией GNU GPLv3. См. файл `LICENSE`.
