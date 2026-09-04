"""Integration tests for ProductionEngine.reconcile_game_state_from_knowledge()
-- the startup-reconciliation half of the game-state sync fix (see
production/state_sync.py and tests/test_state_sync.py for the pure
mapping-function unit tests). This file proves the REAL production
incident is actually closed: a stale, persisted game_state can no
longer survive a process restart once Knowledge State already knows
better, and reconciliation never mutates Knowledge, recap_buffer, or
any other unrelated storage key in the process.

See tests/test_teach_update_command.py for the write-time hook
(/teach update confirm -> reconcile) exercised through the real
Discord command path.
"""

from __future__ import annotations

from pathlib import Path

from database.storage import Storage
from production.competition import CompetitionState, CompetitionType
from production.engine import ProductionEngine
from production.house_status import HouseStatus
from production.knowledge import KnowledgeType


def _teach_the_repair_scenario(storage: Storage) -> None:
    """Seeds Knowledge State with exactly the authoritative values from
    the real production incident this fix addresses."""

    from production.knowledge import KnowledgeStore

    knowledge = KnowledgeStore(storage=storage)
    knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")
    knowledge.teach(
        KnowledgeType.STATE, "Devens, LaLa, Taylor", author_id=1, topic="NOMINEES"
    )
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    knowledge.teach(KnowledgeType.STATE, "YES", author_id=1, topic="VETO_USED")
    knowledge.teach(KnowledgeType.STATE, "UNCONFIRMED", author_id=1, topic="HAVE_NOTS")


def _seed_stale_game_state(storage: Storage) -> None:
    """Persists exactly the stale, incoherent game_state observed in
    the real Railway production incident."""

    storage.set(
        ProductionEngine.GAME_STATE_KEY,
        {
            "house_status": HouseStatus(
                hoh="If Yash",
                nominees=("Angela", "Haley", "Kamu"),
                veto_holder="LaLa",
                veto_used=True,
                have_nots=(),
                feeds="down",
            ).to_dict(),
            "competition": CompetitionState(
                competition=CompetitionType.HOH,
                active=False,
                winner="Drew",
            ).to_dict(),
        },
    )


# ==========================================================
# Startup reconciliation -- the exact production incident.
# ==========================================================


