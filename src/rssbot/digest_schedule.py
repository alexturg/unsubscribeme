"""Weekday settings shared by Telegram commands and the web form."""

from typing import Optional

WEEKDAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_ENGLISH_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_digest_day(value: str) -> Optional[int]:
    value = value.strip().lower()
    if value in {"daily", "ежедневно"}:
        return None
    for names in (WEEKDAY_NAMES, _ENGLISH_DAYS):
        if value in names:
            return names.index(value)
    raise ValueError("Неверный день. Используйте пн, вт, ср, чт, пт, сб, вс или daily.")


def digest_day_label(day: Optional[int]) -> str:
    return "ежедневно" if day is None else WEEKDAY_NAMES[day]
