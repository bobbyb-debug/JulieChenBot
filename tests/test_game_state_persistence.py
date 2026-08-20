"""Tests for HouseStatus/CompetitionState durability across a process
restart -- the automated, RSS-parser-driven live-feed observation
layer (production/house_status.py, production/competition.py), NOT
the official-facts store.

Before this change, ProductionParser, HouseStatusMonitor, and
CompetitionMonitor all started every process instance with blank in-memory
state (HouseStatus()/CompetitionState()) and had no persistence of their
own. A process restart -- e.g. redeploying a fix on Railway -- silently
forgot HOH, nominees, veto, have-nots, feeds, and competition state, even
though it had already been correctly detected and announced by the
previous instance.

IMPORTANT (post official-facts-architecture fix): /hoh, /nominees,
/noms, and /veto no longer read this object at all -- they read
KnowledgeStore official facts exclusively (see commands/hoh.py,
nominees.py, veto.py, and tests/test_official_state_commands.py /
tests/test_teach_update_command.py for that persistence path). This
file's restart-survival tests are still meaningful because /chat and
@mentions still include HouseStatus as a clearly-labeled, unverified
"LIVE FEED OBSERVATION" section of AI context (see services/
ai_service.py format_game_state(), still exercised below via
format_game_state() directly) -- losing it on restart would just make
that live-feed color/context go blank, not affect any official-facts
command. /recap does not use format_game_state() at all (see
commands/recap.py's own docstring).

ProductionEngine now restores that state from Storage on construction
(_load_game_state()) and persists it (_persist_game_state()) whenever
watcher.run() actually promotes a new value into .current -- see
production/engine.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from database.storage import Storage
from production.competition import CompetitionMonitor, CompetitionState, CompetitionType
from production.engine import ProductionEngine
from production.house_status import HouseStatus, HouseStatusMonitor
from production.rss import FeedUpdate
from services.ai_service import format_game_state


# ==========================================================
# Test doubles (local to this file, matching the pattern already used
# in tests/test_production_engine_integration.py and
# tests/test_event_durability.py -- no real network/Discord access)
# ==========================================================


class _RSSDouble:
    """Returns a canned batch of FeedUpdates exactly once, then nothing."""

    def __init__(self, updates: list[FeedUpdate]) -> None:
        self._updates = list(updates)

    def check_all(self, limit: int | None = None) -> list[FeedUpdate]:
        updates, self._updates = self._updates, []
        return updates

    def current(self):
        return None


class _AnnouncerDouble:
    """Records announced events without touching Discord. Always succeeds,
    so a tick's announce() pass never leaves anything in pending_events."""

    def __init__(self) -> None:
        self.events = []

    async def announce(self, event) -> None:
        self.events.append(event)


class _RealStateWatcherDouble:
    """A ProductionWatcher stand-in exposing the exact public surface
    ProductionEngine.tick() touches (.house_status, .competition, .run()),
    backed by REAL HouseStatusMonitor/CompetitionMonitor instances so
    their actual promotion logic runs -- but registering no other
    monitor, so HouseImageMonitor/HamsterwatchMonitor/etc. (which would
    otherwise perform real network I/O) never run. This mirrors
    test_production_engine_integration.py's WatcherDouble, except the
    two monitors that matter for this feature are the genuine article
    rather than simplified doubles.
    """

    def __init__(self, storage: Storage) -> None:
        self.house_status = HouseStatusMonitor(storage=storage)
        self.competition = CompetitionMonitor()

    async def run(self):
        results = []
        events = []
        for monitor in (self.house_status, self.competition):
            result = await monitor.run()
            results.append(result)
            events.extend(result.events)
        return results, events


def _make_engine(storage: Storage, updates: list[FeedUpdate]) -> ProductionEngine:
    """Real ProductionEngine with RSS/announcer/watcher swapped for the
    lightweight doubles above -- no real network or Discord access."""

    engine = ProductionEngine(storage=storage)
    engine.rss = _RSSDouble(updates)
    engine.announcer = _AnnouncerDouble()
    engine.watcher = _RealStateWatcherDouble(storage)
    return engine


