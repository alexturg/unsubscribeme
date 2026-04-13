import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from rssbot import youtube_transcribe


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
