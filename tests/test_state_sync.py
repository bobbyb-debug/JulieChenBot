"""Tests for production/state_sync.py -- mapping a manual STATE topic
onto the real HouseStatus object /hoh, /noms, /nominees, /veto read.
"""

from __future__ import annotations

from production.house_status import HouseStatus
from production.state_sync import RECOGNIZED_TOPICS, apply_state_topic, is_recognized_topic


def test_hoh_topic_is_recognized() -> None:
    assert is_recognized_topic("HOH") is True
    assert is_recognized_topic("hoh") is True  # case-insensitive


def test_unknown_topic_is_not_recognized() -> None:
    assert is_recognized_topic("FAVORITE_SNACK") is False


def test_apply_hoh_sets_the_field() -> None:
    current = HouseStatus(hoh="Yash")
    updated = apply_state_topic("HOH", "Barrett", current)

    assert updated.hoh == "Barrett"
    assert current.hoh == "Yash"  # original untouched (new object returned)


def test_apply_nominees_splits_comma_separated_names() -> None:
    current = HouseStatus()
    updated = apply_state_topic("NOMINEES", "Angela, Dee", current)

    assert updated.nominees == ("Angela", "Dee")


def test_apply_nominees_splits_and_separated_names() -> None:
    current = HouseStatus()
    updated = apply_state_topic("NOMINEES", "Angela and Dee", current)

    assert updated.nominees == ("Angela", "Dee")


def test_apply_veto_winner_sets_holder_and_resets_used_flag() -> None:
    current = HouseStatus(veto_holder="Sam", veto_used=True)
    updated = apply_state_topic("VETO_WINNER", "Barrett", current)

    assert updated.veto_holder == "Barrett"
    assert updated.veto_used is False  # freshly won, not yet used


def test_apply_have_nots_splits_names() -> None:
    current = HouseStatus()
    updated = apply_state_topic("HAVE_NOTS", "Kamu, Mallory", current)

    assert updated.have_nots == ("Kamu", "Mallory")


def test_unrecognized_topic_leaves_house_status_unchanged() -> None:
    current = HouseStatus(hoh="Yash")
    updated = apply_state_topic("FAVORITE_SNACK", "pretzels", current)

    assert updated == current


def test_topic_matching_is_case_insensitive() -> None:
    current = HouseStatus()
    updated = apply_state_topic("hoh", "Yash", current)

    assert updated.hoh == "Yash"


def test_applying_one_topic_does_not_disturb_other_fields() -> None:
    current = HouseStatus(hoh="Yash", nominees=("Angela", "Dee"), veto_holder="Sam")
    updated = apply_state_topic("HOH", "Barrett", current)

    assert updated.hoh == "Barrett"
    assert updated.nominees == ("Angela", "Dee")
    assert updated.veto_holder == "Sam"


def test_recognized_topics_constant_matches_all_apply_branches() -> None:
    """Every topic listed as recognized must actually change
    something when applied -- prevents the list and the mapping from
    silently drifting apart."""

    base = HouseStatus()
    for topic in RECOGNIZED_TOPICS:
        updated = apply_state_topic(topic, "Test Value", base)
        assert updated != base, f"{topic} did not change HouseStatus"