def _hoh_and_nominees_update() -> FeedUpdate:
    return FeedUpdate(
        guid="rss-hoh-nominees-1",
        title="Yash won HOH. Nominees are Alex and Jordan.",
        description="",
        link="https://example.test/hoh-nominees",
        published="2026-08-14T18:00:00Z",
    )


# ==========================================================
# 1. Blank/default startup with no persisted game state
# ==========================================================


def test_blank_startup_with_no_persisted_game_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    engine = ProductionEngine(storage=Storage())

    assert engine.watcher.house_status.current == HouseStatus()
    assert engine.watcher.competition.current == CompetitionState()


# ==========================================================
# 2. Serialization round-trips exactly
# ==========================================================


def test_house_status_to_dict_from_dict_round_trip() -> None:
    original = HouseStatus(
        hoh="Yash",
        nominees=("Alex", "Jordan"),
        veto_holder="Kamu",
        veto_used=True,
        have_nots=("Chuk", "Lyric", "Jason", "Rome"),
        feeds="up",
    )

    restored = HouseStatus.from_dict(original.to_dict())

    assert restored == original


def test_competition_state_to_dict_from_dict_round_trip() -> None:
    original = CompetitionState(
        competition=CompetitionType.HOH,
        active=False,
        winner="Yash",
        started_at=datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 8, 14, 13, 30, 0, tzinfo=UTC),
    )

    restored = CompetitionState.from_dict(original.to_dict())

    assert restored == original


def test_competition_state_round_trip_with_no_timestamps() -> None:
    """started_at/ended_at are Optional -- the common case (no
    competition ever fully tracked with timestamps) must round-trip too."""

    original = CompetitionState(competition=CompetitionType.NONE)

    restored = CompetitionState.from_dict(original.to_dict())

    assert restored == original


# ==========================================================
# 3. A fresh engine/watcher constructed against the same Storage
#    picks up directly-persisted state
# ==========================================================


def test_fresh_engine_restores_directly_persisted_game_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    storage = Storage()
    storage.set(
        ProductionEngine.GAME_STATE_KEY,
        {
            "house_status": HouseStatus(hoh="Yash", nominees=("Alex", "Jordan")).to_dict(),
            "competition": CompetitionState(
                competition=CompetitionType.HOH, winner="Yash"
            ).to_dict(),
        },
    )

    engine = ProductionEngine(storage=Storage())

    assert engine.watcher.house_status.current.hoh == "Yash"
    assert engine.watcher.house_status.current.nominees == ("Alex", "Jordan")
    assert engine.watcher.competition.current.winner == "Yash"


# ==========================================================
# 4-9. Each individual field survives restart (via the full,
# realistic tick() -> promote -> persist -> restart -> restore path)
# ==========================================================


