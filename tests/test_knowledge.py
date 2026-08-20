"""Tests for production/knowledge.py -- administrator-taught durable
knowledge (facts, rules, corrections) backed by the existing Storage
abstraction.

Mirrors the exact persistence conventions already established by A3
(ProductionEngine._persist_pending_events()/_load_pending_events()) and
the game-state persistence work (_persist_game_state()/_load_game_state()):
one Storage key, JSON-safe dicts, loaded once at construction, written
immediately on mutation, malformed records skipped rather than crashing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from database.storage import Storage
from production.knowledge import KnowledgeItem, KnowledgeStore, KnowledgeType


# ==========================================================
# KnowledgeItem serialization
# ==========================================================


def test_knowledge_item_to_dict_from_dict_round_trip() -> None:
    original = KnowledgeItem(
        id=1,
        type=KnowledgeType.FACT,
        content="Yash is the current Head of Household.",
        author_id=123456789,
        created_at=datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC),
        active=True,
    )

    restored = KnowledgeItem.from_dict(original.to_dict())

    assert restored == original


def test_knowledge_item_from_dict_rejects_unknown_type() -> None:
    data = KnowledgeItem(
        id=1,
        type=KnowledgeType.FACT,
        content="x",
        author_id=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ).to_dict()
    data["type"] = "not_a_real_type"

    try:
        KnowledgeItem.from_dict(data)
        assert False, "expected ValueError for an unknown KnowledgeType"
    except ValueError:
        pass


# ==========================================================
# KnowledgeStore: teach / list / forget
# ==========================================================


def test_authorized_admin_can_teach_a_fact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.FACT, "Yash is the current HoH.", author_id=111)

    assert item.id == 1
    assert item.type == KnowledgeType.FACT
    assert item.content == "Yash is the current HoH."
    assert item.author_id == 111
    assert item.active is True
    assert item in store.active_items()


def test_authorized_admin_can_teach_a_rule(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(
        KnowledgeType.RULE,
        "The house-status image is authoritative for Have-Nots.",
        author_id=111,
    )

    assert item.type == KnowledgeType.RULE
    assert item in store.active_items()


def test_authorized_admin_can_teach_a_correction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(
        KnowledgeType.CORRECTION,
        "Yash is HoH, not Barrett.",
        author_id=111,
    )

    assert item.type == KnowledgeType.CORRECTION
    assert item in store.active_items()


def test_ids_increment_and_survive_soft_deletion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    first = store.teach(KnowledgeType.FACT, "one", author_id=1)
    second = store.teach(KnowledgeType.FACT, "two", author_id=1)
    assert (first.id, second.id) == (1, 2)

    store.forget(second.id)
    third = store.teach(KnowledgeType.FACT, "three", author_id=1)

    # Forgetting #2 must not free up its ID for reuse.
    assert third.id == 3


def test_list_shows_only_active_knowledge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    kept = store.teach(KnowledgeType.FACT, "kept", author_id=1)
    removed = store.teach(KnowledgeType.FACT, "removed", author_id=1)
    store.forget(removed.id)

    active = store.active_items()
    assert kept in active
    assert removed not in active
    assert len(active) == 1


def test_forget_deactivates_rather_than_deletes(tmp_path: Path, monkeypatch) -> None:
    """Soft deletion: the record must still exist (auditable), just
    with active=False -- not erased from storage entirely."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.FACT, "will be forgotten", author_id=1)
    removed = store.forget(item.id)

    assert removed is True
    assert store.get(item.id) is not None
    assert store.get(item.id).active is False
    assert any(i.id == item.id for i in store.all_items())
    assert not any(i.id == item.id for i in store.active_items())


def test_forget_is_idempotent_and_safe_for_unknown_ids(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.FACT, "x", author_id=1)

    assert store.forget(item.id) is True
    assert store.forget(item.id) is False  # already forgotten
    assert store.forget(99999) is False  # never existed


# ==========================================================
# Reactivation: reverses forget() IN PLACE, never a duplicate
# ==========================================================


def test_reactivate_restores_a_forgotten_item(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.FACT, "Yash is strong.", author_id=1)
    store.forget(item.id)

    restored = store.reactivate(item.id)

    assert restored is True
    assert store.get(item.id).active is True


