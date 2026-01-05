from __future__ import annotations

from pathlib import Path

from rssbot.config import Settings, ensure_data_dir


def test_allowed_chat_ids_parsing() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test",
        ALLOWED_CHAT_IDS="1, 2, bad, ,3",
    )
    assert settings.allowed_chat_ids() == [1, 2, 3]


def test_allowed_chat_ids_none() -> None:
    settings = Settings(TELEGRAM_BOT_TOKEN="test", ALLOWED_CHAT_IDS="")
    assert settings.allowed_chat_ids() is None


def test_ensure_data_dir_handles_file_and_dir(tmp_path: Path) -> None:
    file_path = tmp_path / "data" / "bot.sqlite"
    ensure_data_dir(file_path)
    assert file_path.parent.is_dir()

    dir_path = tmp_path / "cache"
    ensure_data_dir(dir_path)
    assert dir_path.is_dir()
