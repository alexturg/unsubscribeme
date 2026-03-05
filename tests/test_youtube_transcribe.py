import json
from pathlib import Path
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
