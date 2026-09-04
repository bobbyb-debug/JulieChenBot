"""Unit tests for production/state_sync.py's Knowledge -> HouseStatus
synchronization (sync_house_status_from_knowledge()) -- the fix for a
real production incident where Knowledge State and the separately-
persisted Engine game_state diverged and stayed diverged indefinitely
-- and for its topic-alias resolution (_resolve_active_state()), the
fix for a second real finding: production Knowledge contained
independently-active duplicate topic spellings ("VETO WINNER" vs.
"VETO_WINNER") that a naive single-topic lookup could resolve
inconsistently or silently pick one of.

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
# Basic field mapping -- string, list, bool. sync_house_status_from_knowledge()
# returns (HouseStatus, conflicted_topics) -- every happy-path test
# here also asserts conflicted_topics == [] to prove the alias
# machinery stays silent when there's nothing to report.
# ==========================================================


def test_sync_maps_hoh(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.hoh == "Drew"
    assert conflicts == []


def test_sync_maps_nominees_splitting_comma_separated_names(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Devens, LaLa, Taylor", author_id=1, topic="NOMINEES")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.nominees == ("Devens", "LaLa", "Taylor")
    assert conflicts == []


def test_sync_maps_veto_winner_to_veto_holder(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.veto_holder == "Yash"
    assert conflicts == []


def test_sync_maps_veto_used_yes_to_true(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "YES", author_id=1, topic="VETO_USED")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.veto_used is True
    assert conflicts == []


def test_sync_maps_veto_used_no_to_false(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "NO", author_id=1, topic="VETO_USED")

    result, conflicts = sync_house_status_from_knowledge(
        HouseStatus(veto_used=True), knowledge
    )

    assert result.veto_used is False
    assert conflicts == []


def test_sync_maps_have_nots(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Angela, Barrett", author_id=1, topic="HAVE_NOTS")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.have_nots == ("Angela", "Barrett")
    assert conflicts == []


# ==========================================================
# Unconfirmed values must never become fabricated engine data.
# ==========================================================


def test_unconfirmed_have_nots_becomes_empty_not_a_literal_name(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="HAVE_NOTS")

    result, conflicts = sync_house_status_from_knowledge(
        HouseStatus(have_nots=("Chuk", "Rome")), knowledge
    )

    assert result.have_nots == ()
    assert "UNCONFIRMED" not in result.have_nots
    assert "Unconfirmed" not in result.have_nots
    assert conflicts == []


def test_unconfirmed_hoh_resets_to_empty_string_not_left_stale(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "unconfirmed", author_id=1, topic="HOH")

    result, _conflicts = sync_house_status_from_knowledge(HouseStatus(hoh="If Yash"), knowledge)

    assert result.hoh == ""


def test_unconfirmed_veto_winner_resets_to_empty_string(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "TBD", author_id=1, topic="VETO_WINNER")

    result, _conflicts = sync_house_status_from_knowledge(
        HouseStatus(veto_holder="LaLa"), knowledge
    )

    assert result.veto_holder == ""


def test_ambiguous_veto_used_text_leaves_existing_value_untouched(tmp_path, monkeypatch):
    # A strict bool field has no third "unknown" state to represent
    # "unconfirmed" -- deliberately left alone rather than guessing.
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="VETO_USED")

    result, _conflicts = sync_house_status_from_knowledge(
        HouseStatus(veto_used=True), knowledge
    )

    assert result.veto_used is True  # unchanged, not forced to False


# ==========================================================
# No Knowledge value taught for a topic at all -> field untouched.
# ==========================================================


def test_no_taught_value_leaves_field_completely_untouched(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)  # nothing taught at all

    original = HouseStatus(hoh="If Yash", nominees=("Angela", "Haley", "Kamu"))
    result, conflicts = sync_house_status_from_knowledge(original, knowledge)

    assert result == original
    assert conflicts == []


# ==========================================================
# Fields with no Knowledge mapping are always passed through.
# ==========================================================


def test_feeds_field_is_never_touched(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")

    result, _conflicts = sync_house_status_from_knowledge(HouseStatus(feeds="down"), knowledge)

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

    result, conflicts = sync_house_status_from_knowledge(stale, knowledge)

    assert result.hoh == "Drew"
    assert result.nominees == ("Devens", "LaLa", "Taylor")
    assert result.veto_holder == "Yash"
    assert result.veto_used is True
    assert result.have_nots == ()
    assert result.feeds == "down"  # untouched -- no Knowledge mapping
    assert conflicts == []


def test_reproduces_the_second_production_state_after_lala_evicted(tmp_path, monkeypatch):
    """The game moved on mid-investigation: HOH flipped to Barrett,
    nominees/veto were reset to UNCONFIRMED for the new week. This is
    the second real snapshot pulled live from production -- proving
    the sync isn't just correct for one hand-picked scenario."""

    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="NOMINEES")
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="VETO_WINNER")
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="VETO_USED")

    stale = HouseStatus(
        hoh="Barrett",  # RSS had already independently caught up on HOH
        nominees=("Angela", "Haley", "Kamu"),  # still stale from a prior week
        veto_holder="If Yash",  # the demonstrated garbled parse
        veto_used=True,
        feeds="down",
    )

    result, conflicts = sync_house_status_from_knowledge(stale, knowledge)

    assert result.hoh == "Barrett"
    assert result.nominees == ()
    assert result.veto_holder == ""
    assert result.veto_used is True  # ambiguous bool -- left untouched, documented
    assert result.feeds == "down"
    assert conflicts == []


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