def test_reactivate_preserves_id_content_author_and_created_at(
    tmp_path: Path, monkeypatch
) -> None:
    """The whole point: reactivation must never create a new item or
    lose provenance -- same id, content, author, created_at."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.FACT, "Yash is strong.", author_id=42)
    original_id = item.id
    original_content = item.content
    original_author = item.author_id
    original_created_at = item.created_at

    store.forget(original_id)
    store.reactivate(original_id)

    restored = store.get(original_id)
    assert restored.id == original_id
    assert restored.content == original_content
    assert restored.author_id == original_author
    assert restored.created_at == original_created_at
    assert len(store.all_items()) == 1  # no duplicate was created


def test_reactivate_advances_updated_at(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.FACT, "x", author_id=1)
    store.forget(item.id)
    forgotten_updated_at = store.get(item.id).updated_at

    store.reactivate(item.id)

    assert store.get(item.id).updated_at >= forgotten_updated_at


def test_reactivate_is_idempotent_and_safe_for_unknown_ids(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.FACT, "x", author_id=1)

    assert store.reactivate(item.id) is False  # already active
    assert store.reactivate(99999) is False  # never existed

    store.forget(item.id)
    assert store.reactivate(item.id) is True
    assert store.reactivate(item.id) is False  # already reactivated


def test_reactivated_item_reappears_in_active_items(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.FACT, "x", author_id=1)
    store.forget(item.id)
    assert not any(i.id == item.id for i in store.active_items())

    store.reactivate(item.id)
    assert any(i.id == item.id for i in store.active_items())


def test_reactivate_persists_immediately(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    store = KnowledgeStore(storage=storage)

    item = store.teach(KnowledgeType.FACT, "x", author_id=1)
    store.forget(item.id)
    store.reactivate(item.id)

    reloaded = KnowledgeStore(storage=storage)
    assert reloaded.get(item.id).active is True


def test_reactivating_a_state_item_supersedes_the_active_one_for_topic(
    tmp_path: Path, monkeypatch
) -> None:
    """Reactivating an old STATE item must never produce two active
    STATE items for the same topic -- that would break active_state()'s
    "there is never more than one" guarantee every command and the AI
    chat context rely on."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    old = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    new = store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")
    assert old.active is False  # auto-superseded by the new write
    assert store.active_state("HOH").id == new.id

    store.reactivate(old.id)

    # Reactivating the old value must supersede the currently-active
    # one, not create a second active STATE item for the same topic.
    assert store.get(old.id).active is True
    assert store.get(new.id).active is False
    assert store.active_state("HOH").id == old.id
    assert store.active_state("HOH").content == "Yash"


