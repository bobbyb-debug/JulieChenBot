"""Tests for production/historical_retrieval.py -- deterministic
retrieval over the structured historical event store. Covers cycle
identity resolution, the double/triple-eviction ambiguity policy,
player-anchored lookup, and season-ambiguity handling.
"""

from __future__ import annotations

import pytest

from database.historical_events import HistoricalEventStore
from production.historical_retrieval import (
    extract_cycle_number,
    extract_ordinal,
    extract_week_number,
    find_known_players_mentioned,
    retrieve_hoh,
)


@pytest.fixture
def store(tmp_path):
    return HistoricalEventStore(db_path=tmp_path / "historical_events.db")


def _verified(store, *, season=28, cycle, week, winner):
    claim = store.record_hoh_claim(
        season=season, cycle_sequence_number=cycle, week_number=week,
        winner=winner, source_type="manual_admin_note", source_ref="x",
    )
    store.verify_hoh(claim.id)


# ==========================================================
# Parsing helpers
# ==========================================================


def test_extract_week_number():
    assert extract_week_number("Who was HOH in Week 4?") == 4
    assert extract_week_number("nothing here") is None


def test_extract_cycle_number():
    assert extract_cycle_number("Who was HOH in Cycle 6?") == 6
    assert extract_cycle_number("no cycle mentioned") is None


def test_extract_ordinal():
    assert extract_ordinal("the second HOH during Week 9") == 2
    assert extract_ordinal("the 3rd cycle") == 3
    assert extract_ordinal("who was HOH") is None


# ==========================================================
# Cycle-anchored retrieval
# ==========================================================


def test_explicit_cycle_returns_that_cycles_hoh(store):
    _verified(store, cycle=6, week=6, winner="Melody")

    result = retrieve_hoh("Who was the HOH in Cycle 6?", store)

    assert len(result.events) == 1
    assert result.events[0].participants[0].houseguest == "MELODY"


def test_unmatched_cycle_returns_nothing_not_a_guess(store):
    _verified(store, cycle=6, week=6, winner="Melody")

    result = retrieve_hoh("Who was the HOH in Cycle 99?", store)

    assert result.events == []
    assert bool(result) is False


# ==========================================================
# Week-anchored retrieval -- the double/triple-eviction ambiguity policy
# ==========================================================


def test_normal_week_resolves_to_exactly_one_cycle(store):
    _verified(store, cycle=4, week=4, winner="PlayerA")

    result = retrieve_hoh("Who was HOH in Week 4?", store)

    assert len(result.events) == 1
    assert result.multiple_cycles is False


def test_double_eviction_week_never_arbitrarily_picks_one_cycle(store):
    _verified(store, cycle=9, week=9, winner="PlayerB")
    _verified(store, cycle=10, week=9, winner="PlayerC")

    result = retrieve_hoh("Who was HOH in Week 9?", store)

    assert result.multiple_cycles is True
    assert {e.cycle_sequence_number for e in result.events} == {9, 10}
    winners = {p.houseguest for e in result.events for p in e.participants}
    assert winners == {"PLAYERB", "PLAYERC"}


def test_triple_eviction_week_returns_all_three(store):
    _verified(store, cycle=12, week=12, winner="D")
    _verified(store, cycle=13, week=12, winner="E")
    _verified(store, cycle=14, week=12, winner="F")

    result = retrieve_hoh("Who was HOH in Week 12?", store)

    assert len(result.events) == 3
    assert result.multiple_cycles is True


def test_ordinal_resolves_the_double_eviction_to_a_single_cycle(store):
    _verified(store, cycle=9, week=9, winner="PlayerB")
    _verified(store, cycle=10, week=9, winner="PlayerC")

    result = retrieve_hoh("Who was the second HOH during Week 9?", store)

    assert len(result.events) == 1
    assert result.events[0].participants[0].houseguest == "PLAYERC"


def test_ordinal_first_resolves_to_the_first_cycle(store):
    _verified(store, cycle=9, week=9, winner="PlayerB")
    _verified(store, cycle=10, week=9, winner="PlayerC")

    result = retrieve_hoh("Who was the first HOH during Week 9?", store)

    assert result.events[0].participants[0].houseguest == "PLAYERB"


def test_unmatched_week_returns_nothing(store):
    result = retrieve_hoh("Who was HOH in Week 40?", store)
    assert result.events == []
    assert bool(result) is False


# ==========================================================
# Player-anchored retrieval -- exact match only, never fuzzy
# ==========================================================


