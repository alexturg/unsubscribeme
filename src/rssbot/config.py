from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env",), env_prefix="", case_sensitive=False)

    TELEGRAM_BOT_TOKEN: str = Field(..., description="Telegram bot token")

    # Comma-separated list of chat ids allowed to interact with the bot.
    ALLOWED_CHAT_IDS: Optional[str] = Field(
        default=None, description="Comma-separated chat IDs allowed to control the bot"
    )

    # Default timezone for digest scheduling (IANA TZ name, e.g., Europe/Moscow)
    TZ: str = Field(default="UTC", description="Default timezone for digests and scheduling")

    # Path to SQLite database
    DB_PATH: Path = Field(default=Path("data/bot.sqlite"), description="Path to SQLite DB file")

    # Default polling interval for feeds (minutes)
    DEFAULT_POLL_INTERVAL_MIN: int = Field(default=10)

    # Default daily digest time (HH:MM) in user's local TZ
    DIGEST_DEFAULT_TIME: str = Field(default="20:00")

    # Backfill last N items per feed on startup (0 to disable)
    BACKFILL_ON_START_N: int = Field(default=10)

    # Hide videos whose availability time is in the future
    HIDE_FUTURE_VIDEOS: bool = Field(default=False)

    # Web UI host/port
    WEB_HOST: str = Field(default="127.0.0.1", description="Host for built-in web UI")
    WEB_PORT: int = Field(default=8080, description="Port for built-in web UI")

    def allowed_chat_ids(self) -> Optional[List[int]]:
        if not self.ALLOWED_CHAT_IDS:
            return None
        result: List[int] = []
        for part in self.ALLOWED_CHAT_IDS.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                result.append(int(part))
            except ValueError:
                # ignore malformed ids
                continue
        return result


def ensure_data_dir(path: Path) -> None:
    if path.suffix:
        # treat as file path
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.mkdir(parents=True, exist_ok=True)
