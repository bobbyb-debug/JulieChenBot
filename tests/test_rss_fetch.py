"""Tests for JokersRSS.download()'s bounded, off-thread network fetch.

production/rss.py used to call feedparser.parse(url) directly, which
performs its own network fetch internally with no timeout parameter
in the installed feedparser version, and was called synchronously
from ProductionEngine.tick() -- a stalled connection could block the
entire asyncio event loop indefinitely.

These tests protect two separate things:

    1. download() itself: a fetch failure/timeout must degrade to an
       empty feed (matching the existing bozo/no-entries contract
       entries()/latest() already rely on), not raise -- and the
       network call must be given an explicit, bounded timeout.

    2. ProductionEngine.tick(): the RSS calls must actually execute
       off the event-loop thread, not merely be wrapped in something
       that happens to still run synchronously. Verified by checking
       the real OS thread the call ran on, and by proving a slow RSS
       fetch does not stall other concurrent event-loop work --
       exactly the regression this fix protects against.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from urllib.error import URLError

import production.rss as rss_module
from database.storage import Storage
from production.competition import CompetitionState
from production.engine import ProductionEngine
from production.house_status import HouseStatus
from production.rss import JokersRSS


class FakeStorage:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    @property
    def last_guid(self):
        return self.data.get("last_guid", "")

    @last_guid.setter
    def last_guid(self, value):
        self.data["last_guid"] = value

    @property
    def last_title(self):
        return self.data.get("last_title", "")

    @last_title.setter
    def last_title(self, value):
        self.data["last_title"] = value

    @property
    def last_published(self):
        return self.data.get("last_published", "")

    @last_published.setter
    def last_published(self, value):
        self.data["last_published"] = value


class FakeResponse:
    """Minimal stand-in for the object urlopen() returns."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._payload


# ==========================================================
# download(): bounded timeout
# ==========================================================


def test_download_passes_a_bounded_timeout_to_urlopen(monkeypatch):
    """The actual fix: the network fetch must be given an explicit,
    finite timeout, since feedparser.parse() itself has none."""

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["timeout"] = timeout
        return FakeResponse(b"<rss></rss>")

    monkeypatch.setattr(rss_module, "urlopen", fake_urlopen)

    JokersRSS(storage=FakeStorage()).download()

    # Exact value, not just "some bound" -- ties the test to the real
    # constant so a silent change (e.g. someone loosening the timeout
    # or reintroducing an unbounded fetch) is caught precisely.
    assert captured["timeout"] == 20
    assert captured["timeout"] == rss_module._FETCH_TIMEOUT


# ==========================================================
# download(): failure handling stays bounded, does not raise
# ==========================================================


def test_download_returns_empty_feed_on_fetch_failure_without_raising(monkeypatch):
    """A stalled/failed connection must degrade to the same 'no
    entries' outcome entries()/latest()/check_all() already handle
    for any other empty or malformed feed -- not raise."""

    def fake_urlopen(request, timeout=None):
        raise URLError("simulated timeout")

    monkeypatch.setattr(rss_module, "urlopen", fake_urlopen)

    rss = JokersRSS(storage=FakeStorage())

    feed = rss.download()  # must not raise
    assert feed.entries == []

    assert rss.entries() == []
    assert rss.latest() is None
    assert rss.check_all() == []


def test_download_failure_is_indistinguishable_from_empty_feed_downstream(monkeypatch):
    """check() (the older single-item API) must also degrade cleanly."""

    def fake_urlopen(request, timeout=None):
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(rss_module, "urlopen", fake_urlopen)

    rss = JokersRSS(storage=FakeStorage())
    assert rss.check() is None


# ==========================================================
# download(): parsing behavior is unchanged for a real payload
# ==========================================================

_SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <guid>guid-1</guid>
    <title>Test Update</title>
    <description>desc</description>
    <link>http://x/1</link>
    <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_download_still_parses_a_successful_fetch_correctly(monkeypatch):
    """Existing parsing behavior is preserved end to end: a real RSS
    payload fetched through the new bounded path still produces the
    same FeedUpdate data as before."""

    monkeypatch.setattr(
        rss_module, "urlopen", lambda request, timeout=None: FakeResponse(_SAMPLE_RSS)
    )

    entries = JokersRSS(storage=FakeStorage()).entries()

    assert len(entries) == 1
    assert entries[0].guid == "guid-1"
    assert entries[0].title == "Test Update"
    assert entries[0].link == "http://x/1"


# ==========================================================
# engine.tick(): a real RSS network failure does not take down
# the production cycle
# ==========================================================


