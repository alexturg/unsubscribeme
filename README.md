# UnsubscribeMe — Telegram YouTube RSS Bot

Bot that fetches YouTube RSS feeds, filters by rules, and delivers updates:
- Modes per feed: `immediate`, `digest`, `on_demand`.
- Include/exclude keywords and regex, categories, duration.
- Daily digests at a chosen local time.

Quick start:
- Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN` and `ALLOWED_CHAT_IDS`.
- Repository includes `data/bot.demo.sqlite` with synthetic demo data; keep your real `data/bot.sqlite` local only.
- Python 3.9+ required.
- Install and run:
  - `pip install .`
  - `unsubscribeme`

Core commands:
- `/start` — register.
- `/ai <youtube_url_or_video_id_or_page_url> [дополнительный фокус]` — сделать AI-суммаризацию YouTube-видео или обычной веб-страницы.
- `/audio <youtube_url_or_video_id>` — выгрузить аудио YouTube-видео файлом.
- `/transcribe <youtube_url_or_video_id>` — получить транскрипт в `.txt`; если субтитров нет, бот предложит подтверждение Whisper кнопкой.
- `/addfeed <url> [mode=immediate|digest|on_demand] [label=...] [interval=10] [time=HH:MM]`
- `/addeventsource <url> [type=json|ics] [label=...] [interval=1]` — add events source (start notifications)
- `/addics <url> [label=...] [interval=1]` — add ICS calendar events source (start notifications)
- `/addevents [feed=<id>|<id>] [label=...] [interval=1]` + multiline rows in message — add events directly from Telegram text/CSV rows
- `/channel <channel_id> [mode=...] [label=...] [interval=10] [time=HH:MM]`
- `/playlist <playlist_id> [mode=...] [label=...] [interval=10] [time=HH:MM]`
- `/list` — list feeds.
- `/remove <feed_id>`
- `/setmode <feed_id> <mode> [time=HH:MM]`
- `/setfilter <feed_id> <json>` — e.g. `{ "include_keywords": ["обзор"] }`
- `/digest [feed_id|all]`
- `/mute <feed_id>` / `/unmute <feed_id>`

AI summary (`/ai`) setup:
- Default mode is `openai`, set `OPENAI_API_KEY` in `.env`.
- By default, `/ai` does not write summary artifacts to disk (reply-only mode in Telegram).
- To enable file persistence, set `AI_SUMMARIZER_SAVE_OUTPUT_FILES=true`.
- Optional tuning: `AI_SUMMARIZER_OPENAI_MODEL`, `AI_SUMMARIZER_LANGUAGES`, `AI_SUMMARIZER_MAX_SENTENCES`, `AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS`.
- Web page mode tuning: `AI_SUMMARIZER_WEB_OPENAI_MAX_INPUT_WORDS`, `AI_SUMMARIZER_WEB_FETCH_TIMEOUT_SEC`, `AI_SUMMARIZER_WEB_MAX_RESPONSE_BYTES`, `AI_SUMMARIZER_WEB_MAX_EXTRACTED_WORDS`.
- If YouTube subtitles are unavailable, `/ai` now returns a provisional summary based on short description + top comments and shows a button to run full Whisper transcription.
- Whisper flow requirements: `yt-dlp` binary on server (`AI_SUMMARIZER_WHISPER_YTDLP_BINARY`) and valid `OPENAI_API_KEY`.
- Whisper flow now normalizes audio and auto-splits long files into parts to stay under OpenAI upload limits.
- `/transcribe` shows video duration/size info before Whisper confirmation when subtitles are missing.
- Fallback tuning: `AI_SUMMARIZER_YOUTUBE_CONTEXT_*` settings let you cap extracted HTML/comments and OpenAI input size to save tokens.
- Whisper tuning: `AI_SUMMARIZER_WHISPER_MODEL`, `AI_SUMMARIZER_WHISPER_MAX_AUDIO_MB`, `AI_SUMMARIZER_WHISPER_DOWNLOAD_TIMEOUT_SEC`.
- Security note: web-page summarization blocks private/local addresses and accepts only `http/https`.

JSON events source format:
- Either an array of events, or an object with `events: []`.
- Required fields per event: `title`, `link` (or `url`), `start_at`.
- Recommended field: `id` (stable unique id).

Example:
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

ICS events source format:
- Standard `.ics` calendar with `VEVENT`.
- Event start time comes from `DTSTART` (`Z`, `TZID=...`, and `VALUE=DATE` are supported).
- Event title comes from `SUMMARY`.
- Event link uses `URL`, or first URL from `DESCRIPTION`, or falls back to the feed URL.
- Recommended: stable `UID` for deduplication/upserts.
- `webcal://` links are accepted and automatically normalized to `https://`.

`/addevents` text format examples:
```text
/addevents label=okko interval=1
2026-02-10T19:30:00+03:00;Женщины. Короткая программа;https://okko.sport/...
2026-02-10 21:00;Мужчины. Короткая программа;https://okko.sport/...
```

```text
/addevents feed=12
2026-02-10T19:30:00+03:00;Женщины. Короткая программа;https://okko.sport/...
2026-02-10T21:00:00+03:00;Мужчины. Короткая программа;https://okko.sport/...
```

`/addevents` delimiter is fixed: `;`.

Architecture and detailed plan: `docs/telegram_youtube_rss_bot_architecture.md`.
