from rssbot.bot import _format_feed_list_line, _resolve_feed_display_url
from rssbot.db import Feed


def test_resolve_feed_display_url_for_channel():
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert _resolve_feed_display_url(url) == "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw"


def test_resolve_feed_display_url_for_playlist():
    url = "https://www.youtube.com/feeds/videos.xml?playlist_id=PL590L5WQmH8fJ54Fglz5rT4f8LL2Si3xK"
    assert _resolve_feed_display_url(url) == "https://www.youtube.com/playlist?list=PL590L5WQmH8fJ54Fglz5rT4f8LL2Si3xK"


def test_resolve_feed_display_url_for_non_youtube():
    assert _resolve_feed_display_url("https://example.com/rss.xml") is None


def test_format_feed_list_line_adds_clickable_title_for_youtube():
    feed = Feed(
        id=7,
        user_id=1,
        url="https://www.youtube.com/feeds/videos.xml?channel_id=UC123",
        type="youtube",
        name=None,
        label="Best <Channel>",
        mode="digest",
        digest_time_local="08:30",
        poll_interval_min=10,
        enabled=True,
    )

    line = _format_feed_list_line(feed)
    assert line.startswith("✓ 7:")
    assert '<a href="https://www.youtube.com/channel/UC123">Best &lt;Channel&gt;</a>' in line
    assert " — digest в 08:30 [youtube]" in line


def test_format_feed_list_line_keeps_plain_title_for_other_feeds():
    feed = Feed(
        id=8,
        user_id=1,
        url="https://example.com/rss.xml?x=1&y=2",
        type="event_json",
        name=None,
        label="Site <Feed>",
        mode="immediate",
        digest_time_local=None,
        poll_interval_min=10,
        enabled=False,
    )

    line = _format_feed_list_line(feed)
    assert line.startswith("✗ 8:")
    assert "<a href=" not in line
    assert "Site &lt;Feed&gt;" in line
    assert " — immediate [event_json]" in line
