import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from rssbot import youtube_transcribe


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.waits.append(duration)
        self.now += duration


@pytest.fixture(autouse=True)
def transcript_clock(monkeypatch):
    clock = FakeClock()
    limiter = youtube_transcribe._TranscriptRequestLimiter(clock.monotonic, clock.sleep)
    monkeypatch.setattr(youtube_transcribe, "_transcript_limiter", limiter)
    return clock


def test_transcript_http_requests_share_interval_across_sessions(monkeypatch, transcript_clock):
    import requests

    started = []

    def fake_request(*args, **kwargs):
        started.append(transcript_clock.now)
        assert kwargs["timeout"] == 8
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    direct = youtube_transcribe._build_timeout_http_client(8, min_interval_sec=10)
    proxy = youtube_transcribe._build_timeout_http_client(8, proxy_url="http://example:80", min_interval_sec=10)
    direct.get("https://www.youtube.com/watch?v=test")
    direct.post("https://www.youtube.com/youtubei/v1/player")
    proxy.get("https://www.youtube.com/api/timedtext")
    assert started == [100, 110, 120]
    assert transcript_clock.waits == [10, 10]


def test_transcript_http_429_cools_down_only_affected_route(monkeypatch, transcript_clock):
    import requests

    calls = []

    def fake_request(*args, **kwargs):
        calls.append(transcript_clock.now)
        return SimpleNamespace(status_code=429 if len(calls) == 1 else 200)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    direct = youtube_transcribe._build_timeout_http_client(8, block_cooldown_sec=900)
    proxy = youtube_transcribe._build_timeout_http_client(8, proxy_url="http://example:80")
    direct.get("https://www.youtube.com/watch?v=test")
    with pytest.raises(youtube_transcribe.TranscriptError, match="RequestBlocked"):
        direct.get("https://www.youtube.com/watch?v=test")
    proxy.get("https://www.youtube.com/watch?v=test")
    assert calls == [100, 110]
    transcript_clock.now = 1000
    direct.get("https://www.youtube.com/watch?v=test")
    assert calls == [100, 110, 1000]


def test_transcript_blocked_error_cooldown_is_not_extended_by_retries(transcript_clock):
    limiter = youtube_transcribe._transcript_limiter
    limiter.record_error(RuntimeError("IpBlocked"), route=None, block_cooldown_sec=900)
    transcript_clock.now += 60
    with pytest.raises(youtube_transcribe.TranscriptError) as caught:
        limiter.request(lambda: None, route=None, min_interval_sec=10, block_cooldown_sec=900)
    limiter.record_error(caught.value, route=None, block_cooldown_sec=900)
    transcript_clock.now = 1000
    assert limiter.request(lambda: "ok", route=None, min_interval_sec=10, block_cooldown_sec=900) == "ok"


def test_transcript_failed_connection_also_observes_interval(transcript_clock):
    limiter = youtube_transcribe._transcript_limiter

    def fail():
        raise ConnectionError("connection failed")

    with pytest.raises(ConnectionError):
        limiter.request(fail, route=None, min_interval_sec=10, block_cooldown_sec=900)
    limiter.request(lambda: None, route=None, min_interval_sec=10, block_cooldown_sec=900)
    assert transcript_clock.waits == [10]


def test_transcript_requests_are_serialized_even_when_interval_is_disabled():
    limiter = youtube_transcribe._TranscriptRequestLimiter()
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first_request():
        entered.set()
        assert release.wait(2)

    def run(call):
        limiter.request(call, route=None, min_interval_sec=0, block_cooldown_sec=0)

    first = threading.Thread(target=run, args=(first_request,))
    second = threading.Thread(target=run, args=(second_entered.set,))
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert not second_entered.wait(0.05)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert second_entered.is_set()
    assert not first.is_alive() and not second.is_alive()


def test_transcript_settings_propagate_rate_limit():
    options = youtube_transcribe.transcript_options_from_settings(SimpleNamespace(
        AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_MIN_INTERVAL_SEC=20,
        AI_SUMMARIZER_YOUTUBE_TRANSCRIPT_BLOCK_COOLDOWN_SEC=1800,
    ))
    assert options["min_interval_sec"] == 20
    assert options["block_cooldown_sec"] == 1800