def test_reactivating_a_state_item_with_no_current_active_state_is_simple(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    store.forget(item.id)
    assert store.active_state("HOH") is None

    store.reactivate(item.id)

    assert store.active_state("HOH").id == item.id


# ==========================================================
# Persistence: survives restart/reload
# ==========================================================


def test_knowledge_is_persisted_immediately(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    store = KnowledgeStore(storage=storage)

    store.teach(KnowledgeType.FACT, "Yash is HoH.", author_id=1)

    persisted = storage.get(KnowledgeStore.STORAGE_KEY)
    assert persisted is not None
    assert len(persisted) == 1
    assert persisted[0]["content"] == "Yash is HoH."


def test_knowledge_survives_simulated_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    store_a = KnowledgeStore(storage=storage)
    fact = store_a.teach(KnowledgeType.FACT, "Yash is HoH.", author_id=1)
    rule = store_a.teach(KnowledgeType.RULE, "Trust the image.", author_id=1)
    store_a.forget(rule.id)

    # Simulated Railway restart: fresh Storage + fresh KnowledgeStore.
    store_b = KnowledgeStore(storage=Storage())

    active = store_b.active_items()
    assert len(active) == 1
    assert active[0].content == "Yash is HoH."
    assert active[0].id == fact.id

    # The forgotten rule is still on record (soft delete), just inactive.
    all_items = store_b.all_items()
    assert len(all_items) == 2
    assert any(i.id == rule.id and not i.active for i in all_items)


def test_corrupt_records_do_not_crash_startup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    storage.set(
        KnowledgeStore.STORAGE_KEY,
        [
            {"id": 1, "type": "fact", "content": "ok"},  # missing timestamps
            {"id": 2, "type": "not_a_type", "content": "bad type"},
            "not even a dict",
            {
                "id": 3,
                "type": "fact",
                "content": "good",
                "author_id": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                "active": True,
            },
        ],
    )

    store = KnowledgeStore(storage=Storage())  # must not raise

    assert [item.id for item in store.all_items()] == [3]


def test_completely_wrong_shaped_knowledge_does_not_crash_startup(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    storage.set(KnowledgeStore.STORAGE_KEY, {"not": "a list"})

    # storage.get() returns the dict as-is; KnowledgeStore._load()
    # iterates it (dict iteration yields keys, plain strings) --
    # each "record" fails from_dict() and is skipped, not fatal.
    store = KnowledgeStore(storage=Storage())  # must not raise

    assert store.all_items() == []


# ==========================================================
# Explicit supersession (/teach correction ... supersedes:<id>)
# ==========================================================


def test_teach_with_supersedes_deactivates_the_target_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    stale = store.teach(KnowledgeType.FACT, "Barrett is HoH.", author_id=1)
    correction = store.teach(
        KnowledgeType.CORRECTION,
        "Yash is HoH, not Barrett.",
        author_id=1,
        supersedes=stale.id,
    )

    assert correction.supersedes == stale.id
    active = store.active_items()
    assert correction in active
    assert stale not in active
    assert store.get(stale.id).active is False


def test_teach_with_supersedes_writes_a_single_persisted_state(
    tmp_path: Path, monkeypatch
) -> None:
    """The requirement is stronger than 'both changes eventually land':
    there must never be an intermediate persisted state where the new
    correction exists but the superseded item is still active. Since
    teach() performs both mutations in memory and calls _persist()
    exactly once, inspecting storage.json immediately after the call
    returning is sufficient to prove no such intermediate state was
    ever written."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    store = KnowledgeStore(storage=storage)

    stale = store.teach(KnowledgeType.FACT, "Barrett is HoH.", author_id=1)
    store.teach(
        KnowledgeType.CORRECTION,
        "Yash is HoH, not Barrett.",
        author_id=1,
        supersedes=stale.id,
    )

    persisted = storage.get(KnowledgeStore.STORAGE_KEY)
    persisted_stale = next(rec for rec in persisted if rec["id"] == stale.id)
    persisted_correction = next(
        rec for rec in persisted if rec["content"] == "Yash is HoH, not Barrett."
    )

    assert persisted_stale["active"] is False
    assert persisted_correction["active"] is True
    assert persisted_correction["supersedes"] == stale.id


def test_teach_supersedes_survives_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    store_a = KnowledgeStore(storage=storage)
    stale = store_a.teach(KnowledgeType.FACT, "Barrett is HoH.", author_id=1)
    store_a.teach(
        KnowledgeType.CORRECTION,
        "Yash is HoH, not Barrett.",
        author_id=1,
        supersedes=stale.id,
    )

    store_b = KnowledgeStore(storage=Storage())

    active = store_b.active_items()
    assert len(active) == 1
    assert active[0].content == "Yash is HoH, not Barrett."
    assert active[0].supersedes == stale.id
    assert store_b.get(stale.id).active is False


def test_teach_with_invalid_supersedes_id_raises_and_creates_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """Validation must happen before anything is created or persisted
    -- a typo'd ID must never leave an orphaned correction behind."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    store = KnowledgeStore(storage=storage)

    try:
        store.teach(
            KnowledgeType.CORRECTION,
            "Yash is HoH.",
            author_id=1,
            supersedes=999,
        )
        assert False, "expected ValueError for a nonexistent supersedes target"
    except ValueError:
        pass

    assert store.all_items() == []
    assert storage.get(KnowledgeStore.STORAGE_KEY, []) == []


def test_teach_supersedes_an_already_forgotten_item_is_a_harmless_no_op(
    tmp_path: Path, monkeypatch
) -> None:
    """The target existing (even if already inactive) is enough --
    re-deactivating an already-forgotten item must not error."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    stale = store.teach(KnowledgeType.FACT, "Barrett is HoH.", author_id=1)
    store.forget(stale.id)

    correction = store.teach(
        KnowledgeType.CORRECTION,
        "Yash is HoH, not Barrett.",
        author_id=1,
        supersedes=stale.id,
    )

    assert correction.active is True
    assert store.get(stale.id).active is False


def test_supersedes_round_trips_through_serialization() -> None:
    original = KnowledgeItem(
        id=2,
        type=KnowledgeType.CORRECTION,
        content="Yash is HoH, not Barrett.",
        author_id=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        supersedes=1,
    )

    restored = KnowledgeItem.from_dict(original.to_dict())

    assert restored.supersedes == 1
    assert restored == original


def test_supersedes_defaults_to_none_for_records_predating_the_field() -> None:
    """Backward compatibility: knowledge records persisted before
    supersedes existed have no such key at all."""

    data = KnowledgeItem(
        id=1,
        type=KnowledgeType.FACT,
        content="x",
        author_id=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ).to_dict()
    del data["supersedes"]

    restored = KnowledgeItem.from_dict(data)

    assert restored.supersedes is None


# ==========================================================
# STATE: topic-keyed, auto-superseding current game values
# ==========================================================


def test_state_requires_a_topic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    try:
        store.teach(KnowledgeType.STATE, "Yash", author_id=1)
        assert False, "expected ValueError for a STATE item with no topic"
    except ValueError:
        pass

    assert store.all_items() == []


def test_topic_is_rejected_for_non_state_types(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    try:
        store.teach(KnowledgeType.FACT, "Yash won 3 comps.", author_id=1, topic="HOH")
        assert False, "expected ValueError: topic is STATE-only"
    except ValueError:
        pass

    assert store.all_items() == []


def test_state_creation_stores_normalized_topic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    item = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="hoh")

    assert item.type == KnowledgeType.STATE
    assert item.topic == "HOH"  # normalized upper-case
    assert item in store.active_items()
    assert store.active_state("HOH") == item
    assert store.active_state("hoh") == item  # lookup also normalizes


def test_same_topic_state_auto_supersedes_previous_state(tmp_path: Path, monkeypatch) -> None:
    """The core requirement: a new STATE write for a topic that
    already has an active STATE value must automatically supersede
    it -- no competing active fact, no explicit supersedes needed."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    first = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    second = store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    assert second.supersedes == first.id
    assert store.get(first.id).active is False
    assert store.active_state("HOH") == second

    active_states = [i for i in store.active_items() if i.type == KnowledgeType.STATE]
    assert active_states == [second]  # never two competing active STATEs


def test_different_topics_coexist_independently(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    hoh = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    noms = store.teach(KnowledgeType.STATE, "Angela, Dee", author_id=1, topic="NOMINEES")

    # A later HOH update must not touch the unrelated NOMINEES state.
    store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    assert store.active_state("NOMINEES") == noms
    assert store.get(noms.id).active is True
    assert store.active_state("HOH").content == "Barrett"


def test_state_history_is_preserved_not_deleted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    first = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    second = store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    all_ids = [item.id for item in store.all_items()]
    assert first.id in all_ids
    assert second.id in all_ids
    assert store.get(first.id).content == "Yash"  # old value still readable


def test_state_ids_remain_unique_and_not_reused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    a = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    b = store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")
    c = store.teach(KnowledgeType.FACT, "Yash won 3 comps.", author_id=1)

    assert len({a.id, b.id, c.id}) == 3
    assert c.id == max(a.id, b.id) + 1


def test_explicit_supersedes_overrides_automatic_topic_lookup(
    tmp_path: Path, monkeypatch
) -> None:
    """An explicitly-given supersedes always wins over the automatic
    same-topic lookup."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    unrelated = store.teach(KnowledgeType.FACT, "some other fact", author_id=1)
    current_hoh = store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    new_hoh = store.teach(
        KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH", supersedes=unrelated.id
    )

    assert new_hoh.supersedes == unrelated.id
    assert store.get(unrelated.id).active is False
    # The automatic same-topic candidate was NOT touched, since an
    # explicit target was given instead.
    assert store.get(current_hoh.id).active is True


def test_state_does_not_affect_fact_or_rule_behavior(tmp_path: Path, monkeypatch) -> None:
    """Adding STATE must not change FACT/RULE's own accumulating
    behavior -- both remain simultaneously active regardless of topic
    or STATE activity."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    fact1 = store.teach(KnowledgeType.FACT, "Yash has won several comps.", author_id=1)
    fact2 = store.teach(KnowledgeType.FACT, "Angela is strategic.", author_id=1)
    rule1 = store.teach(KnowledgeType.RULE, "Never invent live-feed info.", author_id=1)
    store.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    store.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    active = store.active_items()
    assert fact1 in active
    assert fact2 in active
    assert rule1 in active


def test_active_state_returns_none_when_nothing_taught(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    store = KnowledgeStore(storage=Storage())

    assert store.active_state("HOH") is None


def test_state_topic_round_trips_through_serialization() -> None:
    original = KnowledgeItem(
        id=1,
        type=KnowledgeType.STATE,
        content="Yash",
        author_id=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        topic="HOH",
    )

    restored = KnowledgeItem.from_dict(original.to_dict())

    assert restored.topic == "HOH"
    assert restored == original


def test_topic_defaults_to_none_for_records_predating_the_field() -> None:
    """Backward compatibility: knowledge records persisted before
    STATE/topic existed have no such key at all."""

    data = KnowledgeItem(
        id=1,
        type=KnowledgeType.FACT,
        content="x",
        author_id=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ).to_dict()
    del data["topic"]

    restored = KnowledgeItem.from_dict(data)

    assert restored.topic is None


def test_state_supersession_survives_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    store_a = KnowledgeStore(storage=storage)
    store_a.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    second = store_a.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    store_b = KnowledgeStore(storage=Storage())

    assert store_b.active_state("HOH").content == "Barrett"
    assert store_b.active_state("HOH").id == second.id
