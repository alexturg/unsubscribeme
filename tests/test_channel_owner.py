import json

import pytest

from utils.yt_channel_id import extract_from_html, get_channel_id


OWNER = "UCCzVNbWZfYpBfyofCCUD_0w"
RECOMMENDED = "UCXYvxL4zw64tEcCY7iYMeyQ"


@pytest.mark.parametrize("owner_html", [
    f'<link rel="canonical" href="https://www.youtube.com/channel/{OWNER}">',
    f"<link href='https://www.youtube.com/channel/{OWNER}' rel='canonical'>",
    f'<meta content="{OWNER}" itemprop="channelId">',
    "var ytInitialData = " + json.dumps({
        "contents": {"channelId": RECOMMENDED},
        "metadata": {"channelMetadataRenderer": {"externalId": OWNER}},
    }) + ";",
])
def test_page_owner_wins_over_recommended_channels(owner_html):
    html = f'<script>var recommendations = {{"channelId":"{RECOMMENDED}"}};</script>' + owner_html
    assert extract_from_html(html) == OWNER


def test_unrelated_ids_never_become_channel_owner():
    assert extract_from_html(f'{{"channelId":"{RECOMMENDED}","browseId":"{RECOMMENDED}"}}') is None


def test_handle_resolves_to_canonical_owner(monkeypatch):
    monkeypatch.setattr("utils.yt_channel_id.fetch_html", lambda *_a: (
        f'{{"channelId":"{RECOMMENDED}"}}<link rel="canonical" href="https://www.youtube.com/channel/{OWNER}">',
        "https://www.youtube.com/@denis_chuzhoy?cbrd=1",
    ))
    assert get_channel_id("https://www.youtube.com/@denis_chuzhoy") == OWNER
