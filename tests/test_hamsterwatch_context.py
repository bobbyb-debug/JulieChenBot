"""Tests for production/hamsterwatch_context.py -- the deterministic,
FTS5-backed retrieval layer that gives Julie's /chat context access to
the Hamsterwatch archive.

Covers keyword extraction, explicit "Day N" detection/routing, the
three retrieval paths (day lookup, keyword search, recency fallback
for a vague query), result limits, metadata preservation, and --
critically -- that a genuine miss on a targeted question never
silently substitutes unrelated recent material (the exact failure
mode this module exists to avoid; see the module's own docstring).
"""

from __future__ import annotations

import pytest

from database.hamsterwatch_archive import HamsterwatchArchive
from production.hamsterwatch_context import (
    HistoricalContextResult,
    extract_bb_day,
    extract_keywords,
    retrieve_historical_context,
)


@pytest.fixture
def archive(tmp_path):
    return HamsterwatchArchive(db_path=tmp_path / "hamsterwatch_archive.db")


def _long(base: str, length: int = 1200) -> str:
    repeated = (base + " ") * (length // (len(base) + 1) + 1)
    return repeated[:length]


def _seed(archive, bb_day: int, content: str, summary: str = "summary") -> None:
    archive.upsert(
        page_url="http://hamsterwatch.com/bb28/test.shtml",
        section_slug=f"day-{bb_day}",
        heading=f"Day {bb_day} recap heading",
        article_date=f"2026-07-{bb_day:02d}",
        bb_day=bb_day,
        content=_long(content),
        summary=summary,
    )


# ==========================================================
# extract_keywords()
# ==========================================================


def test_extract_keywords_drops_stopwords_and_short_words():
    keywords = extract_keywords("What was happening between LaLa and Devens?")

    assert "lala" in keywords
    assert "devens" in keywords
    assert "what" not in keywords
    assert "was" not in keywords
    assert "and" not in keywords


def test_extract_keywords_lowercases():
    assert extract_keywords("YASH") == ["yash"]


def test_extract_keywords_returns_empty_for_a_purely_generic_question():
    assert extract_keywords("What is going on?") == []


def test_extract_keywords_drops_ordinary_chat_filler():
    assert extract_keywords("lol") == []
    assert extract_keywords("thanks Julie") == []
    assert extract_keywords("good morning") == []
    assert extract_keywords("what do you think?") == []
    assert extract_keywords("that's funny") == []
    assert extract_keywords("tell me a joke") == []


def test_extract_keywords_drops_vague_context_signal_words():
    """"house"/"week"/etc are real signal (see _has_vague_context_signal)
    but too generic to be useful FTS5 search terms on their own."""

    assert extract_keywords("What's going on in the house?") == []
    assert extract_keywords("What's happening this week?") == []
    assert extract_keywords("What happened lately?") == []


def test_extract_keywords_still_finds_a_real_keyword_alongside_a_signal_word():
    assert extract_keywords("What did Yash do this week?") == ["yash"]


# ==========================================================
# extract_bb_day()
# ==========================================================


def test_extract_bb_day_finds_explicit_day_reference():
    assert extract_bb_day("What happened on Day 12?") == 12
    assert extract_bb_day("day 37 recap please") == 37
    assert extract_bb_day("Tell me about Day  5") == 5  # extra whitespace still matches


def test_extract_bb_day_returns_none_when_absent():
    assert extract_bb_day("What happened before the veto ceremony?") is None


def test_extract_bb_day_is_case_insensitive():
    assert extract_bb_day("DAY 9") == 9


# ==========================================================
# retrieve_historical_context() -- Day-N path
# ==========================================================


def test_day_n_query_retrieves_that_days_material(archive):
    _seed(archive, 12, "LaLa and Devens discuss the veto plan.")
    _seed(archive, 13, "Unrelated day thirteen content.")

    result = retrieve_historical_context("What happened on Day 12?", archive)

    assert result.matched_bb_day == 12
    assert len(result.articles) == 1
    assert result.articles[0].bb_day == 12


def test_day_n_miss_returns_empty_not_a_different_day(archive):
    """The critical guard this feature exists to enforce: a specific,
    explicit day request that has no archived material must NOT be
    answered with a different day's content."""

    _seed(archive, 5, "Some other day's content entirely.")

    result = retrieve_historical_context("What happened on Day 12?", archive)

    assert result.articles == []
    assert bool(result) is False


def test_day_n_path_ignores_keywords_in_the_same_query(archive):
    """An explicit day reference takes priority over keyword search,
    even when the query also contains searchable words."""

    _seed(archive, 12, "Content specifically about Day 12.")
    _seed(archive, 40, "Devens talked strategy on a totally different day.")

    result = retrieve_historical_context(
        "What did Devens do on Day 12?", archive
    )

    assert result.matched_bb_day == 12
    assert [a.bb_day for a in result.articles] == [12]


# ==========================================================
# retrieve_historical_context() -- keyword path
# ==========================================================


def test_keyword_query_uses_search_not_find_relevant(archive):
    _seed(archive, 3, "LaLa and Devens discuss the veto plan for next week.")
    _seed(archive, 30, "Random unrelated chatter about house pets and snacks.")

    result = retrieve_historical_context("What was LaLa doing?", archive)

    assert result.matched_bb_day is None
    assert len(result.articles) == 1
    assert result.articles[0].bb_day == 3


def test_keyword_query_with_zero_real_matches_returns_empty_not_recent(archive):
    """The other half of the critical guard: a targeted keyword
    question with no genuine FTS5 match must not be backfilled with
    unrelated recent entries (archive.find_relevant()'s behavior,
    deliberately NOT used here -- see module docstring)."""

    _seed(archive, 1, "Completely unrelated content about the kitchen.")
    _seed(archive, 2, "More completely unrelated content about naps.")

    result = retrieve_historical_context(
        "What was Zzznotaplayer doing?", archive
    )

    assert result.articles == []


def test_keyword_query_respects_limit(archive):
    for day in range(1, 6):
        _seed(archive, day, "Yash won the competition again this week.")

    result = retrieve_historical_context("What did Yash do?", archive, limit=2)

    assert len(result.articles) == 2


# ==========================================================
# retrieve_historical_context() -- vague-but-BB-related query ->
# recency fallback, vs. ordinary chatter -> nothing at all
# ==========================================================


def test_vague_bb_context_query_falls_back_to_recent(archive):
    """A question with no searchable keywords but a clear BB/season
    signal word ("the house") still deserves recent background."""

    _seed(archive, 1, "Day one content.")
    _seed(archive, 5, "Day five content.")

    result = retrieve_historical_context(
        "What's going on in the house?", archive
    )

    assert result.matched_bb_day is None
    # Default limit (3) exceeds the 2 seeded articles, so both come
    # back, most recent (highest bb_day) first.
    assert [a.bb_day for a in result.articles] == [5, 1]


def test_vague_bb_context_query_respects_limit(archive):
    for day in range(1, 6):
        _seed(archive, day, "generic content")

    result = retrieve_historical_context(
        "what's happening this week?", archive, limit=2
    )

    assert len(result.articles) == 2


@pytest.mark.parametrize(
    "message",
    [
        "lol",
        "thanks Julie",
        "good morning",
        "what do you think?",
        "that's funny",
        "tell me a joke",
        "what's up",
        "What's going on?",
    ],
)
def test_ordinary_chatter_receives_no_historical_context(archive, message):
    """The behavior this fix exists to guarantee: chatting with Julie
    about nothing in particular must never pull in random Hamsterwatch
    material, even when the archive has content and even though these
    queries yield no real search keywords (the same shape a vague-but-
    BB-related query has). Only an explicit BB/season signal word
    should unlock the recency fallback -- see
    test_vague_bb_context_query_falls_back_to_recent."""

    _seed(archive, 1, "Some recent recap content that must not leak in.")
    _seed(archive, 5, "More recap content that must not leak in.")

    result = retrieve_historical_context(message, archive)

    assert result.articles == []
    assert bool(result) is False


def test_empty_archive_returns_empty_for_any_query_shape(archive):
    assert retrieve_historical_context("Day 12", archive).articles == []
    assert retrieve_historical_context("Yash", archive).articles == []
    assert retrieve_historical_context("what's up", archive).articles == []


# ==========================================================
# Metadata preservation
# ==========================================================


def test_retrieved_articles_preserve_day_date_heading_and_url(archive):
    archive.upsert(
        page_url="http://hamsterwatch.com/bb28/081226.shtml",
        section_slug="day-12",
        heading="Day 12 - Wednesday - July 12, 2026",
        article_date="2026-07-12",
        bb_day=12,
        content=_long("Full recap content for day twelve."),
        summary="Short summary.",
    )

    result = retrieve_historical_context("Day 12", archive)
    article = result.articles[0]

    assert article.bb_day == 12
    assert article.article_date == "2026-07-12"
    assert article.heading == "Day 12 - Wednesday - July 12, 2026"
    assert article.page_url == "http://hamsterwatch.com/bb28/081226.shtml"
    assert "Full recap content" in article.content
    assert article.summary == "Short summary."


# ==========================================================
# Read-only guarantee
# ==========================================================


def test_retrieval_never_mutates_the_archive(archive):
    _seed(archive, 1, "original content")
    before = archive.count()

    retrieve_historical_context("Day 1", archive)
    retrieve_historical_context("anything", archive)
    retrieve_historical_context("what's up", archive)

    assert archive.count() == before


# ==========================================================
# HistoricalContextResult
# ==========================================================


def test_historical_context_result_is_falsy_when_empty():
    assert not HistoricalContextResult()
    assert not HistoricalContextResult(articles=[])


def test_historical_context_result_is_truthy_with_articles(archive):
    _seed(archive, 1, "content")
    result = retrieve_historical_context("Day 1", archive)
    assert bool(result) is True