# ==========================================================
# Topic aliasing -- the duplicate "VETO WINNER"/"VETO_WINNER" and
# "BB BLOCKBUSTER"/"BB_BLOCKBUSTER" finding from real production
# Knowledge. KnowledgeStore.active_state() normalizes only via
# .strip().upper() -- it never collapses a space/underscore
# difference, so both spellings can be independently active. Fixed
# entirely inside state_sync.py's own lookup (_resolve_active_state());
# KnowledgeStore itself, and its stored data, are never touched.
# ==========================================================


def test_alias_variant_with_space_is_recognized_for_veto_winner(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    # Taught under the SPACED variant only -- the underscore form was
    # never written for this topic at all.
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO WINNER")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.veto_holder == "Yash"
    assert conflicts == []


def test_alias_variants_agreeing_is_not_a_conflict(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO WINNER")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.veto_holder == "Yash"
    assert conflicts == []


def test_alias_variants_agreeing_case_insensitively_is_not_a_conflict(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "yash", author_id=1, topic="VETO_WINNER")
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO WINNER")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert conflicts == []


def test_conflicting_alias_variants_never_guess_a_value(tmp_path, monkeypatch):
    """The exact real-world risk: 'VETO WINNER': 'LaLa' (old, taught
    weeks ago) and 'VETO_WINNER': 'UNCONFIRMED' (fresh) independently
    active at once. Must never silently pick either one."""

    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "LaLa", author_id=1, topic="VETO WINNER")
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="VETO_WINNER")

    result, conflicts = sync_house_status_from_knowledge(
        HouseStatus(veto_holder="If Yash"), knowledge
    )

    assert conflicts == ["VETO_WINNER"]
    # Field left exactly as it was -- neither "LaLa" nor "" (the
    # unconfirmed-reset value) was guessed at.
    assert result.veto_holder == "If Yash"


def test_conflicting_alias_variants_do_not_affect_other_fields(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "LaLa", author_id=1, topic="VETO WINNER")
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="VETO_WINNER")
    knowledge.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.hoh == "Barrett"
    assert conflicts == ["VETO_WINNER"]


def test_multiple_topics_can_each_report_their_own_conflict(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "LaLa", author_id=1, topic="VETO WINNER")
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    knowledge.teach(KnowledgeType.STATE, "YES", author_id=1, topic="VETO USED")
    knowledge.teach(KnowledgeType.STATE, "NO", author_id=1, topic="VETO_USED")

    _result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert set(conflicts) == {"VETO_WINNER", "VETO_USED"}


def test_alias_resolution_never_mutates_knowledge_store(tmp_path, monkeypatch):
    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "LaLa", author_id=1, topic="VETO WINNER")
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="VETO_WINNER")
    before = len(knowledge.active_items())

    sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert len(knowledge.active_items()) == before


def test_freshest_alias_variant_wins_when_values_actually_agree_but_differ_in_case(
    tmp_path, monkeypatch
):
    """Not a real conflict (content matches case-insensitively), but
    proves the returned item is deterministic (most-recently-updated)
    rather than depending on dict/list ordering."""

    knowledge = _knowledge(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "yash", author_id=1, topic="VETO WINNER")
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")

    result, conflicts = sync_house_status_from_knowledge(HouseStatus(), knowledge)

    assert result.veto_holder in ("yash", "Yash")  # either casing is fine
    assert conflicts == []
