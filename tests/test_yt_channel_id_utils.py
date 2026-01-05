from __future__ import annotations

import pytest

from utils import yt_channel_id as yt


def test_normalize_url_adds_scheme_and_strips() -> None:
    cid = "UC1234567890123456789012"
    assert (
        yt.normalize_url(f" youtube.com/channel/{cid} ")
        == f"https://youtube.com/channel/{cid}"
    )


def test_normalize_url_empty_raises() -> None:
    with pytest.raises(ValueError):
        yt.normalize_url("   ")


def test_extract_from_path_variants() -> None:
    cid = "UC1234567890123456789012"
    assert yt.extract_from_path(f"/channel/{cid}") == cid
    assert yt.extract_from_path(f"/{cid}") == cid
    assert yt.extract_from_path("/user/someone") is None


def test_extract_from_html_pattern() -> None:
    cid = "UC1234567890123456789012"
    html = f'<meta itemprop="channelId" content="{cid}">'
    assert yt.extract_from_html(html) == cid


def test_get_channel_id_prefers_path(monkeypatch) -> None:
    cid = "UC1234567890123456789012"

    def boom(*_args, **_kwargs):
        raise AssertionError("fetch_html should not be called")

    monkeypatch.setattr(yt, "fetch_html", boom)
    assert yt.get_channel_id(f"https://www.youtube.com/channel/{cid}") == cid


def test_get_channel_id_from_final_url(monkeypatch) -> None:
    cid = "UC1234567890123456789012"

    def fake_fetch_html(_url, _timeout, _insecure, _ca_bundle):
        return "", f"https://www.youtube.com/channel/{cid}"

    monkeypatch.setattr(yt, "fetch_html", fake_fetch_html)
    assert yt.get_channel_id("https://www.youtube.com/@somehandle") == cid