def _tick_and_restart(tmp_path: Path, monkeypatch, title: str) -> ProductionEngine:
    """Runs one real tick() against a single crafted RSS item (promoting
    state through the genuine HouseStatusMonitor/CompetitionMonitor), then
    constructs a brand-new ProductionEngine against the same Storage --
    simulating a Railway redeploy -- and returns it."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    update = FeedUpdate(
        guid="rss-1",
        title=title,
        description="",
        link="https://example.test/rss-1",
        published="2026-08-14T18:00:00Z",
    )
    engine_a = _make_engine(storage, [update])
    asyncio.run(engine_a.tick())

    return ProductionEngine(storage=Storage())


def test_hoh_survives_restart(tmp_path: Path, monkeypatch) -> None:
    restarted = _tick_and_restart(tmp_path, monkeypatch, "Yash won HOH")
    assert restarted.watcher.house_status.current.hoh == "Yash"


def test_nominees_survive_restart(tmp_path: Path, monkeypatch) -> None:
    restarted = _tick_and_restart(
        tmp_path, monkeypatch, "Nominees are Alex and Jordan."
    )
    assert restarted.watcher.house_status.current.nominees == ("Alex", "Jordan")


def test_veto_holder_survives_restart(tmp_path: Path, monkeypatch) -> None:
    restarted = _tick_and_restart(tmp_path, monkeypatch, "Taylor won the Power of Veto")
    assert restarted.watcher.house_status.current.veto_holder == "Taylor"


def test_have_nots_field_still_round_trips_through_persistence(
    tmp_path: Path, monkeypatch
) -> None:
    """have_nots is no longer settable from RSS/live-feed text at all
    (the Joker's Updates house-status image is now the sole
    authoritative source -- see production/house_image.py and
    production/parser.py), so this can no longer use the
    tick()-driven _tick_and_restart() helper the other fields use.
    The persistence *mechanism* itself (HouseStatus.to_dict()/
    from_dict(), _persist_game_state()/_load_game_state()) must still
    round-trip an existing have_nots value correctly regardless of how
    it was set -- proving the game-state persistence work itself was
    not undone by removing its RSS-text trigger.
    """

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    engine_a = ProductionEngine(storage=storage)
    engine_a.watcher.house_status.current = HouseStatus(
        have_nots=("Chuk", "Lyric", "Jason", "Rome")
    )
    engine_a._persist_game_state()

    restarted = ProductionEngine(storage=Storage())

    assert restarted.watcher.house_status.current.have_nots == (
        "Chuk",
        "Lyric",
        "Jason",
        "Rome",
    )


def test_feeds_status_survives_restart(tmp_path: Path, monkeypatch) -> None:
    restarted = _tick_and_restart(tmp_path, monkeypatch, "Live feeds are down.")
    assert restarted.watcher.house_status.current.feeds == "down"


def test_competition_winner_survives_restart(tmp_path: Path, monkeypatch) -> None:
    restarted = _tick_and_restart(tmp_path, monkeypatch, "Yash won HOH")
    assert restarted.watcher.competition.current.winner == "Yash"
    assert restarted.watcher.competition.current.competition == CompetitionType.HOH


# ==========================================================
# 10. Malformed persisted state does not crash startup
# ==========================================================


def test_malformed_game_state_does_not_crash_startup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    storage = Storage()
    storage.set(
        ProductionEngine.GAME_STATE_KEY,
        {"house_status": "not-a-dict", "competition": 12345},
    )

    engine = ProductionEngine(storage=Storage())  # must not raise

    assert engine.watcher.house_status.current == HouseStatus()
    assert engine.watcher.competition.current == CompetitionState()


def test_completely_wrong_shaped_game_state_does_not_crash_startup(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    storage = Storage()
    storage.set(ProductionEngine.GAME_STATE_KEY, ["not", "even", "a", "dict"])

    engine = ProductionEngine(storage=Storage())  # must not raise

    assert engine.watcher.house_status.current == HouseStatus()
    assert engine.watcher.competition.current == CompetitionState()


# ==========================================================
# 11. Existing storage keys remain intact
# ==========================================================


def test_persisting_game_state_preserves_existing_storage_keys(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    storage = Storage()
    storage.last_guid = "some-existing-guid"

    engine = ProductionEngine(storage=storage)
    engine.watcher.house_status.current = HouseStatus(hoh="Yash")
    engine.watcher.competition.current = CompetitionState(
        competition=CompetitionType.HOH, winner="Yash"
    )
    engine._persist_game_state()

    for key in Storage.DEFAULT_DATA:
        assert key in storage._data

    assert storage.last_guid == "some-existing-guid"
    assert storage.get(ProductionEngine.GAME_STATE_KEY) is not None


# ==========================================================
# 12. /chat's format_game_state() receives restored state after restart
# ==========================================================


def test_format_game_state_reflects_restored_state_after_restart(
    tmp_path: Path, monkeypatch
) -> None:
    restarted = _tick_and_restart(
        tmp_path, monkeypatch, "Yash won HOH. Nominees are Alex and Jordan."
    )

    game_state = format_game_state(
        restarted.watcher.house_status.current,
        restarted.watcher.competition.current,
    )

    assert "Yash" in game_state
    assert "Alex" in game_state
    assert "Jordan" in game_state
    assert "Head of Household: Yash" in game_state
    assert "Nominees: Alex, Jordan" in game_state


# ==========================================================
# 13. Existing production event behavior remains unchanged
# ==========================================================


def test_first_observation_stays_silent_but_second_change_still_announces(
    tmp_path: Path, monkeypatch
) -> None:
    """Restoring/persisting game state must not interfere with the
    existing event-emission behavior: a monitor's very first captured
    state is silent by design (HouseStatusMonitor/CompetitionMonitor
    both return changed=False with no events for "first observation" --
    see production/house_status.py, production/competition.py), and a
    genuine subsequent change still produces the expected event."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    # First tick: first observation. Only the RSS_UPDATE event itself
    # (always emitted, regardless of parser recognition) should announce
    # -- no HOH_CHANGED/COMPETITION_WINNER, since nothing changed from
    # Julie's perspective yet.
    engine = _make_engine(storage, [_hoh_and_nominees_update()])
    asyncio.run(engine.tick())

    first_tick_types = {event.event_type.value for event in engine.announcer.events}
    assert first_tick_types == {"rss_update"}
    events_after_first_tick = len(engine.announcer.events)

    # Second tick: a genuine change (new HOH) must still announce as
    # before -- this feature must not suppress real event emission.
    engine.rss = _RSSDouble(
        [
            FeedUpdate(
                guid="rss-2",
                title="Taylor won HOH",
                description="",
                link="https://example.test/rss-2",
                published="2026-08-14T19:00:00Z",
            )
        ]
    )
    asyncio.run(engine.tick())

    second_tick_events = engine.announcer.events[events_after_first_tick:]
    second_tick_types = {event.event_type.value for event in second_tick_events}
    assert "hoh_changed" in second_tick_types
    assert "competition_winner" in second_tick_types


