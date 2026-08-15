"""Tests for the removal of text-based Have-Not detection/announcement.

Product decision: the Joker's Updates house-status image (see
production/house_image.py) is now the sole authoritative source for
Have-Nots. Julie must no longer infer the Have-Not list from RSS/
live-feed text, emit HAVE_NOTS_CHANGED events, or post a "Have-Not
List Updated" card to Discord.

production/parser.py no longer has any Have-Not text pattern at all
(see tests/test_parser.py for parser-level coverage: RSS text can no
longer populate HouseStatus.have_nots under any phrasing). This file
covers the downstream consequence directly: even if
HouseStatus.have_nots were ever to differ between two states (however
that might happen), HouseStatusMonitor must never turn that into an
event -- the announcement code path itself was removed from
production/house_status.py, not merely left unreachable because
nothing sets the field anymore.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from database.storage import Storage
from production.engine import ProductionEngine
from production.house_status import HouseStatus, HouseStatusMonitor
from production.rss import FeedUpdate


class FakeStorage:
    def __init__(self) -> None:
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value


class _RSSDouble:
    def __init__(self, updates: list[FeedUpdate]) -> None:
        self._updates = list(updates)

    def check_all(self, limit: int | None = None) -> list[FeedUpdate]:
        updates, self._updates = self._updates, []
        return updates

    def current(self):
        return None


class _AnnouncerDouble:
    def __init__(self) -> None:
        self.events = []

    async def announce(self, event) -> None:
        self.events.append(event)


class _RealStateWatcherDouble:
    """A ProductionWatcher stand-in backed by REAL HouseStatusMonitor
    (so its actual promotion/event logic runs) but registering no
    other monitor -- avoids HouseImageMonitor/HamsterwatchMonitor/etc.
    performing real network I/O. Mirrors the identical pattern in
    tests/test_game_state_persistence.py."""

    def __init__(self, storage: Storage) -> None:
        from production.competition import CompetitionMonitor

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


# ==========================================================
# Monitor-level: the HAVE_NOTS_CHANGED event-creation code path
# itself is gone, not just unreachable
# ==========================================================


def test_house_status_monitor_never_emits_have_nots_changed_even_if_field_differs() -> None:
    """Directly exercises the removed code path: even when have_nots
    genuinely differs between the current and newly-supplied state,
    no HAVE_NOTS_CHANGED event -- and no event mentioning Have-Nots at
    all -- is ever produced. Other fields' change-detection (hoh,
    nominees, veto_holder, feeds) must remain completely unaffected.
    """

    monitor = HouseStatusMonitor(storage=FakeStorage())

    # First observation is always silent regardless of this change --
    # establish a non-blank baseline first.
    monitor.update(HouseStatus(hoh="Yash", have_nots=("Chuk", "Lyric")))
    asyncio.run(monitor.check())
    assert monitor.current.have_nots == ("Chuk", "Lyric")

    # Now genuinely change have_nots, and only have_nots.
    monitor.update(HouseStatus(hoh="Yash", have_nots=("Jason", "Rome")))
    result = asyncio.run(monitor.check())

    assert result.changed is True  # HouseStatus as a whole did change...
    event_types = {event.event_type.value for event in result.events}
    assert "have_nots_changed" not in event_types  # ...but no event for it
    assert not any("have-not" in event.title.lower() for event in result.events)
    assert result.events == []  # nothing else changed either
    # .current is still updated -- this monitor still tracks the field,
    # it just never announces changes to it.
    assert monitor.current.have_nots == ("Jason", "Rome")


def test_house_status_monitor_still_announces_unrelated_fields_alongside_have_nots_change() -> None:
    """A real HOH change occurring in the same update as a have_nots
    change must still announce HOH_CHANGED -- removing Have-Not
    announcements must not suppress unrelated event emission."""

    monitor = HouseStatusMonitor(storage=FakeStorage())

    monitor.update(HouseStatus(hoh="Yash", have_nots=("Chuk", "Lyric")))
    asyncio.run(monitor.check())

    monitor.update(HouseStatus(hoh="Taylor", have_nots=("Jason", "Rome")))
    result = asyncio.run(monitor.check())

    event_types = {event.event_type.value for event in result.events}
    assert event_types == {"hoh_changed"}


# ==========================================================
# Engine-level: RSS text mentioning Have-Nots produces no
# Have-Not-related event through the real, unmodified pipeline
# ==========================================================


def test_rss_text_mentioning_have_nots_produces_no_have_not_event(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    engine = ProductionEngine(storage=storage)
    engine.rss = _RSSDouble(
        [
            FeedUpdate(
                guid="rss-1",
                title="The Have-Nots are Chuk, Lyric, Jason and Rome",
                description="",
                link="https://example.test/rss-1",
                published="2026-08-14T18:00:00Z",
            )
        ]
    )
    engine.announcer = _AnnouncerDouble()
    engine.watcher = _RealStateWatcherDouble(storage)

    asyncio.run(engine.tick())

    assert engine.watcher.house_status.current.have_nots == ()
    event_types = {event.event_type.value for event in engine.announcer.events}
    assert "have_nots_changed" not in event_types
    # Only the RSS_UPDATE live-feed post itself should have gone out --
    # the text was never recognized as a state change at all.
    assert event_types == {"rss_update"}
    assert not any(
        "have-not" in event.title.lower() for event in engine.announcer.events
    )
