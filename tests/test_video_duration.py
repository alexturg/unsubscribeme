import asyncio
import json

import pytest

from rssbot import youtube_context
from rssbot.cards import linked_title


@pytest.mark.parametrize("details,expected", [
    ({"lengthSeconds": "754"}, 754),
    ({"lengthSeconds": "3754"}, 3754),
    ({"lengthSeconds": "0"}, None),
    ({"lengthSeconds": "unknown"}, None),
    ({"lengthSeconds": "900", "isLive": True}, None),
    ({}, None),
])
def test_watch_page_duration(monkeypatch, details, expected):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _limit):
            return ("var ytInitialPlayerResponse = " + json.dumps({"videoDetails": details}) + ";").encode()

    monkeypatch.setattr(youtube_context.urllib.request, "urlopen", lambda *_a, **_k: Response())
    assert youtube_context.fetch_video_duration("dQw4w9WgXcQ") == expected


@pytest.mark.parametrize("seconds,suffix", [(None, ""), (0, ""), (59, " (0:59)"), (754, " (12:34)"), (3754, " (1:02:34)")])
def test_title_duration(seconds, suffix):
    assert linked_title("A < B", "https://example.com", seconds) == '<a href="https://example.com">A &lt; B</a>' + suffix


def test_unavailable_duration_does_not_block_card(monkeypatch):
    def fail(*_a, **_k):
        raise TimeoutError()

    monkeypatch.setattr(youtube_context, "fetch_video_duration", fail)
    assert asyncio.run(youtube_context.video_duration_for_link("https://youtu.be/dQw4w9WgXcQ")) is None
    assert asyncio.run(youtube_context.video_duration_for_link("https://example.com")) is None