# ==========================================================
# 14. A3 pending_events durability still works independently
# ==========================================================


def test_game_state_and_pending_events_persist_and_restore_independently(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    engine = _make_engine(storage, [_hoh_and_nominees_update()])
    asyncio.run(engine.tick())

    # The double announcer always succeeds, so nothing should remain
    # durably pending -- A3's own behavior, unaffected by this feature.
    assert list(engine.pending_events) == []
    assert storage.get(ProductionEngine.PENDING_EVENTS_KEY) == []

    restarted = ProductionEngine(storage=Storage())

    assert list(restarted.pending_events) == []
    assert restarted.watcher.house_status.current.hoh == "Yash"


# ==========================================================
# CRITICAL REGRESSION TEST -- the exact production incident
# ==========================================================


def test_restart_survives_the_exact_production_incident(
    tmp_path: Path, monkeypatch
) -> None:
    """Process instance A receives an RSS update establishing HoH and
    nominations. The process is then destroyed and recreated (as though
    Railway redeployed the container). Process instance B must
    immediately report the same HoH and nominations through the exact
    same path /chat uses -- without generating any new Discord
    announcement merely because state was restored.
    """

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    # --- Process instance A ---
    engine_a = _make_engine(storage, [_hoh_and_nominees_update()])
    asyncio.run(engine_a.tick())

    assert engine_a.watcher.house_status.current.hoh == "Yash"
    assert engine_a.watcher.house_status.current.nominees == ("Alex", "Jordan")

    persisted = storage.get(ProductionEngine.GAME_STATE_KEY)
    assert persisted is not None
    assert persisted["house_status"]["hoh"] == "Yash"
    assert persisted["house_status"]["nominees"] == ["Alex", "Jordan"]

    # --- Simulated Railway restart: fresh Storage + fresh engine ---
    engine_b = ProductionEngine(storage=Storage())

    # Instance B must immediately report the same state -- no RSS tick,
    # no announce() call, nothing but construction.
    assert engine_b.watcher.house_status.current.hoh == "Yash"
    assert engine_b.watcher.house_status.current.nominees == ("Alex", "Jordan")

    # The exact live-feed-observation path /chat uses (services/discord.py
    # generate_ai_reply()) -- NOT the official-facts path /hoh/nominees/
    # veto use (KnowledgeStore, unaffected by this restart entirely).
    game_state = format_game_state(
        engine_b.watcher.house_status.current,
        engine_b.watcher.competition.current,
    )
    assert "Yash" in game_state
    assert "Alex" in game_state
    assert "Jordan" in game_state

    # Restoration must not replay old events: instance B has no pending
    # events and never called announce(), so nothing new was queued or
    # sent to Discord merely because it restarted.
    assert list(engine_b.pending_events) == []
