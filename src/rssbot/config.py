from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    TELEGRAM_BOT_TOKEN: str = Field(..., description="Telegram bot token")
    OPENAI_API_KEY: Optional[str] = Field(
        default=None,
        description="OpenAI API key (used for /ai in openai mode)",
    )

    # Comma-separated list of chat ids allowed to interact with the bot.
    ALLOWED_CHAT_IDS: Optional[str] = Field(
        default=None, description="Comma-separated chat IDs allowed to control the bot"
    )

    # Default timezone for digest scheduling (IANA TZ name, e.g., Europe/Moscow)
    TZ: str = Field(default="UTC", description="Default timezone for digests and scheduling")

    # Path to SQLite database (example default for development)
    # In production, set DB_PATH environment variable to your actual data directory path
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

    AI_SUMMARIZER_MODE: str = Field(
        default="openai",
        description="Summarization mode: openai or extractive",
    )
    AI_SUMMARIZER_OPENAI_MODEL: str = Field(
        default="gpt-4.1-mini",
        description="OpenAI model for internal AI summarizer in openai mode",
    )
    AI_SUMMARIZER_LANGUAGES: str = Field(
        default="ru,en",
        description="Transcript language priority, comma-separated",
    )
    AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_URLS: str = Field(
        default="",
        description=(
            "Optional comma/newline-separated proxy list for YouTube transcript fetch "
            "(example: http://1.2.3.4:8080,http://5.6.7.8:3128)"
        ),
    )
    AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_URL: str = Field(
        default="",
        description=(
            "Optional HTTP/HTTPS URL to a plain-text proxy list (for example "
            "https://vakhov.github.io/fresh-proxy-list/http.txt)"
        ),
    )
    AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_TIMEOUT_SEC: int = Field(
        default=8,
        description="Timeout for downloading transcript proxy list URL",
    )
    AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_MAX_TRIES: int = Field(
        default=6,
        description="How many proxies to try after direct transcript request fails",
    )
    AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_REQUEST_TIMEOUT_SEC: int = Field(
        default=8,
        description="Per-request timeout for youtube-transcript-api HTTP calls",
    )
    AI_SUMMARIZER_MAX_SENTENCES: int = Field(default=7)
    AI_SUMMARIZER_OPENAI_MAX_INPUT_WORDS: int = Field(default=0)
    AI_SUMMARIZER_SAVE_OUTPUT_FILES: bool = Field(
        default=False,
        description="Save summary/source files to disk; disabled by default to avoid local clutter",
    )
    AI_SUMMARIZER_WEB_OPENAI_MAX_INPUT_WORDS: int = Field(
        default=1400,
        description="Fallback max input words for web-page summarization in OpenAI mode",
    )
    AI_SUMMARIZER_WEB_FETCH_TIMEOUT_SEC: int = Field(
        default=15,
        description="Timeout for loading web pages in /ai mode",
    )
    AI_SUMMARIZER_WEB_MAX_RESPONSE_BYTES: int = Field(
        default=2_000_000,
        description="Max bytes to download from a web page before extraction",
    )
    AI_SUMMARIZER_WEB_MAX_EXTRACTED_WORDS: int = Field(
        default=4500,
        description="Max words kept after HTML cleanup before summarization",
    )
    AI_SUMMARIZER_YOUTUBE_CONTEXT_FETCH_TIMEOUT_SEC: int = Field(
        default=15,
        description="Timeout for loading YouTube watch page in subtitles-missing fallback mode",
    )
    AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_HTML_BYTES: int = Field(
        default=2_500_000,
        description="Max HTML bytes to download from YouTube page for fallback context extraction",
    )
    AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_DESCRIPTION_WORDS: int = Field(
        default=220,
        description="Max words kept from YouTube short description for fallback summarization",
    )
    AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_COMMENTS: int = Field(
        default=12,
        description="Max number of top comments kept for YouTube fallback summarization",
    )
    AI_SUMMARIZER_YOUTUBE_CONTEXT_MAX_COMMENT_WORDS: int = Field(
        default=36,
        description="Per-comment max words in YouTube fallback summarization input",
    )
    AI_SUMMARIZER_YOUTUBE_CONTEXT_OPENAI_MAX_INPUT_WORDS: int = Field(
        default=900,
        description="OpenAI max input words budget for YouTube description/comments fallback",
    )
    AI_SUMMARIZER_WHISPER_MODEL: str = Field(
        default="whisper-1",
        description="OpenAI transcription model used by Whisper fallback flow",
    )
    AI_SUMMARIZER_WHISPER_MAX_AUDIO_MB: int = Field(
        default=24,
        description="Max audio file size for Whisper transcription",
    )
    AI_SUMMARIZER_WHISPER_DOWNLOAD_TIMEOUT_SEC: int = Field(
        default=240,
        description="Timeout for yt-dlp audio download in Whisper flow",
    )
    AI_SUMMARIZER_WHISPER_YTDLP_BINARY: str = Field(
        default="yt-dlp",
        description="Path or binary name of yt-dlp used to fetch audio for Whisper",
    )
    AI_AUDIO_EXPORT_MAX_BYTES: int = Field(
        default=48 * 1024 * 1024,
        description="Max audio file size for /audio command Telegram upload",
    )
    AI_SUMMARIZER_TIMEOUT_SEC: int = Field(default=600)
    AI_SUMMARIZER_OUTPUT_DIR: Path = Field(
        default=Path("data/ai_summaries"),
        description="Directory for internal AI summarizer output files",
    )
    AI_BULLSHIT_PROMPT_PATH: Path = Field(
        default=Path("data/prompts/bullshit_detector_v2.txt"),
        description="Path to system prompt template for /bullshit command",
    )
    AI_BULLSHIT_OPENAI_MODEL: str = Field(
        default="gpt-4.1-mini",
        description="OpenAI model for /bullshit intermediate summaries and final analysis",
    )
    AI_BULLSHIT_MAX_VIDEOS: int = Field(
        default=15,
        description="Default number of latest channel videos to scan in /bullshit",
    )
    AI_BULLSHIT_TOP_K: int = Field(
        default=5,
        description="Default suspicious videos selected for deep /bullshit analysis",
    )
    AI_BULLSHIT_FETCH_TIMEOUT_SEC: int = Field(
        default=20,
        description="Timeout for YouTube RSS fetch in /bullshit flow",
    )
    AI_BULLSHIT_SUMMARY_SENTENCES: int = Field(
        default=10,
        description="Number of bullet points for per-video summary in /bullshit flow",
    )
    AI_BULLSHIT_SUMMARY_MAX_INPUT_WORDS: int = Field(
        default=1600,
        description="OpenAI input budget for per-video summary in /bullshit flow",
    )
    AI_BULLSHIT_OPENAI_MAX_OUTPUT_TOKENS: int = Field(
        default=2200,
        description="OpenAI max output tokens for final /bullshit report",
    )

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
