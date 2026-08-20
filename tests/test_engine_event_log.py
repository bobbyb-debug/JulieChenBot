"""Tests for ProductionEngine's durable delivered-event log
(EVENT_LOG_KEY / _record_event_log() / recent_events()) -- added for
the admin dashboard's Activity/Diagnostics/Event Trace views. See
production/engine.py.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from database.storage import Storage
from production.engine import ProductionEngine
from production.events import EventSeverity, EventType, ProductionEvent


class _AnnouncerDouble:
    """Always succeeds; records every event it was asked to announce."""

    def __init__(self) -> None:
        self.events: list[ProductionEvent] = []

    async def announce(self, event: ProductionEvent) -> None:
        self.events.append(event)


class _FailingAnnouncerDouble:
    """Fails for one specific event instance, succeeds for everything else."""

    def __init__(self, failing_event: ProductionEvent) -> None:
        self.failing_event = failing_event

    async def announce(self, event: ProductionEvent) -> None:
        if event is self.failing_event:
            raise RuntimeError("announcement failed")


def _make_engine(storage: Storage) -> ProductionEngine:
    return ProductionEngine(storage=storage)


def test_recent_events_empty_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    engine = _make_engine(Storage())

    assert engine.recent_events() == []


def test_announcing_an_event_records_it_in_the_log(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", True)

    engine = _make_engine(Storage())
    engine.announcer = _AnnouncerDouble()

    event = ProductionEvent(
        source="HouseImage",
        event_type=EventType.IMAGE_CHANGED,
        title="HOUSE STATUS IMAGE UPDATED",
        detail="The latest House Status is in.",
        severity=EventSeverity.NOTICE,
    )
    engine.pending_events = deque([event])

    asyncio.run(engine.announce())

    logged = engine.recent_events()
    assert len(logged) == 1
    assert logged[0]["event_type"] == "image_changed"
    assert logged[0]["source"] == "HouseImage"
    assert logged[0]["severity"] == "notice"


def test_a_failed_announcement_is_not_recorded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", True)

    engine = _make_engine(Storage())
    failing_event = ProductionEvent(
        source="Test", event_type=EventType.SYSTEM, title="t", detail="d"
    )
    engine.announcer = _FailingAnnouncerDouble(failing_event)
    engine.pending_events = deque([failing_event])

    asyncio.run(engine.announce())

    assert engine.recent_events() == []


def test_recent_events_returns_newest_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", True)

    engine = _make_engine(Storage())
    engine.announcer = _AnnouncerDouble()

    first = ProductionEvent(
        source="A", event_type=EventType.SYSTEM, title="first", detail="d"
    )
    second = ProductionEvent(
        source="B", event_type=EventType.SYSTEM, title="second", detail="d"
    )
    engine.pending_events = deque([first, second])

    asyncio.run(engine.announce())

    logged = engine.recent_events()
    assert [entry["title"] for entry in logged] == ["second", "first"]


def test_recent_events_respects_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", True)

    engine = _make_engine(Storage())
    engine.announcer = _AnnouncerDouble()

    for i in range(5):
        engine.pending_events = deque(
            [
                ProductionEvent(
                    source="A",
                    event_type=EventType.SYSTEM,
                    title=f"event-{i}",
                    detail="d",
                )
            ]
        )
        asyncio.run(engine.announce())

    logged = engine.recent_events(limit=2)
    assert [entry["title"] for entry in logged] == ["event-4", "event-3"]


def test_event_log_is_capped_at_event_log_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", True)
    monkeypatch.setattr(ProductionEngine, "EVENT_LOG_LIMIT", 3)

    engine = _make_engine(Storage())
    engine.announcer = _AnnouncerDouble()

    for i in range(5):
        engine.pending_events = deque(
            [
                ProductionEvent(
                    source="A",
                    event_type=EventType.SYSTEM,
                    title=f"event-{i}",
                    detail="d",
                )
            ]
        )
        asyncio.run(engine.announce())

    logged = engine.recent_events(limit=10)
    assert len(logged) == 3
    assert [entry["title"] for entry in logged] == ["event-4", "event-3", "event-2"]


def test_event_log_persists_across_engine_restarts(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", True)

    storage = Storage()
    engine = _make_engine(storage)
    engine.announcer = _AnnouncerDouble()
    engine.pending_events = deque(
        [
            ProductionEvent(
                source="A", event_type=EventType.SYSTEM, title="persisted", detail="d"
            )
        ]
    )
    asyncio.run(engine.announce())

    restarted = _make_engine(Storage())

    assert [entry["title"] for entry in restarted.recent_events()] == ["persisted"]


# ==========================================================
# ENABLE_ADMIN_API gating (feature-flag regression)
# ==========================================================
#
# _record_event_log() must never run -- and storage.json's event_log
# key must never even be created -- unless config.ENABLE_ADMIN_API is
# true. This is what keeps Julie's production behavior (no extra
# storage write, no extra failure surface in announce()'s per-event
# try/except) byte-for-byte unchanged whenever the admin API feature
# is off, which is the default.


def test_event_log_key_is_never_written_when_admin_api_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", False)

    engine = _make_engine(Storage())
    engine.announcer = _AnnouncerDouble()

    event = ProductionEvent(
        source="HouseImage",
        event_type=EventType.IMAGE_CHANGED,
        title="HOUSE STATUS IMAGE UPDATED",
        detail="The latest House Status is in.",
        severity=EventSeverity.NOTICE,
    )
    engine.pending_events = deque([event])

    asyncio.run(engine.announce())

    # Two independent checks: the public read API reports nothing...
    assert engine.recent_events() == []
    # ...and, more strongly, the underlying storage key was never
    # created at all -- not merely created-and-empty. A sentinel
    # default proves get() fell through to it rather than finding an
    # (empty) list actually written by _record_event_log().
    sentinel = object()
    assert engine.storage.get(ProductionEngine.EVENT_LOG_KEY, sentinel) is sentinel


def test_event_log_stays_empty_across_many_disabled_announcements(
    tmp_path: Path, monkeypatch
) -> None:
    """Not just one event -- a full run of real traffic with the
    feature off must never populate the log, matching pre-PR behavior
    exactly."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", False)

    engine = _make_engine(Storage())
    engine.announcer = _AnnouncerDouble()

    for i in range(5):
        engine.pending_events = deque(
            [
                ProductionEvent(
                    source="A",
                    event_type=EventType.SYSTEM,
                    title=f"event-{i}",
                    detail="d",
                )
            ]
        )
        asyncio.run(engine.announce())

    assert engine.recent_events() == []
    sentinel = object()
    assert engine.storage.get(ProductionEngine.EVENT_LOG_KEY, sentinel) is sentinel


def test_event_log_gating_toggles_cleanly_within_one_process(
    tmp_path: Path, monkeypatch
) -> None:
    """The same engine instance: disabled announcements write nothing,
    then enabling the flag makes the very next announcement recorded --
    proving the gate is checked live, not cached at construction."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", False)

    engine = _make_engine(Storage())
    engine.announcer = _AnnouncerDouble()

    engine.pending_events = deque(
        [ProductionEvent(source="A", event_type=EventType.SYSTEM, title="off", detail="d")]
    )
    asyncio.run(engine.announce())
    assert engine.recent_events() == []

    monkeypatch.setattr("production.engine.ENABLE_ADMIN_API", True)

    engine.pending_events = deque(
        [ProductionEvent(source="A", event_type=EventType.SYSTEM, title="on", detail="d")]
    )
    asyncio.run(engine.announce())

    logged = engine.recent_events()
    assert [entry["title"] for entry in logged] == ["on"]
