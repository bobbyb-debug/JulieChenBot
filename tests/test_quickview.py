import asyncio

from production.events import EventType
from production.monitors import MonitorStatus
from production.quickview import QuickviewMonitor
from production.watcher import ProductionWatcher


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


def test_first_run_baselines_without_event():
    storage = FakeStorage()
    monitor = QuickviewMonitor(storage=storage, fetcher=fetcher("<body>v1</body>"))
    result = asyncio.run(monitor.check())
    assert result.status == MonitorStatus.HEALTHY
    assert result.changed is False
    assert result.events == []
    assert storage.get(monitor.STORAGE_KEY)


def test_unchanged_page_produces_no_event():
    storage = FakeStorage()
    monitor = QuickviewMonitor(storage=storage, fetcher=fetcher("<body>v1</body>"))
    asyncio.run(monitor.check())
    result = asyncio.run(monitor.check())
    assert result.changed is False
    assert result.events == []


def test_changed_page_produces_one_timeline_event():
    storage = FakeStorage()
    monitor = QuickviewMonitor(storage=storage, fetcher=fetcher("<body>v1</body>"))
    asyncio.run(monitor.check())
    monitor.fetcher = fetcher("<body>v2</body>")
    result = asyncio.run(monitor.check())
    assert result.changed is True
    assert len(result.events) == 1
    assert result.events[0].source == "JokersUpdates Quickview"
    assert result.events[0].event_type == EventType.TIMELINE
    assert result.events[0].metadata["url"] == monitor.URL


def test_fetch_failure_is_degraded():
    monitor = QuickviewMonitor(
        storage=FakeStorage(),
        fetcher=fetcher(ConnectionError("offline")),
    )
    result = asyncio.run(monitor.check())
    assert result.status == MonitorStatus.DEGRADED
    assert result.changed is False
    assert result.events == []


def test_quickview_is_instantiated_but_not_registered():
    """Quickview/BBUpdates duplicate RSS's own board with whole-page
    hashing and no real content extraction, so they're kept available
    but intentionally not part of the active monitor set."""

    watcher = ProductionWatcher(storage=FakeStorage())
    assert watcher.quickview not in watcher.monitors
    assert watcher.bb_updates not in watcher.monitors
    assert watcher.total_monitors == 5
