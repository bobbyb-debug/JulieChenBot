"""Tests for database/historical_events.py -- the structured,
administrator-verified historical event store (Phase 1: HOH by game
cycle). Covers cycle identity, verification, corrections, and the
read paths Julie's prompt is built from.
"""

from __future__ import annotations

import pytest

from database.historical_events import (
    AlreadyVerifiedError,
    DuplicateCycleError,
    EventNotFoundError,
    HistoricalEventStore,
)


@pytest.fixture
def store(tmp_path):
    return HistoricalEventStore(db_path=tmp_path / "historical_events.db")


def _record_and_verify(store, *, season=28, cycle=6, week=6, winner="Melody", ref="x"):
    claim = store.record_hoh_claim(
        season=season, cycle_sequence_number=cycle, week_number=week,
        winner=winner, source_type="manual_admin_note", source_ref=ref,
    )
    return store.verify_hoh(claim.id, author_id=1)


# ==========================================================
# Game cycle identity -- (season, cycle_sequence_number), not week
# ==========================================================


def test_cycle_identity_is_season_and_sequence_number_not_week(store):
    _record_and_verify(store, cycle=9, week=9, winner="Drew")
    _record_and_verify(store, cycle=10, week=9, winner="LaTrice")

    week9 = store.verified_hoh_for_week(season=28, week_number=9)
    assert len(week9) == 2
    assert {e.cycle_sequence_number for e in week9} == {9, 10}
    assert {e.cycle_week_number for e in week9} == {9}


def test_triple_eviction_three_cycles_same_week(store):
    _record_and_verify(store, cycle=12, week=12, winner="D")
    _record_and_verify(store, cycle=13, week=12, winner="E")
    _record_and_verify(store, cycle=14, week=12, winner="F")

    week12 = store.verified_hoh_for_week(season=28, week_number=12)
    assert len(week12) == 3
    assert [e.cycle_sequence_number for e in week12] == [12, 13, 14]


def test_duplicate_cycle_identity_is_rejected_safely(store):
    store.create_cycle(season=28, cycle_sequence_number=6)

    with pytest.raises(DuplicateCycleError):
        store.create_cycle(season=28, cycle_sequence_number=6)


def test_backfilling_an_earlier_cycle_does_not_change_existing_identities(store):
    """Insertion order must never determine chronological identity --
    the whole reason cycle_sequence_number is administrator-assigned
    rather than auto-incremented."""

    _record_and_verify(store, cycle=8, week=8, winner="X")
    _record_and_verify(store, cycle=2, week=2, winner="Y")

    cycle_8 = store.get_cycle(season=28, cycle_sequence_number=8)
    cycle_2 = store.get_cycle(season=28, cycle_sequence_number=2)

    assert cycle_8.cycle_sequence_number == 8
    assert cycle_2.cycle_sequence_number == 2


def test_week_number_is_plain_metadata_not_part_of_identity(store):
    """Two cycles CAN legitimately share a week_number -- it is
    explicitly not part of the uniqueness constraint."""

    store.create_cycle(season=28, cycle_sequence_number=9, week_number=9)
    # Must not raise -- only (season, cycle_sequence_number) is unique.
    store.create_cycle(season=28, cycle_sequence_number=10, week_number=9)


def test_week_number_can_be_corrected_as_a_plain_edit(store):
    """Correcting a wrong week LABEL is not a fact correction -- it
    must not require the supersede/verification machinery reserved
    for the HOH winner itself."""

    store.create_cycle(season=28, cycle_sequence_number=6, week_number=5)
    updated = store.set_week_number(season=28, cycle_sequence_number=6, week_number=6)
    assert updated.week_number == 6


def test_week_number_may_be_unknown(store):
    cycle = store.create_cycle(season=28, cycle_sequence_number=6, week_number=None)
    assert cycle.week_number is None


# ==========================================================
# Verification -- UNVERIFIED -> ADMIN_VERIFIED, human-triggered only
# ==========================================================


def test_new_claim_starts_unverified(store):
    claim = store.record_hoh_claim(
        season=28, cycle_sequence_number=6, week_number=6, winner="Melody",
        source_type="manual_admin_note", source_ref="x",
    )
    assert claim.verification_status == "UNVERIFIED"


def test_explicit_verify_promotes_to_admin_verified(store):
    event = _record_and_verify(store)
    assert event.verification_status == "ADMIN_VERIFIED"


def test_a_second_unverified_claim_never_automatically_supersedes_a_verified_one(store):
    """The core Decision-3 guard: inserting a competing claim must
    never itself change what's currently verified."""

    verified = _record_and_verify(store, winner="Melody")
    store.record_hoh_claim(
        season=28, cycle_sequence_number=6, week_number=6, winner="SomeoneElse",
        source_type="fan_wiki", source_ref="y",
    )

    current = store.verified_hoh_for_cycle(season=28, cycle_sequence_number=6)
    assert current.id == verified.id
    assert [p.houseguest for p in current.participants] == ["MELODY"]