def test_fetch_video_info_parses_yt_dlp_json(monkeypatch):
    payload = {
        "title": "Sample title",
        "duration": 321,
        "filesize": 123456,
        "filesize_approx": 200000,
    }

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(youtube_transcribe.subprocess, "run", fake_run)
    info = youtube_transcribe.fetch_video_info("dQw4w9WgXcQ", yt_dlp_binary="yt-dlp", timeout_sec=20)

    assert info.video_id == "dQw4w9WgXcQ"
    assert info.title == "Sample title"
    assert info.duration_seconds == 321
    assert info.filesize_bytes == 123456
    assert info.filesize_approx_bytes == 200000


def test_fetch_video_info_raises_on_yt_dlp_error(monkeypatch):
    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(youtube_transcribe.subprocess, "run", fake_run)
    with pytest.raises(youtube_transcribe.YouTubeMediaError):
        youtube_transcribe.fetch_video_info("dQw4w9WgXcQ")


def test_download_audio_for_export_returns_downloaded_file(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(youtube_transcribe, "extract_video_id", lambda _: "dQw4w9WgXcQ")
    monkeypatch.setattr(youtube_transcribe, "_run_subprocess_checked", lambda *args, **kwargs: None)

    file_path = tmp_path / "dQw4w9WgXcQ.mp3"
    file_path.write_bytes(b"audio")

    result = youtube_transcribe.download_audio_for_export(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        output_dir=tmp_path,
    )
    assert result == file_path


def test_fetch_transcript_retries_with_proxy_urls(monkeypatch):
    calls: list[object] = []

    class FakeApi:
        @staticmethod
        def get_transcript(_video_id, languages=None, proxies=None):
            calls.append(proxies)
            if proxies is None:
                raise RuntimeError("RequestBlocked: blocked")
            return [{"text": "hello proxy", "start": 0.0, "duration": 1.0}]

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", SimpleNamespace(YouTubeTranscriptApi=FakeApi))

    segments = youtube_transcribe.fetch_transcript(
        "dQw4w9WgXcQ",
        ["en"],
        proxy_urls="11.22.33.44:8080",
        proxy_max_tries=2,
    )

    assert [item.text for item in segments] == ["hello proxy"]
    assert calls[0] is None
    assert isinstance(calls[1], dict)
    assert calls[1]["https"] == "http://11.22.33.44:8080"


def test_fetch_transcript_loads_proxy_list_from_url(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _max_bytes: int) -> bytes:
            return b"8.8.8.8:80\n9.9.9.9:8080\n"

    opened_urls: list[str] = []

    def fake_urlopen(req, timeout=0):
        del timeout
        opened_urls.append(str(getattr(req, "full_url", req)))
        return FakeResponse()

    class FakeApi:
        @staticmethod
        def get_transcript(_video_id, languages=None, proxies=None):
            del languages
            if proxies is None:
                raise RuntimeError("IpBlocked")
            if proxies.get("https") == "http://8.8.8.8:80":
                return [{"text": "from-list", "start": 0.0, "duration": 1.0}]
            raise RuntimeError("proxy failed")

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", SimpleNamespace(YouTubeTranscriptApi=FakeApi))
    monkeypatch.setattr(youtube_transcribe.urllib_request, "urlopen", fake_urlopen)

    segments = youtube_transcribe.fetch_transcript(
        "dQw4w9WgXcQ",
        ["en"],
        proxy_list_url="https://example.com/http.txt",
        proxy_max_tries=3,
    )

    assert [item.text for item in segments] == ["from-list"]
    assert opened_urls == ["https://example.com/http.txt"]


def test_fetch_transcript_supports_generic_proxy_config(monkeypatch):
    class FakeProxyConfig:
        def __init__(self, *, http_url: str, https_url: str):
            self.http_url = http_url
            self.https_url = https_url

    class FakeApi:
        def __init__(self, proxy_config=None):
            self.proxy_config = proxy_config

        def fetch(self, _video_id, languages=None, proxies=None):
            del languages, proxies
            if self.proxy_config is None:
                raise RuntimeError("RequestBlocked")
            if self.proxy_config.http_url == "http://55.66.77.88:9000":
                return [{"text": "generic-proxy-ok", "start": 0.0, "duration": 1.0}]
            raise RuntimeError("bad proxy")

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", SimpleNamespace(YouTubeTranscriptApi=FakeApi))
    monkeypatch.setitem(
        sys.modules,
        "youtube_transcript_api.proxies",
        SimpleNamespace(GenericProxyConfig=FakeProxyConfig),
    )

    segments = youtube_transcribe.fetch_transcript(
        "dQw4w9WgXcQ",
        ["en"],
        proxy_urls=["55.66.77.88:9000"],
        proxy_max_tries=2,
    )

    assert [item.text for item in segments] == ["generic-proxy-ok"]
