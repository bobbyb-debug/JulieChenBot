import asyncio

from production.events import EventType
from production.hamsterwatch import HamsterwatchMonitor
from production.monitors import MonitorStatus
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
    monitor = HamsterwatchMonitor(storage=storage, fetcher=fetcher("<body>v1</body>"))
    result = asyncio.run(monitor.check())
    assert result.status == MonitorStatus.HEALTHY
    assert result.changed is False
    assert result.events == []
    assert storage.get(monitor.STORAGE_KEY)


def test_unchanged_page_produces_no_event():
    storage = FakeStorage()
    monitor = HamsterwatchMonitor(storage=storage, fetcher=fetcher("<body>v1</body>"))
    asyncio.run(monitor.check())
    result = asyncio.run(monitor.check())
    assert result.changed is False
    assert result.events == []


def test_changed_page_produces_one_timeline_event():
    storage = FakeStorage()
    monitor = HamsterwatchMonitor(storage=storage, fetcher=fetcher("<body>v1</body>"))
    asyncio.run(monitor.check())
    monitor.fetcher = fetcher("<body>v2</body>")
    result = asyncio.run(monitor.check())
    assert result.changed is True
    assert len(result.events) == 1
    assert result.events[0].source == "Hamsterwatch"
    assert result.events[0].event_type == EventType.TIMELINE
    assert result.events[0].metadata["url"] == monitor.URL


def test_fetch_failure_is_degraded():
    monitor = HamsterwatchMonitor(
        storage=FakeStorage(),
        fetcher=fetcher(ConnectionError("offline")),
    )
    result = asyncio.run(monitor.check())
    assert result.status == MonitorStatus.DEGRADED
    assert result.changed is False
    assert result.events == []


def test_hamsterwatch_is_registered_with_watcher():
    watcher = ProductionWatcher(storage=FakeStorage())
    assert watcher.hamsterwatch in watcher.monitors
    assert watcher.total_monitors == 5


def test_hamsterwatch_event_flows_through_watcher():
    storage = FakeStorage()
    watcher = ProductionWatcher(storage=storage)
    watcher.hamsterwatch.fetcher = fetcher("<body>v1</body>")
    asyncio.run(watcher.run())
    watcher.hamsterwatch.fetcher = fetcher("<body>v2</body>")
    _, events = asyncio.run(watcher.run())
    hamster_events = [event for event in events if event.source == "Hamsterwatch"]
    assert len(hamster_events) == 1
    assert hamster_events[0].event_type == EventType.TIMELINE
