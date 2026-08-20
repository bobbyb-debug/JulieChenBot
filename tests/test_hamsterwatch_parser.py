"""Tests for production/hamsterwatch_parser.py.

Fixture markup mirrors the real Hamsterwatch page structure (verified
against live pages during development): a "Daily Feeds Recaps" <H1>
block containing one <H3> heading per BB day, ending at the next
<H1>.
"""

from __future__ import annotations

from production.hamsterwatch_parser import (
    extract_archive_links,
    parse_recap_sections,
    summarize,
)


def _page(*sections: str) -> str:
    body = "\n".join(sections)
    return f"""
    <HTML><BODY>
    <A NAME="recaps"></A>
    <H1>Daily Feeds Recaps</H1>
    {body}
    <H1>Season Stats</H1>
    <H3>Not a recap heading</H3>
    should not be captured
    </BODY></HTML>
    """


def _day_section(day: int, weekday: str, date_str: str, content: str) -> str:
    return f"<H3>Day {day} - {weekday} - {date_str}</H3>\n{content}<BR>\n<BR>"


# ==========================================================
# Day-section extraction
# ==========================================================


def test_extracts_bb_day_date_heading_and_content():
    html = _page(
        _day_section(
            37, "Wednesday", "August 12, 2026",
            "Kamu and Yash talked strategy in the gym about targeting Chuk.",
        )
    )

    sections = parse_recap_sections("http://hamsterwatch.com/bb28/081026.shtml", html)

    assert len(sections) == 1
    section = sections[0]
    assert section.bb_day == 37
    assert section.article_date == "2026-08-12"
    assert section.heading == "Day 37 - Wednesday - August 12, 2026"
    assert "Kamu and Yash talked strategy" in section.content
    assert section.section_slug == "day-37"
    assert section.page_url == "http://hamsterwatch.com/bb28/081026.shtml"


def test_extracts_multiple_day_sections_in_order():
    html = _page(
        _day_section(37, "Wednesday", "August 12, 2026", "Newest recap content."),
        _day_section(36, "Tuesday", "August 11, 2026", "Previous day recap content."),
        _day_section(35, "Monday", "August 10, 2026", "Oldest recap content on this page."),
    )

    sections = parse_recap_sections("http://hamsterwatch.com/bb28/081026.shtml", html)

    assert [s.bb_day for s in sections] == [37, 36, 35]
    assert [s.section_slug for s in sections] == ["day-37", "day-36", "day-35"]
    assert "Newest" in sections[0].content
    assert "Oldest" in sections[2].content


def test_preseason_headings_have_no_bb_day_but_get_a_date():
    html = _page(
        "<H3>Pre-season continued - July 1, 2026</H3>\nCast rumors are heating up.<BR>",
        "<H3>Pre-season - May 2026</H3>\nEarly preseason chatter, no exact day given.<BR>",
    )

    sections = parse_recap_sections("http://hamsterwatch.com/bb28/preseason.shtml", html)

    assert len(sections) == 2
    assert sections[0].bb_day is None
    assert sections[0].article_date == "2026-07-01"
    assert sections[0].section_slug == "date-2026-07-01"

    assert sections[1].bb_day is None
    assert sections[1].article_date == "2026-05-01"
    assert sections[1].section_slug == "date-2026-05-01"


def test_heading_with_neither_day_nor_date_falls_back_to_slugified_heading():
    html = _page("<H3>Season Wrap-Up Thoughts</H3>\nFinale reflections.<BR>")

    sections = parse_recap_sections("http://hamsterwatch.com/bb28/finale.shtml", html)

    assert len(sections) == 1
    assert sections[0].bb_day is None
    assert sections[0].article_date is None
    assert sections[0].section_slug == "season-wrap-up-thoughts"


def test_duplicate_headings_on_one_page_get_distinct_slugs():
    html = _page(
        _day_section(37, "Wednesday", "August 12, 2026", "First version of the recap."),
        _day_section(37, "Wednesday", "August 12, 2026", "Accidental duplicate heading."),
    )

    sections = parse_recap_sections("http://hamsterwatch.com/bb28/081026.shtml", html)

    assert len(sections) == 2
    assert sections[0].section_slug != sections[1].section_slug


