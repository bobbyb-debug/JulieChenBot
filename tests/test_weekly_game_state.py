"""Tests for the weekly game-state architecture: HISTORY IS NOT
CURRENT STATE.

Root-cause context (see JULIE_HANDOFF.md's replacement audit and this
change's own commit message for the full write-up): production Julie
presented a previous week's HOH/nominees/veto/Have-Nots as current
"OFFICIAL GAME FACTS" because KnowledgeStore STATE topics had no
notion of "this belongs to week N" -- a value, once taught, stayed
"active" (and therefore "current") forever until someone remembered to
re-teach it, and a topic-spelling drift bug (e.g. "VETO WINNER" vs
"VETO_WINNER" -- both free-text, human-typed via /teach update) could
leave two independently-active values for the same real-world fact.

This file exercises the actual fix, at the layer that's actually
authoritative for Julie's answers (production/knowledge.py
KnowledgeStore -- see docs/official-facts-architecture.md: the
automated HouseStatus mirror production/state_sync.py reconciles is
explicitly NOT what /hoh, /nominees, /veto, or the AI chat context
read):

- _canonicalize_topic()/dedupe_topics(): topic-spelling drift can no
  longer silently create two "active" values for one real-world topic.
- current_state()/current_state_items(): a WEEK_SCOPED_TOPICS value
  taught before the current week's boundary reads as UNCONFIRMED, not
  as stale-but-still-current.
- start_new_week()/close_week()/archived_week()/set_archived_week():
  the weekly boundary and historical archive themselves.

See tests/test_official_state_commands.py and
tests/test_ai_service_knowledge_context.py for the command-layer and
prompt-formatting coverage built on top of this.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from database.storage import Storage
from production.knowledge import KnowledgeStore, KnowledgeType


def _store(tmp_path: Path, monkeypatch) -> KnowledgeStore:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    return KnowledgeStore(storage=Storage())


# ==========================================================
# Topic canonicalization -- fixes alias drift at the source (the write
# boundary), rather than patching around it downstream.
# ==========================================================


def test_space_and_underscore_topic_spellings_canonicalize_identically(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)

    item = store.teach(KnowledgeType.STATE, "Devens", author_id=1, topic="BB BLOCKBUSTER")

    assert item.topic == "BB_BLOCKBUSTER"


def test_new_write_under_a_different_spelling_supersedes_the_old_one(
    tmp_path: Path, monkeypatch
) -> None:
    """The actual production bug: a moderator taught "VETO WINNER"
    once and "VETO_WINNER" another time, and the second write never
    superseded the first because they looked like different topics.
    Canonicalizing at write time means they're the same topic from the
    very first write, so teach()'s existing auto-supersede just works.
    """

    store = _store(tmp_path, monkeypatch)

    old = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO WINNER")
    new = store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="VETO_WINNER")

    assert store.get(old.id).active is False
    active_veto_items = [
        i for i in store.active_items()
        if i.type == KnowledgeType.STATE and i.topic == "VETO_WINNER"
    ]
    assert active_veto_items == [new]


# ==========================================================
# dedupe_topics() -- repairs spelling drift already persisted before
# canonicalization existed (idempotent, safe on every startup).
# ==========================================================


def test_dedupe_topics_repairs_pre_existing_conflicting_spellings(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)

    # Simulate legacy pre-fix data: two literal spellings, both
    # written directly (bypassing teach()'s now-canonicalizing write
    # path) so they land as independently-active items, exactly like
    # real production Knowledge did.
    old_id = max((i.id for i in store.all_items()), default=0) + 1
    now = datetime.now(UTC)
    from production.knowledge import KnowledgeItem

    legacy = KnowledgeItem(
        id=old_id, type=KnowledgeType.STATE, content="Devens", author_id=1,
        created_at=now - timedelta(days=7), updated_at=now - timedelta(days=7),
        active=True, topic="BB BLOCKBUSTER",
    )
    fresher = KnowledgeItem(
        id=old_id + 1, type=KnowledgeType.STATE, content="UNCONFIRMED", author_id=1,
        created_at=now, updated_at=now, active=True, topic="BB_BLOCKBUSTER",
    )
    store._items.extend([legacy, fresher])  # noqa: SLF001 -- legacy-data simulation
    store._persist()

    repaired = store.dedupe_topics()

    assert "BB_BLOCKBUSTER" in repaired
    active_bb = [
        i for i in store.active_items()
        if i.type == KnowledgeType.STATE and i.topic == "BB_BLOCKBUSTER"
    ]
    assert len(active_bb) == 1
    assert active_bb[0].content == "UNCONFIRMED"  # most-recently-updated wins
    assert store.get(legacy.id).active is False
    assert store.get(legacy.id).content == "Devens"  # preserved, not deleted


def test_dedupe_topics_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    first = store.dedupe_topics()
    second = store.dedupe_topics()

    assert first == []
    assert second == []


def test_dedupe_topics_never_touches_non_state_knowledge(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    fact = store.teach(KnowledgeType.FACT, "Yash has won 3 comps.", author_id=1)

    store.dedupe_topics()

    assert store.get(fact.id).content == "Yash has won 3 comps."
    assert store.get(fact.id).active is True


# ==========================================================
# current_state() -- the actual fix: a value taught before the
# current week started is UNCONFIRMED, never stale-but-current.
# ==========================================================


def test_current_state_matches_active_state_when_no_week_is_set(
    tmp_path: Path, monkeypatch
) -> None:
    """Backward compatible by construction: a deployment that has
    never called start_new_week() behaves exactly as it always has."""

    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    assert store.current_state("HOH") == store.active_state("HOH")
    assert store.current_state("HOH").content == "Barrett"


def test_value_taught_before_the_current_week_reads_as_unconfirmed(
    tmp_path: Path, monkeypatch
) -> None:
    """TEST 3/4/5 from the acceptance criteria, at the store level:
    Week 8's veto/BB Blockbuster/nominees/Have-Nots must not survive
    into Week 9 as "current" just because nobody re-taught them."""

    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    store.teach(KnowledgeType.STATE, "Devens", author_id=1, topic="BB_BLOCKBUSTER")
    store.teach(
        KnowledgeType.STATE, "Drew, LaLa, Taylor", author_id=1, topic="NOMINEES"
    )
    store.teach(
        KnowledgeType.STATE, "LaLa, Taylor, Mallory", author_id=1, topic="HAVE_NOTS"
    )

    store.start_new_week(9)

    assert store.current_state("VETO_WINNER") is None
    assert store.current_state("BB_BLOCKBUSTER") is None
    assert store.current_state("NOMINEES") is None
    assert store.current_state("HAVE_NOTS") is None

    # The old values are still fully readable as history -- nothing
    # was deleted or deactivated by starting a new week.
    assert store.active_state("VETO_WINNER").content == "Yash"
    assert store.active_state("BB_BLOCKBUSTER").content == "Devens"


def test_value_re_taught_after_the_week_boundary_is_current(
    tmp_path: Path, monkeypatch
) -> None:
    """TEST 1/2: Barrett as HOH and Angela/Dee/Devens as nominees,
    taught AFTER start_new_week(9), must read as current."""

    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    store.start_new_week(9)

    store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")
    store.teach(
        KnowledgeType.STATE, "Angela, Dee, Devens", author_id=1, topic="NOMINEES"
    )

    assert store.current_state("HOH").content == "Barrett"
    assert store.current_state("NOMINEES").content == "Angela, Dee, Devens"


def test_current_state_ignores_week_boundary_for_non_week_scoped_topics(
    tmp_path: Path, monkeypatch
) -> None:
    """REMAINING_HOUSEGUESTS/LAST_EVICTED/VOTE are running/one-time
    facts, not per-week values -- a new week must not blank them out
    just because nobody re-taught them this week."""

    store = _store(tmp_path, monkeypatch)
    store.teach(
        KnowledgeType.STATE,
        "Angela, Barrett, Dee, Drew, Melody, Devens, Taylor, Yash",
        author_id=1,
        topic="REMAINING_HOUSEGUESTS",
    )
    store.teach(KnowledgeType.STATE, "LaLa", author_id=1, topic="LAST_EVICTED")

    store.start_new_week(9)

    assert "Barrett" in store.current_state("REMAINING_HOUSEGUESTS").content
    assert store.current_state("LAST_EVICTED").content == "LaLa"


def test_explicit_unconfirmed_current_value_is_not_masked_by_history(
    tmp_path: Path, monkeypatch
) -> None:
    """TEST 11: an explicit current-week "UNCONFIRMED" write must not
    be second-guessed by an older, more specific historical value."""

    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    store.start_new_week(9)
    store.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="VETO_WINNER")

    current = store.current_state("VETO_WINNER")
    assert current is not None
    assert current.content == "UNCONFIRMED"


def test_current_state_items_excludes_stale_week_scoped_topics_only(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    store.teach(KnowledgeType.STATE, "LaLa", author_id=1, topic="LAST_EVICTED")

    store.start_new_week(9)
    store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    topics = {item.topic: item.content for item in store.current_state_items()}

    assert topics == {"HOH": "Barrett", "LAST_EVICTED": "LaLa"}


# ==========================================================
# Week transitions: start_new_week() / close_week() / archived_week()
# ==========================================================


def test_starting_a_new_week_does_not_delete_or_deactivate_anything(
    tmp_path: Path, monkeypatch
) -> None:
    """TEST 12: starting a new week must never mutate a single
    KnowledgeItem -- the boundary is a separate, time-based read, not
    a rewrite of stored data."""

    store = _store(tmp_path, monkeypatch)
    item = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")

    store.start_new_week(9)

    assert store.get(item.id).active is True
    assert store.get(item.id).content == "Yash"


def test_close_week_requires_a_week_to_have_been_started(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)

    try:
        store.close_week()
        assert False, "expected ValueError with no current week set"
    except ValueError:
        pass


def test_close_week_preserves_the_previous_weeks_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """TEST 13: closing a week preserves its historical snapshot."""

    store = _store(tmp_path, monkeypatch)
    store.start_new_week(8)
    store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    store.teach(KnowledgeType.STATE, "Devens", author_id=1, topic="BB_BLOCKBUSTER")

    record = store.close_week()

    assert record["week"] == 8
    assert record["snapshot"]["VETO_WINNER"] == "Yash"
    assert record["snapshot"]["BB_BLOCKBUSTER"] == "Devens"
    assert store.archived_week(8) == record


def test_starting_the_next_week_after_closing_produces_a_clean_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """TEST 14: closing week 8 then starting week 9 must not inherit
    week 8's nominees/veto/Have-Nots/BB Blockbuster."""

    store = _store(tmp_path, monkeypatch)
    store.start_new_week(8)
    store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    store.teach(KnowledgeType.STATE, "Devens", author_id=1, topic="BB_BLOCKBUSTER")
    store.close_week()

    store.start_new_week(9)

    assert store.current_state("VETO_WINNER") is None
    assert store.current_state("BB_BLOCKBUSTER") is None
    # But Week 8's history is still queryable.
    assert store.archived_week(8)["snapshot"]["VETO_WINNER"] == "Yash"