def test_verifying_a_second_claim_for_an_already_verified_slot_raises(store):
    """A second candidate can coexist as UNVERIFIED, but promoting it
    while another is already verified must fail loudly, not silently
    supersede -- see correct_hoh() for the deliberate, explicit path
    that IS allowed to replace a verified record."""

    _record_and_verify(store, winner="Melody")
    second = store.record_hoh_claim(
        season=28, cycle_sequence_number=6, week_number=6, winner="Other",
        source_type="fan_wiki", source_ref="y",
    )

    with pytest.raises(AlreadyVerifiedError):
        store.verify_hoh(second.id)


def test_reject_marks_a_losing_claim_without_deleting_it(store):
    _record_and_verify(store, winner="Melody")
    second = store.record_hoh_claim(
        season=28, cycle_sequence_number=6, week_number=6, winner="Other",
        source_type="fan_wiki", source_ref="y",
    )

    rejected = store.reject_hoh(second.id)

    assert rejected.verification_status == "REJECTED"
    assert rejected.active is True  # never deleted


# ==========================================================
# Corrections -- old record preserved, superseded, never mutated
# ==========================================================


def test_correction_creates_a_new_row_and_supersedes_the_old(store):
    original = _record_and_verify(store, winner="Melody")

    corrected = store.correct_hoh(
        old_event_id=original.id, winner="ActualWinner",
        source_type="manual_admin_note", source_ref="better source",
    )

    assert corrected.id != original.id
    assert corrected.supersedes == original.id
    assert corrected.verification_status == "ADMIN_VERIFIED"


def test_correction_preserves_the_old_record_as_traceable(store):
    original = _record_and_verify(store, winner="Melody")
    store.correct_hoh(
        old_event_id=original.id, winner="ActualWinner",
        source_type="manual_admin_note", source_ref="better source",
    )

    candidates = store.find_hoh_candidates(season=28, cycle_sequence_number=6)
    old = next(c for c in candidates if c.id == original.id)

    assert old.verification_status == "CORRECTED"
    assert old.active is True  # never deleted
    assert [p.houseguest for p in old.participants] == ["MELODY"]


def test_only_the_corrected_record_is_returned_as_currently_verified(store):
    original = _record_and_verify(store, winner="Melody")
    store.correct_hoh(
        old_event_id=original.id, winner="ActualWinner",
        source_type="manual_admin_note", source_ref="better source",
    )

    current = store.verified_hoh_for_cycle(season=28, cycle_sequence_number=6)
    assert [p.houseguest for p in current.participants] == ["ACTUALWINNER"]


def test_correcting_a_non_verified_record_is_rejected(store):
    unverified = store.record_hoh_claim(
        season=28, cycle_sequence_number=6, week_number=6, winner="Melody",
        source_type="manual_admin_note", source_ref="x",
    )
    with pytest.raises(ValueError):
        store.correct_hoh(
            old_event_id=unverified.id, winner="Someone",
            source_type="manual_admin_note", source_ref="y",
        )


def test_correcting_an_unknown_event_raises(store):
    with pytest.raises(EventNotFoundError):
        store.correct_hoh(
            old_event_id=999, winner="X",
            source_type="manual_admin_note", source_ref="y",
        )


# ==========================================================
# Provenance survives storage/retrieval
# ==========================================================


def test_provenance_is_stored_and_retrievable(store):
    claim = store.record_hoh_claim(
        season=28, cycle_sequence_number=6, week_number=6, winner="Melody",
        source_type="hamsterwatch", source_ref="http://hamsterwatch.com/bb28/w6",
        excerpt="Melody wins the Week 6 HOH.", bb_day=41, author_id=7,
    )

    assert claim.source_type == "hamsterwatch"
    assert claim.source_ref == "http://hamsterwatch.com/bb28/w6"
    assert claim.excerpt == "Melody wins the Week 6 HOH."
    assert claim.bb_day == 41
    assert claim.author_id == 7


def test_read_paths_never_return_unverified_records(store):
    store.record_hoh_claim(
        season=28, cycle_sequence_number=6, week_number=6, winner="Melody",
        source_type="manual_admin_note", source_ref="x",
    )

    assert store.verified_hoh_for_cycle(season=28, cycle_sequence_number=6) is None
    assert store.verified_hoh_for_week(season=28, week_number=6) == []
    assert store.verified_hoh_for_player("Melody") == []


def test_read_paths_never_return_rejected_or_corrected_records(store):
    original = _record_and_verify(store, winner="Melody")
    store.correct_hoh(
        old_event_id=original.id, winner="ActualWinner",
        source_type="manual_admin_note", source_ref="y",
    )

    assert store.verified_hoh_for_player("Melody") == []
    current = store.verified_hoh_for_player("ActualWinner")
    assert len(current) == 1


def test_admin_review_path_sees_every_status_unlike_julie_facing_reads(store):
    _record_and_verify(store, winner="Melody")
    store.record_hoh_claim(
        season=28, cycle_sequence_number=6, week_number=6, winner="Rejected",
        source_type="fan_wiki", source_ref="y",
    )

    candidates = store.find_hoh_candidates(season=28, cycle_sequence_number=6)
    assert len(candidates) == 2
    assert {c.verification_status for c in candidates} == {"ADMIN_VERIFIED", "UNVERIFIED"}
