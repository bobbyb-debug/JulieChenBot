"""Tests for ProductionWatcher's monitor-construction fault isolation (A1).

_register_builtin_monitors() previously had no isolation at all: if
any monitor's constructor raised, the exception propagated straight
out of ProductionWatcher.__init__ -- aborting the whole watcher, and
in the real startup chain (ProductionEngine -> Scheduler ->
DiscordService -> JulieApplication -> bot.py), the whole bot.

HamsterwatchMonitor's construction (it builds a HamsterwatchArchive,
which creates/opens a SQLite database and runs FTS5 schema DDL) is
the one monitor with a realistic, verified construction-time failure
mode -- see database/hamsterwatch_archive.py. These tests protect its
isolation without touching real SQLite/FTS5: they monkeypatch
production.watcher.HamsterwatchMonitor itself to a fake whose
constructor raises, precisely simulating a real archive/database
failure at the exact point ProductionWatcher would encounter one.
"""

from __future__ import annotations

import asyncio

import production.watcher as watcher_module
from database.storage import Storage
from production.engine import ProductionEngine
from production.watcher import ProductionWatcher


class FakeStorage:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class RaisingHamsterwatchMonitor:
    """Stands in for HamsterwatchMonitor, raising on construction to
    simulate a real HamsterwatchArchive/SQLite/FTS5 failure without
    touching real SQLite."""

    def __init__(self, storage=None):
        raise RuntimeError("simulated HamsterwatchArchive/SQLite failure")


class NullWatcher:
    """Stands in for the tick-execution phase in the engine-
    compatibility test, so no real monitor network I/O happens --
    that test is about ProductionEngine construction succeeding
    despite a failed monitor and tick() still completing, not about
    exercising real monitor checks."""

    async def run(self):
        return [], []


class NullRSS:
    def check_all(self, limit=None):
        return []

    def current(self):
        return None


# ==========================================================
# 1. A single constructor failure does not prevent initialization
# ==========================================================


def test_hamsterwatch_constructor_failure_does_not_prevent_watcher_init(monkeypatch):
    monkeypatch.setattr(watcher_module, "HamsterwatchMonitor", RaisingHamsterwatchMonitor)

    watcher = ProductionWatcher(storage=FakeStorage())  # must not raise

    assert len(watcher.failed_monitors) == 1
    failed = watcher.failed_monitors[0]
    assert failed.name == "HamsterwatchMonitor"
    assert "simulated HamsterwatchArchive/SQLite failure" in failed.error


def test_failure_is_logged_at_error_level_with_traceback(monkeypatch):
    monkeypatch.setattr(watcher_module, "HamsterwatchMonitor", RaisingHamsterwatchMonitor)

    calls: list[tuple[tuple, dict]] = []
    original_error = watcher_module.logger.error

    def spy_error(*args, **kwargs):
        calls.append((args, kwargs))
        return original_error(*args, **kwargs)

    monkeypatch.setattr(watcher_module.logger, "error", spy_error)

    ProductionWatcher(storage=FakeStorage())

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert "HamsterwatchMonitor" in args
    assert kwargs.get("exc_info") is True


def test_failed_monitor_is_not_registered(monkeypatch):
    monkeypatch.setattr(watcher_module, "HamsterwatchMonitor", RaisingHamsterwatchMonitor)

    watcher = ProductionWatcher(storage=FakeStorage())

    assert not any(m.name == "HamsterwatchMonitor" for m in watcher.monitors)
    assert not hasattr(watcher, "hamsterwatch")


# ==========================================================
# 2. Later monitors still initialize (the critical regression test --
#    this must fail against the pre-fix implementation)
# ==========================================================


def test_later_monitor_still_initializes_after_earlier_failure(monkeypatch):
    """HamsterwatchMonitor is constructed before XUpdatesMonitor in
    registration order. Proves the old bug (one constructor failure
    aborts the entire sequential construction, so nothing registered
    after it ever gets a chance) is fixed."""

    monkeypatch.setattr(watcher_module, "HamsterwatchMonitor", RaisingHamsterwatchMonitor)

    watcher = ProductionWatcher(storage=FakeStorage())

    assert hasattr(watcher, "x_updates")
    assert watcher.x_updates in watcher.monitors
    assert [m.name for m in watcher.monitors] == [
        "HouseStatusMonitor",
        "HouseImageMonitor",
        "CompetitionMonitor",
        "XUpdatesMonitor",
    ]


# ==========================================================
# 3. Failure is observable through the existing snapshot()
#    representation
# ==========================================================


