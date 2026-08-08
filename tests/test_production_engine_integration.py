"""
Integration tests for Julie ChenBot's production runtime pipeline.

The tests use small in-memory doubles around the real ProductionEngine
so they verify the engine's integration responsibilities without
requiring Discord or network-backed monitors.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from database.storage import Storage
from production.engine import ProductionEngine
from production.events import EventType, ProductionEvent
from production.monitors import MonitorResult, MonitorStatus
from production.rss import FeedUpdate
from services.scheduler import Scheduler


class SpyEvent(ProductionEvent):
    """ProductionEvent that records calls to mark_announced()."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.mark_announced_calls = 0

    def mark_announced(self) -> None:
        self.mark_announced_calls += 1
        super().mark_announced()


class WatcherDouble:
    """Minimal watcher double with the ProductionWatcher public contract."""

    def __init__(
        self,
        results: list[MonitorResult] | None = None,
        events: list[ProductionEvent] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.events = events or []
        self.error = error
        self.run_calls = 0
        self.total_monitors = len(self.results)

    async def run(self) -> tuple[list[MonitorResult], list[ProductionEvent]]:
        self.run_calls += 1
        if self.error is not None:
            raise self.error
        return self.results, self.events


class RSSDouble:
    """Minimal RSS double that avoids network access in integration tests."""

    def __init__(self, update: FeedUpdate | None = None) -> None:
        self.update = update
        self.check_calls = 0

    def check(self) -> FeedUpdate | None:
        self.check_calls += 1
        update = self.update
        self.update = None
        return update

    def current(self) -> FeedUpdate | None:
        return self.update


class AnnouncerDouble:
    """Minimal announcer double that can fail for a chosen event."""

    def __init__(self, failing_event: ProductionEvent | None = None) -> None:
        self.failing_event = failing_event
        self.events: list[ProductionEvent] = []

    async def announce(self, event: ProductionEvent) -> None:
        self.events.append(event)
        if event is self.failing_event:
            raise RuntimeError("announcement failed")


class EngineDouble:
    """Engine double used to exercise Scheduler's continuous loop."""

    def __init__(self) -> None:
        self.tick_calls = 0
        self.failure: Exception | None = None
        self.on_tick = None

    async def tick(self) -> None:
        self.tick_calls += 1
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure
        if self.on_tick is not None:
            self.on_tick()


def make_event(title: str = "Production event") -> SpyEvent:
    """Creates an event with the repository's concrete event contract."""

    return SpyEvent(
        source="IntegrationTest",
        event_type=EventType.SYSTEM,
        title=title,
        detail="Engine integration test event.",
    )


def make_result(events: list[ProductionEvent] | None = None) -> MonitorResult:
    """Creates a healthy result produced by a monitor."""

    return MonitorResult(
        monitor="IntegrationMonitor",
        status=MonitorStatus.HEALTHY,
        changed=bool(events),
        detail="Integration monitor completed.",
        events=events or [],
    )


def make_engine(
    storage: Storage,
    watcher: WatcherDouble | None = None,
    announcer: AnnouncerDouble | None = None,
    rss_update: FeedUpdate | None = None,
) -> ProductionEngine:
    """Creates a real engine with controlled pipeline collaborators."""

    engine = ProductionEngine(storage=storage)
    engine.watcher = watcher or WatcherDouble()
    engine.announcer = announcer or AnnouncerDouble()
    engine.rss = RSSDouble(rss_update)
    return engine


def test_tick_runs_complete_pipeline_and_clears_last_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A successful tick runs watcher, processing, announcing, and saving."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    event = make_event()
    result = make_result([event])
    watcher = WatcherDouble(results=[result], events=[event])
    announcer = AnnouncerDouble()
    engine = make_engine(Storage(), watcher, announcer)
    engine.last_error = "previous cycle failed"

    observed_pending_events: list[ProductionEvent] = []
    original_process_events = engine.process_events

    async def observe_process_events() -> None:
        observed_pending_events.extend(engine.pending_events)
        await original_process_events()

    engine.process_events = AsyncMock(side_effect=observe_process_events)
    engine.announce = AsyncMock(wraps=engine.announce)
    engine.save_state = AsyncMock(wraps=engine.save_state)

    asyncio.run(engine.tick())

    assert watcher.run_calls == 1
    assert engine.last_results == [result]
    assert observed_pending_events == [event]
    assert announcer.events == [event]
    assert event.mark_announced_calls == 1
    assert event.announced is True
    assert list(engine.pending_events) == []
    assert engine.tick_count == 1
    assert engine.last_error is None
    engine.process_events.assert_awaited_once()
    engine.announce.assert_awaited_once()
    engine.save_state.assert_awaited_once()


def test_tick_applies_new_rss_state_to_monitors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A new RSS item is parsed and supplied to both built-in monitors."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    engine = make_engine(
        Storage(),
        rss_update=FeedUpdate(
            guid="rss-1",
            title="Morgan won HOH",
            description="",
            link="https://example.test/rss-1",
            published="2026-08-08T00:00:00Z",
        ),
    )

    asyncio.run(engine.tick())

    assert engine.watcher.house_status.hoh == "Morgan"
    assert engine.watcher.competition.winner == "Morgan"
    assert engine.watcher.competition.competition.value == "Head of Household"
    assert engine.tick_count == 1


def test_failed_cycle_records_error_without_counting_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Watcher failures are recorded and do not count as completed cycles."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    engine = make_engine(
        Storage(),
        watcher=WatcherDouble(error=RuntimeError("watcher failed")),
    )

    asyncio.run(engine.tick())

    assert engine.error_count == 1
    assert engine.last_error == "watcher failed"
    assert engine.tick_count == 0


def test_announcement_failure_requeues_events_in_original_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An announcement failure leaves the failed event and successors queued."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    first = make_event("first")
    failed = make_event("failed")
    third = make_event("third")
    announcer = AnnouncerDouble(failing_event=failed)
    engine = make_engine(Storage(), announcer=announcer)
    engine.pending_events = deque([first, failed, third])

    asyncio.run(engine.announce())

    assert announcer.events == [first, failed]
    assert first.mark_announced_calls == 1
    assert first.announced is True
    assert failed.mark_announced_calls == 0
    assert list(engine.pending_events) == [failed, third]


def test_scheduler_retries_after_error_and_runs_until_stopped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The scheduler catches tick errors and continues until stop() is called."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr("services.scheduler.CHECK_INTERVAL", 0)

    scheduler = Scheduler()
    engine = EngineDouble()
    engine.failure = RuntimeError("transient tick failure")
    scheduler.engine = engine

    def stop_after_second_success() -> None:
        if engine.tick_calls == 3:
            scheduler.stop()

    engine.on_tick = stop_after_second_success

    asyncio.run(scheduler.start())

    assert engine.tick_calls == 3
    assert scheduler.running is False


def test_shutdown_saves_and_restart_loads_storage_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Shutdown saves storage and the next engine observes persisted state."""

    storage_file = tmp_path / "storage.json"
    monkeypatch.setattr(Storage, "FILE", storage_file)

    storage = Storage()
    storage.last_guid = "persisted-guid"
    storage.save = MagicMock(wraps=storage.save)
    engine = make_engine(storage)
    engine.running = True

    asyncio.run(engine.shutdown())

    assert storage.save.call_count == 1
    assert engine.running is False
    assert storage_file.exists()

    restarted_engine = make_engine(Storage())

    assert restarted_engine.storage.last_guid == "persisted-guid"
