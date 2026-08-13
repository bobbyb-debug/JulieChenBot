"""Tests for database/hamsterwatch_archive.py.

Covers idempotent storage, cosmetic-vs-significant change detection,
and the retrieval layer /recap reads from.
"""

from __future__ import annotations

import pytest

from database.hamsterwatch_archive import HamsterwatchArchive


@pytest.fixture
def archive(tmp_path):
    return HamsterwatchArchive(db_path=tmp_path / "hamsterwatch_archive.db")


def _long_content(base: str, length: int = 1200) -> str:
    """Builds realistic-length recap content (real sections run
    several thousand characters) so the significance thresholds are
    exercised the way they'd behave on real content."""

    repeated = (base + " ") * (length // (len(base) + 1) + 1)
    return repeated[:length]


ARTICLE_KWARGS = dict(
    page_url="http://hamsterwatch.com/bb28/070926.shtml",
    section_slug="day-3",
    heading="Day 3 - Thursday - July 9, 2026",
    article_date="2026-07-09",
    bb_day=3,
)


# ==========================================================
# Idempotency / provenance
# ==========================================================


def test_new_article_is_new_and_carries_default_source(archive):
    outcome = archive.upsert(
        **ARTICLE_KWARGS,
        content=_long_content("Move-in day chaos in the house."),
        summary="Move-in day chaos.",
    )

    assert outcome.is_new is True
    assert outcome.significant_change is True
    assert outcome.article.source == "Hamsterwatch"
    assert outcome.article.id is not None


def test_reimporting_identical_article_does_not_duplicate(archive):
    kwargs = dict(
        **ARTICLE_KWARGS,
        content=_long_content("Move-in day chaos in the house."),
        summary="Move-in day chaos.",
    )

    archive.upsert(**kwargs)
    archive.upsert(**kwargs)
    archive.upsert(**kwargs)

    assert archive.count() == 1


def test_reimporting_unchanged_content_is_not_flagged_as_new_or_changed(archive):
    kwargs = dict(
        **ARTICLE_KWARGS,
        content=_long_content("Move-in day chaos in the house."),
        summary="Move-in day chaos.",
    )

    archive.upsert(**kwargs)
    outcome = archive.upsert(**kwargs)

    assert outcome.is_new is False
    assert outcome.significant_change is False


def test_different_slugs_on_the_same_page_are_separate_articles(archive):
    archive.upsert(
        **ARTICLE_KWARGS,
        content=_long_content("Day three recap content."),
        summary="s",
    )
    other = dict(ARTICLE_KWARGS)
    other.update(section_slug="day-4", heading="Day 4 - Friday - July 10, 2026", bb_day=4)
    archive.upsert(**other, content=_long_content("Day four recap content."), summary="s")

    assert archive.count() == 2
    assert archive.known_page_urls() == {ARTICLE_KWARGS["page_url"]}


# ==========================================================
# Cosmetic vs. meaningful change detection
# ==========================================================


def test_small_edit_is_stored_but_not_significant(archive):
    base = _long_content(
        "Kamu and Yash talked strategy in the gym about targeting Chuk next week."
    )
    archive.upsert(**ARTICLE_KWARGS, content=base, summary="s")

    edited = base + " Fixed a typo."  # well under both thresholds
    outcome = archive.upsert(**ARTICLE_KWARGS, content=edited, summary="s")

    assert outcome.is_new is False
    assert outcome.significant_change is False
    # The edit is still persisted, even though it's not "announcement-worthy".
    assert archive.recent(1)[0].content == edited


def test_large_edit_is_significant(archive):
    base = _long_content(
        "Kamu and Yash talked strategy in the gym about targeting Chuk next week."
    )
    archive.upsert(**ARTICLE_KWARGS, content=base, summary="s")

    edited = base + (
        " A whole new paragraph was added describing the veto ceremony in "
        "detail, including who spoke and what was decided about the block."
    ) * 3
    outcome = archive.upsert(**ARTICLE_KWARGS, content=edited, summary="s")

    assert outcome.is_new is False
    assert outcome.significant_change is True


# ==========================================================
# Discovery bookkeeping
# ==========================================================


def test_known_page_urls_and_latest_page_url(archive):
    archive.upsert(**ARTICLE_KWARGS, content=_long_content("a"), summary="s")

    later = dict(
        page_url="http://hamsterwatch.com/bb28/071126.shtml",
        section_slug="day-5",
        heading="Day 5 - Saturday - July 11, 2026",
        article_date="2026-07-11",
        bb_day=5,
    )
    archive.upsert(**later, content=_long_content("b"), summary="s")

    assert archive.known_page_urls() == {
        "http://hamsterwatch.com/bb28/070926.shtml",
        "http://hamsterwatch.com/bb28/071126.shtml",
    }
    assert archive.latest_page_url() == "http://hamsterwatch.com/bb28/071126.shtml"


def test_count_reflects_stored_articles(archive):
    assert archive.count() == 0
    archive.upsert(**ARTICLE_KWARGS, content=_long_content("a"), summary="s")
    assert archive.count() == 1


# ==========================================================
# Retrieval
# ==========================================================


def _seed_days(archive, *days: int) -> None:
    for day in days:
        kwargs = dict(ARTICLE_KWARGS)
        kwargs.update(
            section_slug=f"day-{day}",
            heading=f"Day {day} recap heading",
            bb_day=day,
            article_date=f"2026-07-{day:02d}",
        )
        archive.upsert(**kwargs, content=_long_content(f"content for day {day}"), summary="s")


def test_recent_orders_newest_bb_day_first(archive):
    _seed_days(archive, 1, 3, 2)

    recent = archive.recent(limit=3)

    assert [a.bb_day for a in recent] == [3, 2, 1]


def test_recent_respects_limit(archive):
    _seed_days(archive, 1, 2, 3, 4, 5)

    assert len(archive.recent(limit=2)) == 2


def test_by_bb_day_and_by_date(archive):
    _seed_days(archive, 9)

    assert [a.section_slug for a in archive.by_bb_day(9)] == ["day-9"]
    assert [a.section_slug for a in archive.by_date("2026-07-09")] == ["day-9"]
    assert archive.by_bb_day(999) == []
    assert archive.by_date("2099-01-01") == []


def test_search_finds_article_mentioning_a_player(archive):
    kwargs1 = dict(ARTICLE_KWARGS)
    archive.upsert(
        **kwargs1,
        content=_long_content("LaLa and Devens discuss the veto plan for next week."),
        summary="s",
    )
    kwargs2 = dict(ARTICLE_KWARGS)
    kwargs2.update(section_slug="day-4", heading="Day 4", bb_day=4)
    archive.upsert(
        **kwargs2,
        content=_long_content("Kamu ran his usual loops with Yash in the gym."),
        summary="s",
    )

    results = archive.search(["LaLa"])

    assert len(results) == 1
    assert results[0].section_slug == "day-3"


def test_search_ranks_more_relevant_match_first(archive):
    """Two articles both mention the keyword, but one is dense with
    mentions (clearly about that player) and the other mentions it
    once in a sea of unrelated text. FTS5's bm25 ranking must put the
    denser/more relevant match first — this is what /recap relies on
    when find_relevant() has more keyword hits than its limit."""

    kwargs_sparse = dict(ARTICLE_KWARGS)
    archive.upsert(
        **kwargs_sparse,
        content=(
            "Unrelated filler chatter about snacks and naps repeated many "
            "times with nothing of substance. " * 15
            + " LaLa was mentioned once in passing."
        ),
        summary="s",
    )

    kwargs_dense = dict(ARTICLE_KWARGS)
    kwargs_dense.update(section_slug="day-4", heading="Day 4", bb_day=4)
    archive.upsert(
        **kwargs_dense,
        content=(
            "LaLa talked strategy. LaLa is the target this week. Everyone "
            "keeps discussing LaLa's game and LaLa's alliance nonstop."
        ),
        summary="s",
    )

    results = archive.search(["LaLa"])

    assert len(results) == 2
    assert results[0].section_slug == "day-4"  # dense match ranked first
    assert results[1].section_slug == "day-3"


def test_search_with_no_matches_returns_empty(archive):
    archive.upsert(
        **ARTICLE_KWARGS,
        content=_long_content("Kamu ran his usual loops."),
        summary="s",
    )

    assert archive.search(["Zzznotaplayer"]) == []


def test_search_handles_apostrophes_in_names_safely(archive):
    archive.upsert(
        **ARTICLE_KWARGS,
        content=_long_content("O'Connell joined the celebrity panel for Unlocked."),
        summary="s",
    )

    results = archive.search(["O'Connell"])

    assert len(results) == 1


def test_search_with_only_blank_keywords_returns_empty(archive):
    archive.upsert(**ARTICLE_KWARGS, content=_long_content("content"), summary="s")

    assert archive.search([]) == []
    assert archive.search(["", "   "]) == []


def test_find_relevant_prioritizes_keyword_match_over_recency(archive):
    kwargs1 = dict(ARTICLE_KWARGS)  # bb_day 3, older
    archive.upsert(
        **kwargs1,
        content=_long_content("LaLa and Devens discuss the veto plan for next week."),
        summary="s",
    )
    kwargs2 = dict(ARTICLE_KWARGS)
    kwargs2.update(section_slug="day-30", heading="Day 30", bb_day=30)  # newer, irrelevant
    archive.upsert(
        **kwargs2,
        content=_long_content("Random unrelated chatter about house pets and snacks."),
        summary="s",
    )

    relevant = archive.find_relevant(["LaLa"], limit=1)

    assert len(relevant) == 1
    assert relevant[0].section_slug == "day-3"


def test_find_relevant_fills_remaining_slots_with_recent_articles(archive):
    _seed_days(archive, 1, 2)

    relevant = archive.find_relevant(["Zzznotaplayer"], limit=2)

    assert [a.section_slug for a in relevant] == ["day-2", "day-1"]


def test_find_relevant_with_no_keywords_returns_recent(archive):
    _seed_days(archive, 1, 2, 3)

    relevant = archive.find_relevant(None, limit=1)

    assert len(relevant) == 1
    assert relevant[0].bb_day == 3


def test_find_relevant_never_exceeds_limit(archive):
    _seed_days(archive, 1, 2, 3, 4, 5)

    assert len(archive.find_relevant(None, limit=2)) == 2