def test_archived_week_returns_none_for_a_week_never_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    assert store.archived_week(3) is None


def test_set_archived_week_backfills_a_week_that_predates_tracking(
    tmp_path: Path, monkeypatch
) -> None:
    """TEST 7/8: Week 8's veto/BB Blockbuster must be answerable even
    though week-tracking only started at Week 9 -- an admin backfills
    the historical snapshot directly (the dashboard's equivalent
    action), never by hand-editing JSON."""

    store = _store(tmp_path, monkeypatch)
    store.start_new_week(9)

    record = store.set_archived_week(
        8, {"VETO_WINNER": "Yash", "BB_BLOCKBUSTER": "Devens"}
    )

    assert record["snapshot"]["VETO_WINNER"] == "Yash"
    assert store.archived_week(8)["snapshot"]["BB_BLOCKBUSTER"] == "Devens"


def test_week_meta_survives_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    store_a = KnowledgeStore(storage=storage)
    store_a.start_new_week(8)
    store_a.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    store_a.close_week()
    store_a.start_new_week(9)

    store_b = KnowledgeStore(storage=Storage())

    assert store_b.current_week == 9
    assert store_b.current_state("VETO_WINNER") is None
    assert store_b.archived_week(8)["snapshot"]["VETO_WINNER"] == "Yash"