def test_startup_reconciles_the_exact_production_incident(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    _teach_the_repair_scenario(storage)
    _seed_stale_game_state(storage)

    engine = ProductionEngine(storage=Storage())  # simulates a Railway restart

    hs = engine.watcher.house_status.current
    assert hs.hoh == "Drew"
    assert hs.nominees == ("Devens", "LaLa", "Taylor")
    assert hs.veto_holder == "Yash"
    assert hs.veto_used is True
    assert hs.have_nots == ()
    # No Knowledge mapping for feeds -- passed through from whatever
    # was persisted, never invented or blanked.
    assert hs.feeds == "down"


def test_startup_reconciliation_never_touches_competition_state(
    tmp_path: Path, monkeypatch
) -> None:
    """No Knowledge topic maps to any CompetitionState field -- see
    production/state_sync.py's own comment on RECOGNIZED_TOPICS vs.
    the sync tables. Reconciliation must leave it exactly as
    persisted, never inventing or blanking a competition winner."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    _teach_the_repair_scenario(storage)
    _seed_stale_game_state(storage)

    engine = ProductionEngine(storage=Storage())

    assert engine.watcher.competition.current.winner == "Drew"
    assert engine.watcher.competition.current.competition == CompetitionType.HOH


def test_blank_startup_with_knowledge_but_no_persisted_game_state_still_reconciles(
    tmp_path: Path, monkeypatch
) -> None:
    """Knowledge State exists, but nothing was ever persisted under
    game_state at all (e.g. a genuinely fresh deploy, or the exact
    pre-fix incident where reconciliation never ran) -- startup must
    still pick up Knowledge immediately, not wait for the next RSS
    item to happen to restate it."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    _teach_the_repair_scenario(storage)

    engine = ProductionEngine(storage=Storage())

    assert engine.watcher.house_status.current.hoh == "Drew"
    assert engine.watcher.house_status.current.nominees == ("Devens", "LaLa", "Taylor")


def test_startup_with_no_knowledge_and_no_game_state_stays_blank(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    engine = ProductionEngine(storage=Storage())

    assert engine.watcher.house_status.current == HouseStatus()


# ==========================================================
# reconcile_game_state_from_knowledge() return value / persistence.
# ==========================================================


def test_reconcile_returns_true_and_persists_when_something_changed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    engine = ProductionEngine(storage=Storage())
    engine.knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")

    changed = engine.reconcile_game_state_from_knowledge()

    assert changed is True
    persisted = engine.storage.get(ProductionEngine.GAME_STATE_KEY)
    assert persisted["house_status"]["hoh"] == "Drew"


def test_reconcile_returns_false_and_does_not_rewrite_when_nothing_changed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    engine = ProductionEngine(storage=Storage())
    engine.knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")
    engine.reconcile_game_state_from_knowledge()  # first call applies it

    changed_again = engine.reconcile_game_state_from_knowledge()

    assert changed_again is False


def test_persisted_game_state_round_trips_through_real_deserialization(
    tmp_path: Path, monkeypatch
) -> None:
    """Validates the reconciled value the exact same way
    ProductionEngine._load_game_state() itself deserializes it --
    not a hand-rolled assumption about the JSON shape."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    engine = ProductionEngine(storage=Storage())
    engine.knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")
    engine.knowledge.teach(
        KnowledgeType.STATE, "Devens, LaLa, Taylor", author_id=1, topic="NOMINEES"
    )
    engine.reconcile_game_state_from_knowledge()

    persisted = engine.storage.get(ProductionEngine.GAME_STATE_KEY)
    restored_house_status = HouseStatus.from_dict(persisted["house_status"])
    restored_competition = CompetitionState.from_dict(persisted["competition"])

    assert restored_house_status.hoh == "Drew"
    assert restored_house_status.nominees == ("Devens", "LaLa", "Taylor")
    assert restored_competition == CompetitionState()  # untouched default


# ==========================================================
# No feedback loop / no unrelated mutation.
# ==========================================================


def test_reconciliation_never_mutates_knowledge_item_count(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    engine = ProductionEngine(storage=Storage())
    engine.knowledge.teach(KnowledgeType.STATE, "Drew", author_id=1, topic="HOH")
    before_count = len(engine.knowledge.active_items())

    engine.reconcile_game_state_from_knowledge()
    engine.reconcile_game_state_from_knowledge()  # idempotent, called twice

    assert len(engine.knowledge.active_items()) == before_count


def test_reconciliation_preserves_unrelated_storage_keys(
    tmp_path: Path, monkeypatch
) -> None:
    """recap_buffer, seen_guids, feed_state, event_log, statistics --
    every top-level key reconciliation has no business touching --
    must survive byte-for-byte through a full startup reconciliation
    pass."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    _teach_the_repair_scenario(storage)
    _seed_stale_game_state(storage)

    storage.set(
        "recap_buffer",
        [{"created_at": "2026-08-14T18:00:00+00:00", "detail": "A real feed update."}],
    )
    storage.set("seen_guids", ["guid-1", "guid-2"])
    storage.last_guid = "guid-2"
    storage.feed_state = "up"
    storage.set("event_log", [{"kind": "test"}])
    storage.set("statistics", {"rss_updates": 42, "announcements": 3})

    ProductionEngine(storage=Storage())  # triggers startup reconciliation

    reloaded = Storage()
    assert reloaded.get("recap_buffer") == [
        {"created_at": "2026-08-14T18:00:00+00:00", "detail": "A real feed update."}
    ]
    assert reloaded.get("seen_guids") == ["guid-1", "guid-2"]
    assert reloaded.last_guid == "guid-2"
    assert reloaded.feed_state == "up"
    assert reloaded.get("event_log") == [{"kind": "test"}]
    assert reloaded.get("statistics") == {"rss_updates": 42, "announcements": 3}


def test_reconciliation_generates_no_monitor_events(tmp_path: Path, monkeypatch) -> None:
    """Reconciling on startup must behave like _load_game_state()
    already does -- a direct assignment into .current, never routed
    through HouseStatusMonitor.update()/check(), so restoring/
    correcting state on boot never fires a spurious Discord
    announcement merely because the process started."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    _teach_the_repair_scenario(storage)
    _seed_stale_game_state(storage)

    engine = ProductionEngine(storage=Storage())

    assert engine.watcher.house_status.pending_status is None
    assert list(engine.pending_events) == []
