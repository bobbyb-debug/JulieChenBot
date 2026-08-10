import asyncio

from production.events import EventType
from production.house_image import HouseImageMonitor
from production.monitors import MonitorStatus


class FakeStorage:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def fetcher(value):
    async def _fetch():
        if isinstance(value, BaseException):
            raise value
        return value
    return _fetch


def test_first_run_creates_baseline_without_event():
    storage = FakeStorage()
    monitor = HouseImageMonitor(storage=storage, fetcher=fetcher(b"image-v1"))
    result = asyncio.run(monitor.check())
    assert result.status == MonitorStatus.HEALTHY
    assert result.changed is False
    assert result.events == []
    assert storage.get(monitor.STORAGE_KEY)


def test_unchanged_image_produces_no_event():
    storage = FakeStorage()
    monitor = HouseImageMonitor(storage=storage, fetcher=fetcher(b"image-v1"))
    asyncio.run(monitor.check())
    result = asyncio.run(monitor.check())
    assert result.changed is False
    assert result.events == []


def test_changed_image_produces_house_status_event():
    storage = FakeStorage()
    monitor = HouseImageMonitor(storage=storage, fetcher=fetcher(b"image-v1"))
    asyncio.run(monitor.check())
    monitor.fetcher = fetcher(b"image-v2")
    result = asyncio.run(monitor.check())
    assert result.changed is True
    assert len(result.events) == 1
    event = result.events[0]
    assert event.source == "HouseImage"
    assert event.event_type == EventType.IMAGE_CHANGED
    assert event.metadata["url"] == monitor.URL


def test_changed_image_updates_watermark():
    storage = FakeStorage()
    monitor = HouseImageMonitor(storage=storage, fetcher=fetcher(b"image-v1"))
    asyncio.run(monitor.check())
    first = storage.get(monitor.STORAGE_KEY)
    monitor.fetcher = fetcher(b"image-v2")
    asyncio.run(monitor.check())
    second = storage.get(monitor.STORAGE_KEY)
    assert first != second


def test_fetch_failure_is_degraded_and_does_not_create_event():
    monitor = HouseImageMonitor(
        storage=FakeStorage(),
        fetcher=fetcher(ConnectionError("offline")),
    )
    result = asyncio.run(monitor.check())
    assert result.status == MonitorStatus.DEGRADED
    assert result.changed is False
    assert result.events == []
