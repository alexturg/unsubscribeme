import sys

import pytest

from rssbot import youtube_transcribe as media


def test_media_download_disables_config_and_checks_real_directory_size(monkeypatch, tmp_path):
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        (tmp_path / "audio.mp3").write_bytes(b"audio")

    monkeypatch.setattr(media, "_run_subprocess_checked", run)
    media._download_audio_for_whisper(
        watch_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", work_dir=tmp_path,
        yt_dlp_binary="yt-dlp", timeout_sec=30,
    )
    command, kwargs = commands[0]
    assert "--ignore-config" in command
    assert command[command.index("--max-filesize") + 1] == str(media.MAX_MEDIA_DOWNLOAD_BYTES)
    assert command[command.index("--downloader") + 1] == "dash,m3u8:native"
    assert kwargs["work_dir"] == tmp_path


def test_media_process_stops_when_work_directory_exceeds_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "MAX_MEDIA_WORK_BYTES", 512)
    with pytest.raises(media.WhisperTranscriptionError, match="лимит временных"):
        media._run_subprocess_checked(
            [sys.executable, "-c", "from pathlib import Path; import time; "
             "Path('audio.part').write_bytes(b'x' * 1024); time.sleep(10)"],
            timeout_sec=3, fail_message="download", work_dir=tmp_path,
        )


def test_media_timeout_kills_the_process_group(monkeypatch, tmp_path):
    groups = []
    killpg = media.os.killpg

    def kill_group(pid, signal):
        groups.append(pid)
        killpg(pid, signal)

    monkeypatch.setattr(media.os, "killpg", kill_group)
    with pytest.raises(media.WhisperTranscriptionError, match="таймаут"):
        media._run_subprocess_checked(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_sec=0.1, fail_message="download", work_dir=tmp_path,
        )
    assert len(groups) == 1


def test_media_rejects_excessive_diagnostics(tmp_path):
    with pytest.raises(media.WhisperTranscriptionError, match="вывод команды"):
        media._run_subprocess_checked(
            [sys.executable, "-c", "import sys; sys.stdout.write('x'*1100000)"],
            timeout_sec=3, fail_message="download", work_dir=tmp_path,
        )


def test_media_process_can_finish_and_reports_failures(tmp_path):
    media._run_subprocess_checked(
        [sys.executable, "-c", "print('ok')"],
        timeout_sec=3, fail_message="download", work_dir=tmp_path,
    )
    with pytest.raises(media.WhisperTranscriptionError, match="decoder failed"):
        media._run_subprocess_checked(
            [sys.executable, "-c", "import sys; sys.stderr.write('decoder failed'); sys.exit(1)"],
            timeout_sec=3, fail_message="download", work_dir=tmp_path,
        )