class NullWatcher:
    """Stands in for ProductionWatcher so tick() never touches real
    monitors (network, Hamsterwatch archive, etc.) -- only the RSS
    call sites under test are exercised.

    house_status/competition are minimal stand-ins (blank .current,
    matching ProductionWatcher's real attributes) because tick() reads
    them unconditionally, before run(), to decide whether to persist
    game state -- see production/engine.py.
    """

    def __init__(self) -> None:
        self.house_status = SimpleNamespace(current=HouseStatus())
        self.competition = SimpleNamespace(current=CompetitionState())

    async def run(self):
        return [], []


class ThreadRecordingRSS:
    """Records which real OS thread check_all()/current() ran on, and
    can simulate a slow blocking call -- so tests can prove actual
    thread-offloading happened, not just that a wrapper function was
    referenced somewhere in the source."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.check_all_thread: threading.Thread | None = None
        self.current_thread: threading.Thread | None = None

    def check_all(self, limit=None):
        self.check_all_thread = threading.current_thread()
        if self.delay:
            time.sleep(self.delay)
        return []

    def current(self):
        self.current_thread = threading.current_thread()
        return None


def _make_engine(storage: Storage, rss) -> ProductionEngine:
    engine = ProductionEngine(storage=storage)
    engine.watcher = NullWatcher()
    engine.rss = rss
    return engine


def test_tick_completes_successfully_when_rss_fetch_fails(tmp_path, monkeypatch):
    """Integration-level proof using the REAL JokersRSS (not a test
    double): a genuine network failure in download() must degrade to
    the safe/empty result and let engine.tick() complete a normal
    cycle, rather than raising and aborting the whole tick (which
    would also stop every other monitor from running that cycle)."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    def failing_urlopen(request, timeout=None):
        raise URLError("simulated network failure")

    monkeypatch.setattr(rss_module, "urlopen", failing_urlopen)

    storage = Storage()
    engine = ProductionEngine(storage=storage)
    engine.watcher = NullWatcher()
    # engine.rss is the real JokersRSS built by ProductionEngine's own
    # __init__, pointed at the real config.RSS_FEED URL -- only the
    # module-level urlopen it calls is faked, so this exercises the
    # real download() -> check_all()/current() failure path.

    asyncio.run(engine.tick())

    assert engine.tick_count == 1
    assert engine.last_error is None
    assert engine.pending_event_count == 0


def test_tick_runs_rss_check_all_off_the_main_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    main_thread = threading.current_thread()
    rss = ThreadRecordingRSS()
    engine = _make_engine(Storage(), rss)

    asyncio.run(engine.tick())

    assert rss.check_all_thread is not None
    assert rss.check_all_thread is not main_thread


def test_tick_runs_rss_current_off_the_main_thread_on_first_launch(tmp_path, monkeypatch):
    """current() is only reached on the "no snapshot yet" branch --
    exercise that specific path directly rather than assuming it."""

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    main_thread = threading.current_thread()
    storage = Storage()
    assert not storage.last_guid  # confirms the "first launch" branch will run

    rss = ThreadRecordingRSS()
    engine = _make_engine(storage, rss)

    asyncio.run(engine.tick())

    assert rss.current_thread is not None
    assert rss.current_thread is not main_thread


def test_slow_rss_fetch_does_not_block_other_event_loop_work(tmp_path, monkeypatch):
    """The real-world failure mode this fix protects against: while a
    slow RSS fetch is in flight, the rest of the event loop (Discord
    heartbeats, other coroutines) must keep making progress.

    Simulated with a concurrent counter task racing a 0.3s "blocking"
    RSS call. If the RSS call is ever moved back onto the event loop
    thread (e.g. someone changes `await asyncio.to_thread(self.rss.
    check_all)` back to a direct `self.rss.check_all()`), the counter
    stalls for the full 0.3s instead of incrementing continuously,
    and this test fails.
    """

    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")

    rss = ThreadRecordingRSS(delay=0.3)
    engine = _make_engine(Storage(), rss)

    async def run_test() -> int:
        counter = 0
        stop_event = asyncio.Event()

        async def ticker() -> None:
            nonlocal counter
            while not stop_event.is_set():
                counter += 1
                await asyncio.sleep(0.01)

        ticker_task = asyncio.create_task(ticker())

        await engine.tick()

        stop_event.set()
        await ticker_task
        return counter

    counter = asyncio.run(run_test())

    # ~30 increments are possible in 0.3s at a 0.01s interval if the
    # event loop stayed responsive throughout. A regression that
    # blocks the loop for the RSS call's duration would leave this at
    # 0 or 1 (whatever ran before tick() started).
    assert counter >= 10, (
        f"only {counter} event-loop tick(s) ran during the simulated slow "
        "RSS fetch -- the event loop appears to have been blocked"
    )