def test_content_strips_tags_and_normalizes_whitespace():
    html = _page(
        '<H3>Day 1 - Tuesday - July 7, 2026</H3>\n'
        '<IMG SRC="x.jpg" WIDTH="300">Move-in   day    chaos.<BR clear=all>\n<BR>\n'
        "More   text with <B>bold</B> markup.<BR>"
    )

    sections = parse_recap_sections("http://hamsterwatch.com/bb28/070726.shtml", html)

    assert sections[0].content == "Move-in day chaos. More text with bold markup."


# ==========================================================
# Malformed / unavailable input
# ==========================================================


def test_empty_html_returns_no_sections():
    assert parse_recap_sections("http://x/page.shtml", "") == []


def test_html_without_recap_block_returns_no_sections():
    html = "<html><body><h1>Some Other Page</h1><p>Nothing recap-shaped here.</p></body></html>"
    assert parse_recap_sections("http://x/page.shtml", html) == []


def test_recap_block_with_no_h3_headings_returns_no_sections():
    html = "<H1>Daily Feeds Recaps</H1>\nJust a paragraph, no day headings.\n<H1>Season Stats</H1>"
    assert parse_recap_sections("http://x/page.shtml", html) == []


# ==========================================================
# summarize()
# ==========================================================


def test_summarize_returns_short_content_unchanged():
    assert summarize("Short recap text.") == "Short recap text."


def test_summarize_empty_content_returns_empty_string():
    assert summarize("") == ""
    assert summarize("   ") == ""


def test_summarize_truncates_long_content_at_a_sentence_boundary():
    content = (
        "Kamu talked strategy in the gym. Yash agreed to target Chuk next week. "
        "Later, Angela and Barrett discussed the veto plan quietly in the kitchen "
        "while everyone else napped through the afternoon heat."
    )

    result = summarize(content, max_chars=70)

    assert len(result) < len(content)
    assert result.endswith((".", "…"))


def test_summarize_never_exceeds_a_reasonable_bound_past_max_chars():
    content = "word " * 500
    result = summarize(content, max_chars=200)
    assert len(result) <= 205


# ==========================================================
# Discovery: extract_archive_links
# ==========================================================


def test_extracts_and_resolves_dated_and_preseason_links():
    html = (
        '<A HREF="/bb28/preseason.shtml" TARGET="_top">Pre-season</A>'
        '<A HREF="/bb28/070926.shtml" TARGET="_top">Week 1</A>'
        '<A HREF="/bb28/071126.shtml" TARGET="_top">Week 1 more</A>'
    )

    links = extract_archive_links(html, "http://hamsterwatch.com/bb28/days.shtml")

    assert links == [
        "http://hamsterwatch.com/bb28/preseason.shtml",
        "http://hamsterwatch.com/bb28/070926.shtml",
        "http://hamsterwatch.com/bb28/071126.shtml",
    ]


def test_deduplicates_repeated_links_preserving_first_order():
    html = (
        '<A HREF="/bb28/070926.shtml">first mention</A>'
        '<A HREF="/bb28/071126.shtml">other</A>'
        '<A HREF="/bb28/070926.shtml">second mention</A>'
    )

    links = extract_archive_links(html, "http://hamsterwatch.com/bb28/days.shtml")

    assert links == [
        "http://hamsterwatch.com/bb28/070926.shtml",
        "http://hamsterwatch.com/bb28/071126.shtml",
    ]


def test_ignores_links_outside_the_bb28_recap_pattern():
    html = (
        '<A HREF="/bb28/links.shtml">Links page</A>'
        '<A HREF="/other/page.shtml">unrelated</A>'
        '<A HREF="/bb28/070926.shtml">a real recap page</A>'
    )

    links = extract_archive_links(html, "http://hamsterwatch.com/bb28/days.shtml")

    assert links == ["http://hamsterwatch.com/bb28/070926.shtml"]


def test_empty_or_malformed_index_returns_no_links():
    assert extract_archive_links("", "http://hamsterwatch.com/bb28/days.shtml") == []
    assert extract_archive_links("<html>no links here</html>", "http://hamsterwatch.com/bb28/days.shtml") == []
