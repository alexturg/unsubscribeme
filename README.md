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
- `/addfeed <url> [mode=immediate|digest|on_demand] [label=...] [interval=10] [time=HH:MM]`
- `/channel <channel_id> [mode=...] [label=...] [interval=10] [time=HH:MM]`
- `/playlist <playlist_id> [mode=...] [label=...] [interval=10] [time=HH:MM]`
- `/list` — list feeds.
- `/remove <feed_id>`
- `/setmode <feed_id> <mode> [time=HH:MM]`
- `/setfilter <feed_id> <json>` — e.g. `{ "include_keywords": ["обзор"] }`
- `/digest [feed_id|all]`
- `/mute <feed_id>` / `/unmute <feed_id>`

Architecture and detailed plan: `docs/telegram_youtube_rss_bot_architecture.md`.
