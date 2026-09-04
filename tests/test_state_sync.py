"""Unit tests for production/state_sync.py's Knowledge -> HouseStatus
synchronization (sync_house_status_from_knowledge()) -- the fix for a
real production incident where Knowledge State and the separately-
persisted Engine game_state diverged and stayed diverged indefinitely.

See tests/test_game_state_reconciliation.py for the integration-level
tests (ProductionEngine.reconcile_game_state_from_knowledge(), the
/teach update hook, and startup reconciliation).
"""

from __future__ import annotations

from database.storage import Storage
from production.house_status import HouseStatus
from production.knowledge import KnowledgeStore, KnowledgeType
from production.state_sync import (
    RECOGNIZED_TOPICS,
    is_recognized_topic,
    sync_house_status_from_knowledge,
)


def _knowledge(tmp_path, monkeypatch) -> KnowledgeStore:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    return KnowledgeStore(storage=Storage())


# ==========================================================
# RECOGNIZED_TOPICS / is_recognized_topic() -- untouched, still exact.
# (Original coverage from before this file was extended for the
# sync fix -- preserved here rather than dropped.)
# ==========================================================


def test_hoh_topic_is_recognized() -> None:
    assert is_recognized_topic("HOH") is True
    assert is_recognized_topic("hoh") is True  # case-insensitive


def test_all_expected_topics_are_recognized() -> None:
    for topic in ("HOH", "NOMINEES", "VETO_WINNER", "HAVE_NOTS"):
        assert is_recognized_topic(topic) is True


def test_unknown_topic_is_not_recognized() -> None:
    assert is_recognized_topic("FAVORITE_SNACK") is False


def test_topic_matching_is_case_insensitive() -> None:
    assert is_recognized_topic("hoh") is True
    assert is_recognized_topic("Nominees") is True


def test_recognized_topics_constant_is_exactly_the_documented_set() -> None:
    assert set(RECOGNIZED_TOPICS) == {"HOH", "NOMINEES", "VETO_WINNER", "HAVE_NOTS"}


def test_recognized_topics_unchanged():
    assert RECOGNIZED_TOPICS == ("HOH", "NOMINEES", "VETO_WINNER", "HAVE_NOTS")


def test_is_recognized_topic_case_insensitive():
    assert is_recognized_topic("hoh") is True
    assert is_recognized_topic("HOH") is True
    assert is_recognized_topic("EVICTED") is False


# ==========================================================
# Basic field mapping -- string, list, bool.
# ==========================================================


def test_sync_maps_hoh(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")

    result = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.hoh == "Drew"


def test_sync_maps_nominees_splitting_comma_separated_names(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Devens, LaLa, Taylor", author_id=1, topic="NOMINEES")

    result = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.nominees == ("Devens", "LaLa", "Taylor")


def test_sync_maps_veto_winner_to_veto_holder(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")

    result = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.veto_holder == "Yash"


def test_sync_maps_veto_used_yes_to_true(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "YES", author_id=1, topic="VETO_USED")

    result = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.veto_used is True


def test_sync_maps_veto_used_no_to_false(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "NO", author_id=1, topic="VETO_USED")

    result = sync_house_status_from_knowledge(
        HouseStatus(veto_used=True), knowledge
    )

    assert result.veto_used is False


def test_sync_maps_have_nots(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Angela, Barrett", author_id=1, topic="HAVE_NOTS")

    result = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.have_nots == ("Angela", "Barrett")


# ==========================================================
# Unconfirmed values must never become fabricated engine data.
# ==========================================================


def test_unconfirmed_have_nots_becomes_empty_not_a_literal_name(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="HAVE_NOTS")

    result = sync_house_status_from_knowledge(
        HouseStatus(have_nots=("Chuk", "Rome")), knowledge
    )

    assert result.have_nots == ()
    assert "UNCONFIRMED" not in result.have_nots
    assert "Unconfirmed" not in result.have_nots


def test_unconfirmed_hoh_resets_to_empty_string_not_left_stale(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "unconfirmed", author_id=1, topic="HOH")

    result = sync_house_status_from_knowledge(HouseStatus(hoh="If Yash"), knowledge)

    assert result.hoh == ""


def test_unconfirmed_veto_winner_resets_to_empty_string(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "TBD", author_id=1, topic="VETO_WINNER")

    result = sync_house_status_from_knowledge(HouseStatus(veto_holder="LaLa"), knowledge)

    assert result.veto_holder == ""


def test_ambiguous_veto_used_text_leaves_existing_value_untouched(tmp_path, monkeypatch):
    # A strict bool field has no third "unknown" state to represent
    # "unconfirmed" -- deliberately left alone rather than guessing.
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="VETO_USED")

    result = sync_house_status_from_knowledge(
        HouseStatus(veto_used=True), knowledge
    )

    assert result.veto_used is True  # unchanged, not forced to False


# ==========================================================
# No Knowledge value taught for a topic at all -> field untouched.
# ==========================================================


def test_no_taught_value_leaves_field_completely_untouched(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)  # nothing taught at all

    original = HouseStatus(hoh="If Yash", nominees=("Angela", "Haley", "Kamu"))
    result = sync_house_status_from_knowledge(original, knowledge)

    assert result == original


# ==========================================================
# Fields with no Knowledge mapping are always passed through.
# ==========================================================


def test_feeds_field_is_never_touched(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")

    result = sync_house_status_from_knowledge(HouseStatus(feeds="down"), knowledge)

    assert result.feeds == "down"


# ==========================================================
# The exact production incident, reproduced directly.
# ==========================================================


def test_reproduces_the_exact_production_repair(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")
    knowledge.teach(KnowledgeType.STATE, "Devens, LaLa, Taylor", author_id=1, topic="NOMINEES")
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    knowledge.teach(KnowledgeType.STATE, "YES", author_id=1, topic="VETO_USED")
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="HAVE_NOTS")

    stale = HouseStatus(
        hoh="If Yash",
        nominees=("Angela", "Haley", "Kamu"),
        veto_holder="LaLa",
        veto_used=True,
        have_nots=(),
        feeds="down",
    )

    result = sync_house_status_from_knowledge(stale, knowledge)

    assert result.hoh == "Drew"
    assert result.nominees == ("Devens", "LaLa", "Taylor")
    assert result.veto_holder == "Yash"
    assert result.veto_used is True
    assert result.have_nots == ()
    assert result.feeds == "down"  # untouched -- no Knowledge mapping


# ==========================================================
# Purity -- never mutates its inputs.
# ==========================================================


def test_sync_never_mutates_knowledge_store(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")
    before = list(knowledge.active_items())

    sync_house_status_from_knowledge(HouseStatus(), knowledge)

    after = list(knowledge.active_items())
    assert [item.content for item in before] == [item.content for item in after]
    assert len(before) == len(after)


def test_sync_never_mutates_the_input_house_status(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")

    original = HouseStatus(hoh="If Yash")
    sync_house_status_from_knowledge(original, knowledge)

    assert original.hoh == "If Yash"  # the input object itself is untouched