def test_failed_monitor_appears_in_snapshot(monkeypatch):
    monkeypatch.setattr(watcher_module, "HamsterwatchMonitor", RaisingHamsterwatchMonitor)

    watcher = ProductionWatcher(storage=FakeStorage())
    snapshot = watcher.snapshot()

    assert snapshot["failed_monitors"] == [
        {
            "name": "HamsterwatchMonitor",
            "error": "simulated HamsterwatchArchive/SQLite failure",
        }
    ]
    assert not any(m["name"] == "HamsterwatchMonitor" for m in snapshot["monitors"])
    # total_monitors reflects only what actually registered -- a failed
    # construction was never counted as if it were healthy.
    assert snapshot["total_monitors"] == 4


def test_snapshot_failed_monitors_is_empty_on_normal_startup():
    watcher = ProductionWatcher(storage=FakeStorage())
    assert watcher.snapshot()["failed_monitors"] == []


# ==========================================================
# 4. Normal (all-success) startup is byte-for-byte unchanged
# ==========================================================


def test_normal_startup_is_unchanged_when_everything_succeeds():
    watcher = ProductionWatcher(storage=FakeStorage())

    assert watcher.total_monitors == 5
    assert watcher.failed_monitors == []
    assert [m.name for m in watcher.monitors] == [
        "HouseStatusMonitor",
        "HouseImageMonitor",
        "CompetitionMonitor",
        "HamsterwatchMonitor",
        "XUpdatesMonitor",
    ]
    assert watcher.house_status in watcher.monitors
    assert watcher.house_image in watcher.monitors
    assert watcher.competition in watcher.monitors
    assert watcher.hamsterwatch in watcher.monitors
    assert watcher.x_updates in watcher.monitors


# ==========================================================
# 5. Hamsterwatch construction failure specifically: the watcher
#    remains usable with the other appropriately-registered monitors
# ==========================================================


def test_watcher_remains_usable_after_hamsterwatch_construction_failure(monkeypatch):
    monkeypatch.setattr(watcher_module, "HamsterwatchMonitor", RaisingHamsterwatchMonitor)

    watcher = ProductionWatcher(storage=FakeStorage())

    assert watcher.total_monitors == 4
    assert watcher.enabled_monitors + watcher.disabled_monitors == 4
    assert {m.name for m in watcher.monitors} == {
        "HouseStatusMonitor",
        "HouseImageMonitor",
        "CompetitionMonitor",
        "XUpdatesMonitor",
    }


# ==========================================================
# 6. ProductionEngine compatibility: initializes and can still tick
#    when an independently-failable monitor failed construction
# ==========================================================


def test_engine_initializes_and_ticks_despite_hamsterwatch_construction_failure(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monkeypatch.setattr(watcher_module, "HamsterwatchMonitor", RaisingHamsterwatchMonitor)

    engine = ProductionEngine(storage=Storage())  # must not raise

    assert engine.watcher.failed_monitors[0].name == "HamsterwatchMonitor"
    assert not hasattr(engine.watcher, "hamsterwatch")

    # Prove the engine is genuinely usable afterward: stub the tick-
    # execution phase (no real monitor network I/O -- this test is
    # about construction resilience, not re-testing individual
    # monitors' check() behavior) and confirm a normal tick completes.
    engine.watcher = NullWatcher()
    engine.rss = NullRSS()

    asyncio.run(engine.tick())

    assert engine.tick_count == 1
    assert engine.last_error is None


# ==========================================================
# 7. Quickview/BBUpdates: remain instantiated but unregistered,
#    unaffected by an unrelated monitor's construction failure
# ==========================================================


def test_quickview_and_bb_updates_remain_unregistered(monkeypatch):
    monkeypatch.setattr(watcher_module, "HamsterwatchMonitor", RaisingHamsterwatchMonitor)

    watcher = ProductionWatcher(storage=FakeStorage())

    assert watcher.quickview is not None
    assert watcher.bb_updates is not None
    assert watcher.quickview not in watcher.monitors
    assert watcher.bb_updates not in watcher.monitors


# ==========================================================
# 8. XUpdates: disabled-without-token behavior is unaffected by an
#    unrelated monitor's construction failure
# ==========================================================


def test_x_updates_disabled_behavior_is_unaffected_by_hamsterwatch_failure(monkeypatch):
    monkeypatch.setattr(watcher_module, "HamsterwatchMonitor", RaisingHamsterwatchMonitor)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)

    watcher = ProductionWatcher(storage=FakeStorage())

    assert watcher.x_updates.enabled is False
    assert watcher.x_updates in watcher.monitors
