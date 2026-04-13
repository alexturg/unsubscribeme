from rssbot.config import Settings


def test_settings_ignore_unknown_environment_variables(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEF")
    monkeypatch.setenv(
        "AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_PROXY_LIST_URL",
        "https://vakhov.github.io/fresh-proxy-list/http.txt",
    )
    monkeypatch.setenv("SOME_FUTURE_UNKNOWN_SETTING", "1")

    settings = Settings(_env_file=None)

    assert settings.TELEGRAM_BOT_TOKEN == "123456:ABCDEF"