def test_known_player_name_resolves_to_their_verified_cycles(store):
    _verified(store, cycle=6, week=6, winner="Taylor")

    result = retrieve_hoh("What happened during Taylor's HOH?", store)

    assert len(result.events) == 1
    assert result.events[0].participants[0].houseguest == "TAYLOR"


def test_unknown_player_name_is_not_fuzzily_matched(store):
    """Julie has no recorded HOH data for this name at all -- it must
    not be guessed or partially matched against a similar known name."""

    _verified(store, cycle=6, week=6, winner="Taylor")

    result = retrieve_hoh("What happened during Tayler's HOH?", store)  # typo'd

    assert result.events == []


def test_a_player_who_won_hoh_multiple_times_returns_every_verified_cycle(store):
    _verified(store, cycle=3, week=3, winner="Yash")
    _verified(store, cycle=11, week=11, winner="Yash")

    result = retrieve_hoh("Was Yash ever HOH?", store)

    assert {e.cycle_sequence_number for e in result.events} == {3, 11}


# ==========================================================
# A question naming more than one known player (a comparison) must
# retrieve every named player's events, not silently just one --
# see find_known_players_mentioned() and retrieve_hoh()'s own
# docstring for the "Compare Taylor's HOH with Dee's" scenario this
# covers.
# ==========================================================


def test_find_known_players_mentioned_matches_every_known_name_present(store):
    _verified(store, cycle=2, week=2, winner="Taylor")
    _verified(store, cycle=5, week=5, winner="Dee")

    matches = find_known_players_mentioned("Compare Taylor's HOH with Dee's", store)

    assert set(matches) == {"TAYLOR", "DEE"}


def test_find_known_players_mentioned_ignores_unmentioned_known_players(store):
    _verified(store, cycle=2, week=2, winner="Taylor")
    _verified(store, cycle=5, week=5, winner="Dee")

    matches = find_known_players_mentioned("What happened during Taylor's HOH?", store)

    assert matches == ["TAYLOR"]


def test_comparison_question_retrieves_both_named_players_events(store):
    _verified(store, cycle=2, week=2, winner="Taylor")
    _verified(store, cycle=5, week=5, winner="Dee")

    result = retrieve_hoh("Compare Taylor's HOH with Dee's", store)

    winners = {
        participant.houseguest
        for event in result.events
        for participant in event.participants
        if participant.role == "WINNER"
    }
    assert winners == {"TAYLOR", "DEE"}


def test_comparison_question_includes_multiple_cycles_for_a_repeat_winner(store):
    _verified(store, cycle=2, week=2, winner="Taylor")
    _verified(store, cycle=9, week=9, winner="Taylor")
    _verified(store, cycle=5, week=5, winner="Dee")

    result = retrieve_hoh("Compare Taylor's HOH with Dee's", store)

    assert {e.cycle_sequence_number for e in result.events} == {2, 9, 5}
    assert result.multiple_cycles is True


def test_a_single_named_player_still_returns_only_that_players_events(store):
    """Backward compatibility: naming just one known player must
    behave exactly as before this change."""

    _verified(store, cycle=2, week=2, winner="Taylor")
    _verified(store, cycle=5, week=5, winner="Dee")

    result = retrieve_hoh("What happened during Taylor's HOH?", store)

    assert len(result.events) == 1
    assert result.events[0].participants[0].houseguest == "TAYLOR"


# ==========================================================
# No match at all -- must never hallucinate
# ==========================================================


def test_no_week_cycle_or_known_player_returns_empty(store):
    _verified(store, cycle=6, week=6, winner="Melody")

    result = retrieve_hoh("who is the current HOH?", store)

    assert result.events == []
    assert bool(result) is False


def test_empty_store_returns_empty_for_any_query_shape(store):
    assert retrieve_hoh("Who was HOH in Week 4?", store).events == []
    assert retrieve_hoh("Who was HOH in Cycle 4?", store).events == []
    assert retrieve_hoh("What happened during Taylor's HOH?", store).events == []


# ==========================================================
# Season ambiguity -- Phase 1 has no CURRENT_SEASON; retrieval must
# not guess across multiple seasons' data
# ==========================================================


def test_single_season_present_is_unambiguous(store):
    _verified(store, season=28, cycle=6, week=6, winner="Melody")

    result = retrieve_hoh("Who was HOH in Week 6?", store)

    assert len(result.events) == 1
    assert result.season_ambiguous is False


def test_multiple_seasons_present_does_not_guess_which_one(store):
    _verified(store, season=27, cycle=6, week=6, winner="OldWinner")
    _verified(store, season=28, cycle=6, week=6, winner="Melody")

    result = retrieve_hoh("Who was HOH in Week 6?", store)

    assert result.events == []
    assert result.season_ambiguous is True
