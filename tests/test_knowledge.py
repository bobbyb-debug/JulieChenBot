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
