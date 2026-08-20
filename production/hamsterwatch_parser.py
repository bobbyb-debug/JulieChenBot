"""
Julie ChenBot Hamsterwatch Parser
==================================

Converts raw Hamsterwatch HTML into structured recap sections.

The parser owns interpretation of Hamsterwatch markup. It does not
fetch pages, touch storage, or emit ProductionEvents — matching the
separation already used by ProductionParser for JokersUpdates RSS.

Hamsterwatch page shape (confirmed against live pages)
--------------------------------------------------------
Each dated page (e.g. /bb28/081026.shtml) contains one or more
per-day recap sections between an ``<H1>Daily Feeds Recaps</H1>``
heading and the next ``<H1>`` tag:

    <H1>Daily Feeds Recaps</H1>
    <H3>Day 37 - Wednesday - August 12, 2026</H3>
    ...recap text and images...
    <H3>Day 36 - Tuesday - August 11, 2026</H3>
    ...
    <H1>Season Stats</H1>

The "current" (most recent) dated page keeps accumulating new
``<H3>`` sections as the season progresses; once a new dated page
appears, the previous one is frozen. Pre-season pages use a
different heading style ("Pre-season continued - July 1, 2026" or
"Pre-season - May 2026") with no BB day number.

Each ``<H3>`` section is treated as one atomic article, addressed by
(page_url, section_slug) — this is what lets the archive store
detect real per-day changes instead of hashing an entire growing
page as one blob.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html import unescape
from urllib.parse import urljoin

# ==========================================================
# Parsed section
# ==========================================================


@dataclass(slots=True)
class ParsedSection:
    """One recap section extracted from a Hamsterwatch page."""

    page_url: str
    section_slug: str
    heading: str
    article_date: str | None  # ISO date (YYYY-MM-DD), when extractable
    bb_day: int | None
    content: str
    summary: str


# ==========================================================
# Shared HTML normalization
# ==========================================================


def normalize_html(html: str) -> str:
    """Returns stable visible text suitable for hashing/reading.

    Shared with the rest of Julie's monitors (Quickview, HouseImage)
    so "meaningful change" always means "the visible text changed,"
    not "a byte moved."
    """

    html = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(html)).strip()


# ==========================================================
# Recap section extraction
# ==========================================================

_RECAP_BLOCK = re.compile(
    r"(?is)<H1[^>]*>\s*Daily\s+Feeds\s+Recaps\s*</H1>(?P<body>.*?)(?:<H1[^>]*>|\Z)"
)

_H3_HEADING = re.compile(r"(?is)<H3[^>]*>(?P<heading>.*?)</H3>")

_BB_DAY = re.compile(r"(?i)\bDay\s+(\d+)\b")

_FULL_DATE = re.compile(
    r"(?i)\b(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{1,2}),?\s*(\d{4})\b"
)

_MONTH_YEAR = re.compile(
    r"(?i)\b(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{4})\b"
)

_MONTH_NUMBER = {
    name.lower(): index
    for index, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        start=1,
    )
}


def _slugify(value: str, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:max_length] or "section"


def _extract_bb_day(heading_text: str) -> int | None:
    match = _BB_DAY.search(heading_text)
    return int(match.group(1)) if match else None


def _extract_article_date(heading_text: str) -> date | None:
    match = _FULL_DATE.search(heading_text)
    if match:
        month_name, day, year = match.groups()
        month = _MONTH_NUMBER.get(month_name.lower())
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                return None

    match = _MONTH_YEAR.search(heading_text)
    if match:
        month_name, year = match.groups()
        month = _MONTH_NUMBER.get(month_name.lower())
        if month:
            try:
                return date(int(year), month, 1)
            except ValueError:
                return None

    return None


def _section_slug(heading_text: str, bb_day: int | None, article_date: date | None) -> str:
    if bb_day is not None:
        return f"day-{bb_day}"
    if article_date is not None:
        return f"date-{article_date.isoformat()}"
    return _slugify(heading_text)


def summarize(content: str, max_chars: int = 280) -> str:
    """Returns a short, dependency-free summary of a recap section.

    Naive first-sentences truncation rather than an AI call: this
    keeps the monitor's per-tick check() free of any AI-provider
    dependency (and its latency/quota/failure surface), and always
    produces *something* as long as content is non-empty — which is
    what "reliably available" means in practice.
    """

    content = content.strip()
    if not content:
        return ""

    if len(content) <= max_chars:
        return content

    truncated = content[:max_chars]
    # Prefer to end on a sentence boundary when one exists nearby.
    boundary = max(truncated.rfind(". "), truncated.rfind("! "), truncated.rfind("? "))
    if boundary >= max_chars * 0.4:
        return truncated[: boundary + 1].strip()

    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.strip() + "…"


def parse_recap_sections(page_url: str, html: str) -> list[ParsedSection]:
    """Extracts every per-day recap section from one Hamsterwatch page.

    Returns an empty list for malformed or unrecognized markup rather
    than raising — a page that doesn't match the expected structure
    (site redesign, error page, empty response) simply yields no
    articles, and the caller decides what that means for monitor
    health.
    """

    if not html:
        return []

    block_match = _RECAP_BLOCK.search(html)
    if block_match is None:
        return []

    body = block_match.group("body")
    headings = list(_H3_HEADING.finditer(body))
    if not headings:
        return []

    sections: list[ParsedSection] = []
    seen_slugs: set[str] = set()

    for index, match in enumerate(headings):
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        raw_heading = normalize_html(match.group("heading"))
        raw_content = body[start:end]
        content = normalize_html(raw_content)

        if not raw_heading:
            continue

        bb_day = _extract_bb_day(raw_heading)
        article_date = _extract_article_date(raw_heading)
        slug = _section_slug(raw_heading, bb_day, article_date)

        # Guards against two headings on the same page normalizing to
        # the same slug (shouldn't happen given real Hamsterwatch
        # markup, but a duplicate would otherwise silently overwrite
        # the first section on upsert).
        original_slug = slug
        suffix = 2
        while slug in seen_slugs:
            slug = f"{original_slug}-{suffix}"
            suffix += 1
        seen_slugs.add(slug)

        sections.append(
            ParsedSection(
                page_url=page_url,
                section_slug=slug,
                heading=raw_heading,
                article_date=article_date.isoformat() if article_date else None,
                bb_day=bb_day,
                content=content,
                summary=summarize(content),
            )
        )

    return sections


# ==========================================================
# Discovery: BB28 daily index
# ==========================================================

_ARCHIVE_LINK = re.compile(
    r"""(?is)<A\s[^>]*HREF=["'](?P<href>[^"']*/bb28/(?:\d{6}|preseason\d*)\.shtml)["']"""
)


def extract_archive_links(html: str, base_url: str) -> list[str]:
    """Extracts every BB28 dated/preseason recap page link from the
    Hamsterwatch daily index page (``/bb28/days.shtml``).

    Resolved to absolute URLs and de-duplicated, preserving the
    order links appear in the index. Returns an empty list for
    unrecognized markup rather than raising.
    """

    if not html:
        return []

    seen: set[str] = set()
    links: list[str] = []
    for match in _ARCHIVE_LINK.finditer(html):
        absolute = urljoin(base_url, match.group("href"))
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)

    return links
