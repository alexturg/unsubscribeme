from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .db import FeedRule


@dataclass
class Content:
    title: str = ""
    description: str = ""
    categories: Optional[Iterable[str]] = None
    duration_sec: Optional[int] = None


def _normalize(text: str, case_sensitive: bool) -> str:
    return text if case_sensitive else text.lower()


def _any_keyword(text: str, keywords: Iterable[str], case_sensitive: bool) -> bool:
    if not keywords:
        return False
    base = _normalize(text, case_sensitive)
    for kw in keywords:
        if not kw:
            continue
        needle = kw if case_sensitive else kw.lower()
        if needle in base:
            return True
    return False


def _all_keywords(text: str, keywords: Iterable[str], case_sensitive: bool) -> bool:
    if not keywords:
        return True
    base = _normalize(text, case_sensitive)
    for kw in keywords:
        if not kw:
            continue
        needle = kw if case_sensitive else kw.lower()
        if needle not in base:
            return False
    return True


def _any_regex(text: str, patterns: Iterable[str], case_sensitive: bool) -> bool:
    flags = 0 if case_sensitive else re.IGNORECASE
    for pat in patterns or []:
        try:
            if re.search(pat, text, flags=flags):
                return True
        except re.error:
            # invalid pattern -> ignore
            continue
    return False


def matches_rules(content: Content, rules: Optional[FeedRule]) -> bool:
    # If no rules, allow all
    if rules is None:
        return True

    text = (content.title or "") + "\n" + (content.description or "")
    categories = [c.lower() for c in (content.categories or [])]

    # Exclude checks first
    if rules.exclude_keywords and _any_keyword(text, rules.exclude_keywords, rules.case_sensitive):
        return False
    if rules.exclude_regex and _any_regex(text, rules.exclude_regex, rules.case_sensitive):
        return False

    if rules.categories:
        want = {c.lower() for c in rules.categories}
        if not categories or not (set(categories) & want):
            # If categories filter set and no intersection -> reject
            return False

    # Duration checks
    if content.duration_sec is not None:
        if rules.min_duration_sec is not None and content.duration_sec < rules.min_duration_sec:
            return False
        if rules.max_duration_sec is not None and content.duration_sec > rules.max_duration_sec:
            return False

    # Include checks: if include lists provided, must match
    include_blocks = []
    if rules.include_keywords:
        if rules.require_all:
            include_blocks.append(_all_keywords(text, rules.include_keywords, rules.case_sensitive))
        else:
            include_blocks.append(_any_keyword(text, rules.include_keywords, rules.case_sensitive))
    if rules.include_regex:
        include_blocks.append(_any_regex(text, rules.include_regex, rules.case_sensitive))

    return all(include_blocks) if include_blocks else True

