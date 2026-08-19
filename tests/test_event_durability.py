"""Tests for durable event-delivery persistence (A3).

Before this change, ProductionEngine.pending_events was a plain
in-memory collections.deque. ProductionEvent.to_dict()/from_dict()
existed but were never called from production code. A process crash
or redeploy between an event being queued and it finishing delivery
-- or between one Discord destination succeeding and another still
pending -- silently dropped the event (or its delivered_to progress)
entirely, since nothing about it ever reached disk.

ProductionEngine now persists its pending-event queue to the existing
Storage abstraction (database/storage.py) at two points each tick:
once in process_events() (before any Discord attempt) and once in
announce() (after Discord has been attempted, capturing whatever
per-destination event.delivered_to progress
DiscordOutputRouter.publish() made -- see services/discord_output.py).
On startup, ProductionEngine._load_pending_events() reads that same
key back and re-seeds pending_events, so recovered events go through
the exact same process_events()/announce() path as any other event --
there is no separate recovery pipeline.

These tests use a real Storage backed by a tmp_path file (so
"process restart" is modeled honestly: a second Storage/ProductionEngine
instance reading the same file, not a mock) and a real
DiscordOutputRouter wired to fake in-memory Discord channels -- no
real network or Discord API calls anywhere in this file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from database.storage import Storage
from production.announcer import ProductionAnnouncer
from production.engine import ProductionEngine
from production.events import EventSeverity, EventType, ProductionEvent
from services.discord_output import DiscordOutputRouter


# ==========================================================
# Fakes (self-contained: no real Discord/network involved)
# ==========================================================


class FakeChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.messages: list[dict] = []

    async def send(self, **kwargs) -> None:
        self.messages.append(kwargs)


class FlakyChannel(FakeChannel):
    """Fails a fixed number of sends before succeeding -- simulates one
    destination having a transient Discord-side failure."""

    def __init__(self, channel_id: int, name: str, fail_times: int = 0) -> None:
        super().__init__(channel_id, name)
        self.fail_times = fail_times
        self.send_calls = 0

    async def send(self, **kwargs) -> None:
        self.send_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated Discord send failure")
        await super().send(**kwargs)


class FakeBot:
    def __init__(self, channels: list[FakeChannel]) -> None:
        self.channels = channels

    def get_channel(self, channel_id: int):
        return next((c for c in self.channels if c.id == channel_id), None)

    async def fetch_channel(self, channel_id: int):
        return self.get_channel(channel_id)

    def get_all_channels(self):
        return iter(self.channels)


def make_event(
    title: str = "Durability test",
    event_type: EventType = EventType.IMAGE_CHANGED,
    severity: EventSeverity = EventSeverity.IMPORTANT,
) -> ProductionEvent:
    """IMAGE_CHANGED is the only event type that routes to two
    destinations (house-status, live-updates) via DiscordOutputRouter
    -- see services/discord_output.py _destinations() -- which is
    what makes partial-delivery scenarios possible. (Structured
    game-state events like HOH_CHANGED route to live-updates only as
    of the house-status routing fix; they no longer exercise
    multi-destination durability.) metadata["url"] is required for
    IMAGE_CHANGED's embed building, but _download() is stubbed for
    this whole file (see _route_to_fake_channels below), so its value
    is never actually fetched."""

    return ProductionEvent(
        source="DurabilityTest",
        event_type=event_type,
        title=title,
        detail="A durability regression event.",
        severity=severity,
        metadata={"url": "http://example.test/house.png"},
    )


def wire_engine(storage: Storage, house: FakeChannel, live: FakeChannel) -> ProductionEngine:
    """Builds a real ProductionEngine with a real DiscordOutputRouter
    pointed at fake channels -- everything except the Discord network
    boundary is the genuine production code path."""

    engine = ProductionEngine(storage=storage)
    engine.announcer = ProductionAnnouncer()
    engine.announcer._discord_output = DiscordOutputRouter(FakeBot([house, live]))
    return engine


async def _stub_download(self, url: str, **kwargs):
    """No-network stand-in for every DiscordOutputRouter instance in
    this file -- these tests exercise durability/delivery-tracking
    semantics, not image handling, so a real network call must never
    be involved. A miss (None) still results in exactly one
    channel.send() per destination attempt via the hotlink fallback,
    which is all these tests depend on."""

    return None


@pytest.fixture(autouse=True)
def _route_to_fake_channels(monkeypatch):
    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)
    monkeypatch.setattr(DiscordOutputRouter, "_download", _stub_download)


# ==========================================================
# 1. Event persistence
# ==========================================================


def test_queued_event_survives_storage_reload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    storage = Storage()
    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    engine = wire_engine(storage, house, live)

    event = make_event()
    engine.pending_events.append(event)
    asyncio.run(engine.process_events())

    reloaded = Storage()
    persisted = reloaded.get(ProductionEngine.PENDING_EVENTS_KEY)

    assert len(persisted) == 1
    assert persisted[0]["title"] == "Durability test"
    assert persisted[0]["delivered_to"] == []


# ==========================================================
# 2. Partial delivery
# ==========================================================


def test_partial_delivery_marks_one_destination_delivered_and_one_pending(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    storage = Storage()
    house = FakeChannel(1, "house-status")
    live = FlakyChannel(2, "live-updates", fail_times=1)
    engine = wire_engine(storage, house, live)

    event = make_event()
    engine.pending_events.append(event)
    asyncio.run(engine.process_events())
    asyncio.run(engine.announce())  # announce() swallows the partial-publish error

    assert event.delivered_to == {"house-status"}
    assert list(engine.pending_events) == [event]
    assert len(house.messages) == 1
    assert len(live.messages) == 0

    persisted = Storage().get(ProductionEngine.PENDING_EVENTS_KEY)
    assert len(persisted) == 1
    assert persisted[0]["delivered_to"] == ["house-status"]


# ==========================================================
# 3 & 4 & 5. Restart recovery: recovered state, no duplicate
# success, failed destination retried
# ==========================================================


def test_restart_recovers_partial_delivery_state_and_completes_without_duplicating(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    # --- "before crash": one destination succeeds, one fails, then the
    # process stops (nothing more is ever done with this engine).
    storage_before = Storage()
    house_before = FakeChannel(1, "house-status")
    live_before = FlakyChannel(2, "live-updates", fail_times=1)
    engine_before = wire_engine(storage_before, house_before, live_before)

    event = make_event()
    engine_before.pending_events.append(event)
    asyncio.run(engine_before.process_events())
    asyncio.run(engine_before.announce())

    assert event.delivered_to == {"house-status"}
    assert len(house_before.messages) == 1

    # --- "restart": fresh Storage instance reading the same file, fresh
    # ProductionEngine, fresh Discord channel objects (a new process
    # would reconnect to Discord and get new channel objects too).
    storage_after = Storage()
    house_after = FakeChannel(1, "house-status")
    live_after = FakeChannel(2, "live-updates")  # no longer flaky: recovers cleanly
    engine_after = wire_engine(storage_after, house_after, live_after)

    # 3. Recovered with the correct delivered-destination state.
    assert len(engine_after.pending_events) == 1
    recovered_event = engine_after.pending_events[0]
    assert recovered_event.delivered_to == {"house-status"}
    assert recovered_event.title == "Durability test"

    # Recovery re-enters the normal pipeline -- no separate API.
    asyncio.run(engine_after.process_events())
    asyncio.run(engine_after.announce())

    # 4. The already-delivered destination is never sent to again.
    assert len(house_after.messages) == 0, "house-status must not be resent after recovery"

    # 5. The previously-failed destination is retried and now succeeds.
    assert len(live_after.messages) == 1

    assert recovered_event.delivered_to == {"house-status", "live-updates"}


# ==========================================================
# 6. Complete delivery removes the event from durable state
# ==========================================================


def test_fully_delivered_event_is_removed_from_persisted_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    storage = Storage()
    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    engine = wire_engine(storage, house, live)

    event = make_event()
    engine.pending_events.append(event)
    asyncio.run(engine.process_events())
    asyncio.run(engine.announce())

    assert event.delivered_to == {"house-status", "live-updates"}
    assert list(engine.pending_events) == []
    assert Storage().get(ProductionEngine.PENDING_EVENTS_KEY) == []


# ==========================================================
# 7. Multiple events remain independent
# ==========================================================


def test_multiple_events_do_not_interfere_with_each_others_delivery_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    storage = Storage()
    house = FakeChannel(1, "house-status")
    live = FlakyChannel(2, "live-updates", fail_times=0)
    engine = wire_engine(storage, house, live)

    complete_event = make_event(title="First (will fully deliver)")
    partial_event = make_event(title="Second (will partially fail)")

    # First event: both destinations healthy at this point.
    engine.pending_events.append(complete_event)
    asyncio.run(engine.process_events())
    asyncio.run(engine.announce())
    assert complete_event.delivered_to == {"house-status", "live-updates"}
    assert list(engine.pending_events) == []

    # Second event: live-updates now fails once.
    live.fail_times = 1
    engine.pending_events.append(partial_event)
    asyncio.run(engine.process_events())
    asyncio.run(engine.announce())

    assert partial_event.delivered_to == {"house-status"}
    assert list(engine.pending_events) == [partial_event]

    # house-status received one message per event (2 total); live-updates
    # only received the first event's message, since it failed for the
    # second -- the two events' outcomes did not bleed into each other.
    assert len(house.messages) == 2
    assert len(live.messages) == 1

    persisted = Storage().get(ProductionEngine.PENDING_EVENTS_KEY)
    assert len(persisted) == 1
    assert persisted[0]["title"] == "Second (will partially fail)"
    assert persisted[0]["delivered_to"] == ["house-status"]


# ==========================================================
# 8. Crash-window semantics: at-least-once, not exactly-once
# ==========================================================


def test_crash_before_persist_can_cause_at_least_once_duplicate(
    tmp_path: Path, monkeypatch
) -> None:
    """Documents the acknowledged, unavoidable crash window: if the
    process stops after Discord accepts a message for one destination
    but before ProductionEngine._persist_pending_events() runs again,
    restart recovery has no way to know that delivery happened -- the
    destination is resent. This is intentionally not "fixed": a
    Discord API call and this process's local disk write can never be
    one atomic transaction, so the system provides at-least-once
    delivery, never exactly-once.
    """

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    storage = Storage()
    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    engine = wire_engine(storage, house, live)

    event = make_event()
    engine.pending_events.append(event)
    # Persists the event as pending, with nothing delivered yet.
    asyncio.run(engine.process_events())

    # Simulate the crash window: Discord already accepted the message
    # for one destination (delivered_to mutated in memory, exactly as
    # DiscordOutputRouter.publish() does -- see
    # services/discord_output.py), but the process stops before
    # announce()'s finally-block persist call runs again.
    event.delivered_to.add("house-status")
    house.messages.append({"embed": "sent just before the simulated crash"})

    # No engine.announce() call here -- this models the process dying
    # mid-announce(), before _persist_pending_events() runs.

    restarted_house = FakeChannel(1, "house-status")
    restarted_live = FakeChannel(2, "live-updates")
    restarted_engine = wire_engine(Storage(), restarted_house, restarted_live)

    recovered_event = restarted_engine.pending_events[0]
    # Disk never learned about the pre-crash success.
    assert recovered_event.delivered_to == set()

    asyncio.run(restarted_engine.process_events())
    asyncio.run(restarted_engine.announce())

    # house-status is resent: a duplicate from Discord's point of view,
    # and the documented, accepted consequence of at-least-once
    # semantics rather than a bug this implementation claims to fix.
    assert len(restarted_house.messages) == 1
    assert len(restarted_live.messages) == 1
