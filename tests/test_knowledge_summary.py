"""Tests for production/knowledge_summary.py -- the deterministic,
no-AI-call detector for a genuine "tell me everything you know" style
capability question (as distinct from an ordinary factual question
that happens to share a word, or is scoped to one topic with "about"),
and the deterministic, read-only collect_summary_metadata() that turns
real store state into safe, content-free summary data.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from database.hamsterwatch_archive import HamsterwatchArchive
from database.historical_events import HistoricalEventStore
from database.storage import Storage
from production.competition import CompetitionState, CompetitionType
from production.house_status import HouseStatus
from production.knowledge import KnowledgeStore, KnowledgeType
from production.knowledge_summary import (
    collect_summary_metadata,
    is_broad_knowledge_query,
)
from production.memory import MemoryStore


# ==========================================================
# True positives: genuine capability/knowledge-overview questions
# ==========================================================


def test_tell_me_everything_you_know_is_broad():
    assert is_broad_knowledge_query("Tell me everything you know.") is True


def test_bare_what_do_you_know_is_broad():
    assert is_broad_knowledge_query("What do you know?") is True


def test_what_can_you_do_is_broad():
    assert is_broad_knowledge_query("Julie, what can you do?") is True


def test_what_are_you_capable_of_is_broad():
    assert is_broad_knowledge_query("What are you capable of?") is True


def test_what_knowledge_do_you_have_is_broad():
    assert is_broad_knowledge_query("What knowledge do you have access to?") is True


def test_is_case_insensitive():
    assert is_broad_knowledge_query("TELL ME EVERYTHING YOU KNOW") is True


def test_trigger_phrase_anywhere_in_message_is_broad():
    assert is_broad_knowledge_query("Hey Julie, tell me everything you know please!") is True


# ==========================================================
# False positives to avoid: ordinary/scoped questions
# ==========================================================


def test_ordinary_hoh_question_is_not_broad():
    assert is_broad_knowledge_query("Who is the current HoH?") is False


def test_question_containing_know_alone_is_not_broad():
    assert is_broad_knowledge_query("Do you know who is HOH?") is False


def test_question_containing_everything_alone_is_not_broad():
    assert is_broad_knowledge_query("Did everything go okay during the veto ceremony?") is False


def test_scoped_with_about_is_not_broad():
    assert (
        is_broad_knowledge_query("Tell me everything you know about Taylor's HOH")
        is False
    )


def test_what_do_you_know_about_topic_is_not_broad():
    assert is_broad_knowledge_query("What do you know about Week 2?") is False


# ==========================================================
# "about" doesn't automatically mean narrowly scoped -- a genuinely
# broad historical question ("about the history of BB28") must still
# be recognized as broad, distinct from a specific-topic one
# ("about Taylor's HOH").
# ==========================================================


def test_about_the_history_of_the_season_is_still_broad():
    assert (
        is_broad_knowledge_query("Tell me everything you know about the history of Big Brother 28")
        is True
    )


def test_about_the_season_is_still_broad():
    assert is_broad_knowledge_query("What do you know about the season?") is True


def test_plain_banter_is_not_broad():
    assert is_broad_knowledge_query("lol that nomination is wild") is False


def test_empty_string_is_not_broad():
    assert is_broad_knowledge_query("") is False


# ==========================================================
# collect_summary_metadata(): deterministic, read-only, real data
# ==========================================================


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """One shared Storage-backed KnowledgeStore/MemoryStore pair (the
    real production wiring: both live in the same storage.json under
    different keys -- see production/knowledge.py and
    production/memory.py), plus a fresh HistoricalEventStore/
    HamsterwatchArchive on their own temp sqlite files. Storage.FILE
    is a class attribute pointing at a real path by default, so it
    must be monkeypatched before Storage() is constructed -- the same
    pattern tests/test_knowledge_summary_boundary.py's _setup() uses.
    """

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    return SimpleNamespace(
        knowledge=KnowledgeStore(storage=storage),
        memory=MemoryStore(storage=storage),
        historical_events=HistoricalEventStore(db_path=tmp_path / "historical_events.db"),
        hamsterwatch=HamsterwatchArchive(db_path=tmp_path / "hamsterwatch.db"),
    )


def _metadata(stores, *, channel_id=1, house_status=None, competition=None,
              hamsterwatch_archive=None, knowledge_items=None):
    return collect_summary_metadata(
        knowledge_items=(
            stores.knowledge.active_items() if knowledge_items is None else knowledge_items
        ),
        historical_events=stores.historical_events,
        hamsterwatch_archive=hamsterwatch_archive,
        house_status=house_status or HouseStatus(),
        competition=competition or CompetitionState(),
        memory_store=stores.memory,
        channel_id=channel_id,
    )


def test_metadata_reports_only_state_topics_actually_set(stores):
    stores.knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    stores.knowledge.teach(KnowledgeType.STATE, "Alex, Sam", author_id=1, topic="Nominees")

    metadata = _metadata(stores)

    # KnowledgeStore.teach() normalizes topics to uppercase.
    assert set(metadata.official_state_topics) == {"HOH", "NOMINEES"}


def test_metadata_reports_empty_state_topics_when_none_set(stores):
    metadata = _metadata(stores)

    assert metadata.official_state_topics == ()


def test_metadata_counts_admin_taught_knowledge_by_type(stores):
    stores.knowledge.teach(
        KnowledgeType.RULE, "Have-Nots are set by the house-status image.", author_id=1
    )
    stores.knowledge.teach(KnowledgeType.FACT, "Yash made final 4.", author_id=1)
    stores.knowledge.teach(KnowledgeType.FACT, "Taylor was evicted.", author_id=1)
    stores.knowledge.teach(KnowledgeType.CORRECTION, "Actually it was Alex.", author_id=1)

    metadata = _metadata(stores)

    assert metadata.admin_rule_count == 1
    assert metadata.admin_fact_count == 2
    assert metadata.admin_correction_count == 1


def test_metadata_never_contains_knowledge_content_only_counts(stores):
    secret_content = "a very specific piece of admin knowledge content"
    stores.knowledge.teach(KnowledgeType.FACT, secret_content, author_id=1)

    metadata = _metadata(stores)

    assert secret_content not in repr(metadata)
    assert metadata.admin_fact_count == 1


def test_metadata_distinguishes_zero_verified_hoh_records_from_capability(stores):
    metadata = _metadata(stores)

    assert metadata.historical_hoh_known_winners_count == 0


def test_metadata_reports_verified_hoh_winner_count(stores):
    claim = stores.historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=1, week_number=1, winner="Taylor",
        source_type="manual_admin_note", source_ref="x",
    )
    stores.historical_events.verify_hoh(claim.id)

    metadata = _metadata(stores)

    assert metadata.historical_hoh_known_winners_count == 1


def test_metadata_never_counts_unverified_hoh_claims(stores):
    stores.historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=1, week_number=1, winner="Barrett",
        source_type="fan_wiki", source_ref="x",
    )  # never verified

    metadata = _metadata(stores)

    assert metadata.historical_hoh_known_winners_count == 0


def test_metadata_reports_hamsterwatch_article_count(stores):
    stores.hamsterwatch.upsert(
        page_url="http://hamsterwatch.com/bb28/day1.shtml",
        section_slug="day-1",
        heading="Day 1 recap",
        article_date="2026-07-01",
        bb_day=1,
        content="content " * 200,
        summary="summary",
    )

    metadata = _metadata(stores, hamsterwatch_archive=stores.hamsterwatch)

    assert metadata.hamsterwatch_article_count == 1


def test_metadata_reports_zero_hamsterwatch_articles_when_archive_is_none(stores):
    metadata = _metadata(stores, hamsterwatch_archive=None)

    assert metadata.hamsterwatch_article_count == 0


def test_metadata_reports_live_feed_populated_when_house_status_has_data(stores):
    metadata = _metadata(stores, house_status=HouseStatus(hoh="Yash"))

    assert metadata.live_feed_populated is True


def test_metadata_reports_live_feed_not_populated_when_empty(stores):
    metadata = _metadata(stores, house_status=HouseStatus())

    assert metadata.live_feed_populated is False


def test_metadata_reports_live_feed_populated_when_competition_active(stores):
    metadata = _metadata(
        stores, competition=CompetitionState(competition=CompetitionType.HOH, active=True)
    )

    assert metadata.live_feed_populated is True


def test_metadata_scopes_memory_count_to_the_requesting_channel_only(stores):
    stores.memory.remember(
        channel_id=1, author_id=1, author_name="Alex", content="in-channel note"
    )
    stores.memory.remember(
        channel_id=2, author_id=1, author_name="Alex", content="a different channel's note"
    )

    metadata = _metadata(stores, channel_id=1)

    assert metadata.channel_memory_count == 1


def test_metadata_never_exposes_memory_content_only_a_count(stores):
    private_note = "Bobby's real first name is something private"
    stores.memory.remember(
        channel_id=1, author_id=1, author_name="Alex", content=private_note
    )

    metadata = _metadata(stores, channel_id=1)

    assert private_note not in repr(metadata)
    assert metadata.channel_memory_count == 1
