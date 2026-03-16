import pytest

from rssbot.bullshit_detector import (
    ChannelVideo,
    parse_bullshit_request_text,
    score_video_suspicion,
    shortlist_suspicious_videos,
)


def test_parse_bullshit_request_text_with_overrides():
    request = parse_bullshit_request_text(
        "/bullshit https://www.youtube.com/@example videos=20 top=4",
        default_max_videos=15,
        default_top_k=5,
    )
    assert request.channel_ref == "https://www.youtube.com/@example"
    assert request.max_videos == 20
    assert request.top_k == 4


def test_parse_bullshit_request_text_rejects_unknown_arg():
    with pytest.raises(ValueError):
        parse_bullshit_request_text("/bullshit UC123 bad=1")


def test_score_video_suspicion_detects_clickbait_patterns():
    score, reasons = score_video_suspicion("ШОК!!! 100% ПРАВДА, о которой молчат")
    assert score >= 30
    assert reasons


def test_shortlist_suspicious_videos_prefers_higher_scores():
    videos = [
        ChannelVideo("a", "A", "u1", 100, 5, ("weak",)),
        ChannelVideo("b", "B", "u2", 110, 45, ("high",)),
        ChannelVideo("c", "C", "u3", 120, 20, ("mid",)),
    ]
    shortlist = shortlist_suspicious_videos(videos, top_k=2)
    assert [video.video_id for video in shortlist] == ["b", "c"]


def test_shortlist_suspicious_videos_falls_back_to_latest_when_scores_zero():
    videos = [
        ChannelVideo("a", "A", "u1", 100, 0, ()),
        ChannelVideo("b", "B", "u2", 120, 0, ()),
        ChannelVideo("c", "C", "u3", 110, 0, ()),
    ]
    shortlist = shortlist_suspicious_videos(videos, top_k=2)
    assert [video.video_id for video in shortlist] == ["b", "c"]
