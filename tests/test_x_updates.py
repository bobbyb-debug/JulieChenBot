"""Tests for the optional @JokersBBUpdates X monitor."""

import asyncio
from pathlib import Path

from database.storage import Storage
from production.events import EventType
from production.x_updates import XUpdatesMonitor


class FakeXMonitor(XUpdatesMonitor):
    def __init__(self, storage: Storage, responses: list[dict]) -> None:
        super().__init__(storage=storage)
        self.enabled = True
        self.responses = iter(responses)

    def _request_json(self, path: str, params: dict[str, str] | None = None) -> dict:
        return next(self.responses)


def test_first_x_snapshot_does_not_publish(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    monitor = FakeXMonitor(
        Storage(),
        [
            {"data": {"id": "123", "username": "JokersBBUpdates"}},
            {"data": [{"id": "456", "text": "First post", "created_at": "2026-08-09T06:00:00Z"}]},
        ],
    )

    result = asyncio.run(monitor.check())

    assert result.changed is False
    assert result.events == []


def test_new_x_post_becomes_production_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    storage.set(XUpdatesMonitor.USER_ID_KEY, "123")
    storage.set(XUpdatesMonitor.LAST_POST_ID_KEY, "456")

    monitor = FakeXMonitor(
        storage,
        [
            {"data": [
                {"id": "789", "text": "New Joker's Updates post", "created_at": "2026-08-09T06:10:00Z"},
                {"id": "456", "text": "Old post", "created_at": "2026-08-09T06:00:00Z"},
            ]},
        ],
    )

    result = asyncio.run(monitor.check())

    assert result.changed is True
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_type is EventType.RSS_UPDATE
    assert event.source == "JokersBBUpdates"
    assert event.detail == "New Joker's Updates post"
    assert event.metadata["post_id"] == "789"
    assert event.metadata["link"].endswith("/status/789")


def test_x_monitor_does_not_replay_posts_after_watermark_falls_out_of_window(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    storage.set(XUpdatesMonitor.USER_ID_KEY, "123")
    storage.set(XUpdatesMonitor.LAST_POST_ID_KEY, "old")

    monitor = FakeXMonitor(
        storage,
        [{"data": [
            {"id": "900", "text": "Recent post", "created_at": "2026-08-09T06:20:00Z"},
            {"id": "899", "text": "Older recent post", "created_at": "2026-08-09T06:19:00Z"},
        ]}],
    )

    result = asyncio.run(monitor.check())

    assert result.changed is False
    assert result.events == []
    assert storage.get(XUpdatesMonitor.LAST_POST_ID_KEY) == "900"
