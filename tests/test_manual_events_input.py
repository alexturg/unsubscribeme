from datetime import datetime, timezone

from rssbot.bot import parse_bulk_events_text


def test_parse_bulk_events_text_semicolon_rows():
    block = (
        "2026-02-10T19:30:00+03:00;Event One;https://example.com/1\n"
        "2026-02-10 21:00;Event Two;https://example.com/2"
    )
    items, errors = parse_bulk_events_text(block, "Europe/Moscow")
    assert not errors
    assert len(items) == 2
    assert items[0]["title"] == "Event One"
    assert items[0]["published_at"] == datetime(2026, 2, 10, 16, 30, tzinfo=timezone.utc)
    assert items[1]["title"] == "Event Two"
    assert items[1]["published_at"] == datetime(2026, 2, 10, 18, 0, tzinfo=timezone.utc)


def test_parse_bulk_events_text_semicolon_rows_without_id():
    block = (
        "2026-02-10T19:30:00+03:00;Event One;https://example.com/1\n"
        "2026-02-10T20:30:00+03:00;Event Two;https://example.com/2"
    )
    items, errors = parse_bulk_events_text(block, "Europe/Moscow")
    assert not errors
    assert len(items) == 2
    assert items[0]["external_id"]
    assert items[1]["external_id"]
    assert items[0]["external_id"] != items[1]["external_id"]


def test_parse_bulk_events_text_reports_errors():
    block = (
        "broken line without separator\n"
        "2026-02-10 19:00 | Title only | https://example.com\n"
        "evt-1;2026-02-10T19:30:00+03:00;Event One;https://example.com/1"
    )
    items, errors = parse_bulk_events_text(block, "Europe/Moscow")
    assert not items
    assert len(errors) == 3
