from rssbot.rules import Content, matches_rules
from rssbot.db import FeedRule
import pytest


def test_rules_include_exclude_keywords():
    content = Content(title="Обзор Python Tips", description="")

    rules = FeedRule(
        feed_id=1,
        include_keywords=["обзор"],
        exclude_keywords=["стрим"],
        require_all=False,
        case_sensitive=False,
    )
    assert matches_rules(content, rules) is True

    content2 = Content(title="Стрим по Python")
    assert matches_rules(content2, rules) is False  # excluded by keyword


def test_rules_regex_and_categories():
    content = Content(title="Python tips", categories=["Education", "Tech"])

    rules = FeedRule(
        feed_id=1,
        include_regex=[r"(?i)python\s+tips"],
        categories=["education"],
        case_sensitive=False,
    )
    assert matches_rules(content, rules) is True

    content2 = Content(title="Other", categories=["Music"])  # wrong category
    assert matches_rules(content2, rules) is False


def test_rules_duration_bounds():
    rules = FeedRule(feed_id=1, min_duration_sec=60, max_duration_sec=3600)
    assert matches_rules(Content(title="A", duration_sec=120), rules) is True
    assert matches_rules(Content(title="A", duration_sec=10), rules) is False
    assert matches_rules(Content(title="A", duration_sec=7200), rules) is False


@pytest.mark.parametrize("link,allowed", [
    ("https://www.youtube.com/shorts/abc?feature=share", False),
    ("https://youtube.com/shorts/abc", False),
    ("https://m.youtube.com/shorts/abc", False),
    ("https://www.youtube.com/watch?v=abc", True),
    ("https://example.com/shorts/abc", True),
    ("https://youtube.com.example.com/shorts/abc", True),
    ("", True),
    ("https://[invalid", True),
])
def test_exclude_shorts_uses_youtube_link_without_duration(link, allowed):
    rules = FeedRule(feed_id=1, exclude_shorts=True)
    assert matches_rules(Content(title="Regular title", link=link), rules) is allowed


def test_shorts_allowed_unless_explicitly_excluded():
    content = Content(title="Short", link="https://www.youtube.com/shorts/abc")
    assert matches_rules(content, None)
    assert matches_rules(content, FeedRule(feed_id=1, exclude_shorts=False))
