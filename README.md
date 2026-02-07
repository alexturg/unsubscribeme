# UnsubscribeMe — Telegram YouTube RSS Bot

Bot that fetches YouTube RSS feeds, filters by rules, and delivers updates:
- Modes per feed: `immediate`, `digest`, `on_demand`.
- Include/exclude keywords and regex, categories, duration.
- Daily digests at a chosen local time.

Quick start:
- Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN` and `ALLOWED_CHAT_IDS`.
- Python 3.9+ required.
- Install and run:
  - `pip install .`
  - `unsubscribeme`

Core commands:
- `/start` — register.
- `/ai <youtube_url_or_video_id> [дополнительный фокус]` — расшифровать и сделать AI-суммаризацию видео.
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
- Optional tuning: `AI_SUMMARIZER_OPENAI_MODEL`, `AI_SUMMARIZER_LANGUAGES`, `AI_SUMMARIZER_MAX_SENTENCES`, `AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS`.

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
