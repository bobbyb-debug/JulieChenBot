"""Tests for house-status image URL discovery and rediscovery."""

from __future__ import annotations

import asyncio

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from production.house_image import HouseImageMonitor
from production.monitors import MonitorStatus

PAGE = (
    '<html><body><img src="/ubbthreads/images/headers/bigbrother/hg/'
    'bbupdatesblock1786231774.png"></body></html>'
)

PAGE_ROTATED = (
    '<html><body><img src="/ubbthreads/images/headers/bigbrother/hg/'
    'bbupdatesblock1786999999.png"></body></html>'
)


class FakeStorage:
    """In-memory stand-in matching the branch's existing test convention."""

    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def temp_storage() -> FakeStorage:
    return FakeStorage()


def make_monitor(storage, page, image_map, start_url="http://old/stale.png"):
    """Builds a monitor whose network is fully faked.

    image_map maps URL -> bytes, or URL -> Exception to simulate a 404.
    """

    calls = {"pages": 0, "images": []}

    async def page_fetcher():
        calls["pages"] += 1
        return page

    monitor = HouseImageMonitor(
        storage=storage,
        page_url="https://www.jokersupdates.com/",
        page_fetcher=page_fetcher,
    )
    monitor.image_url = start_url

    async def fetch_image(url):
        calls["images"].append(url)
        result = image_map.get(url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError(f"404 for {url}")
        return result

    monitor._fetch_image = fetch_image

    return monitor, calls


DISCOVERED = (
    "https://www.jokersupdates.com/ubbthreads/images/headers/"
    "bigbrother/hg/bbupdatesblock1786231774.png"
)

ROTATED = (
    "https://www.jokersupdates.com/ubbthreads/images/headers/"
    "bigbrother/hg/bbupdatesblock1786999999.png"
)


async def _test_discovers_url_from_page():
    monitor, _ = make_monitor(temp_storage(), PAGE, {})
    assert await monitor.discover() == DISCOVERED


async def _test_discovery_returns_none_when_no_image_on_page():
    monitor, _ = make_monitor(temp_storage(), "<html>nothing here</html>", {})
    assert await monitor.discover() is None


async def _test_stale_url_triggers_rediscovery():
    storage = temp_storage()
    monitor, calls = make_monitor(
        storage,
        PAGE,
        {DISCOVERED: b"image-bytes"},  # stale start_url absent -> 404
    )

    result = await monitor.check()

    assert result.status == MonitorStatus.HEALTHY
    assert calls["pages"] == 1  # rediscovery happened
    assert monitor.image_url == DISCOVERED
    # discovered URL persisted for restart survival
    assert storage.get("house_image_last_url") == DISCOVERED


async def _test_discovered_url_survives_restart():
    storage = temp_storage()
    monitor, _ = make_monitor(storage, PAGE, {DISCOVERED: b"image-bytes"})
    await monitor.check()

    # Simulate restart: a brand new monitor reads the remembered URL
    restarted = HouseImageMonitor(storage=storage)
    assert restarted.image_url == DISCOVERED


async def _test_same_url_same_content_reports_no_change():
    storage = temp_storage()
    monitor, _ = make_monitor(
        storage, PAGE, {DISCOVERED: b"same"}, start_url=DISCOVERED
    )

    first = await monitor.check()
    assert first.changed is False  # initial snapshot

    second = await monitor.check()
    assert second.changed is False
    assert second.status == MonitorStatus.HEALTHY


async def _test_same_url_changed_content_emits_image_changed():
    storage = temp_storage()
    monitor, _ = make_monitor(
        storage, PAGE, {DISCOVERED: b"first"}, start_url=DISCOVERED
    )
    await monitor.check()  # snapshot

    async def changed(url):
        return b"second"

    monitor._fetch_image = changed

    result = await monitor.check()

    assert result.changed is True
    assert len(result.events) == 1
    assert result.events[0].event_type.value == "image_changed"


async def _test_rotated_filename_same_content_is_not_a_content_change():
    """A rotated URL with identical bytes must NOT announce."""

    storage = temp_storage()
    monitor, _ = make_monitor(
        storage, PAGE, {DISCOVERED: b"identical"}, start_url=DISCOVERED
    )
    await monitor.check()  # snapshot

    # Filename rotates, bytes are identical
    monitor.page_fetcher = None

    async def rotated_page():
        return PAGE_ROTATED

    monitor.page_fetcher = rotated_page

    async def fetch(url):
        if url == DISCOVERED:
            raise RuntimeError("404 - rotated away")
        return b"identical"

    monitor._fetch_image = fetch

    result = await monitor.check()

    assert monitor.image_url == ROTATED  # discovery happened
    assert result.changed is False  # but content did NOT change
    assert result.events == []
    assert "rotated" in result.detail.lower()


async def _test_rotated_filename_changed_content_does_announce():
    storage = temp_storage()
    monitor, _ = make_monitor(
        storage, PAGE, {DISCOVERED: b"original"}, start_url=DISCOVERED
    )
    await monitor.check()  # snapshot

    async def rotated_page():
        return PAGE_ROTATED

    monitor.page_fetcher = rotated_page

    async def fetch(url):
        if url == DISCOVERED:
            raise RuntimeError("404 - rotated away")
        return b"brand-new-board"

    monitor._fetch_image = fetch

    result = await monitor.check()

    assert monitor.image_url == ROTATED
    assert result.changed is True
    assert len(result.events) == 1


async def _test_total_failure_is_degraded_not_crash():
    storage = temp_storage()
    monitor, _ = make_monitor(storage, "<html>no image</html>", {})

    result = await monitor.check()

    assert result.status == MonitorStatus.DEGRADED
    assert result.changed is False
    assert result.events == []


def test_discovers_url_from_page():
    asyncio.run(_test_discovers_url_from_page())

def test_discovery_returns_none_when_no_image_on_page():
    asyncio.run(_test_discovery_returns_none_when_no_image_on_page())

def test_stale_url_triggers_rediscovery():
    asyncio.run(_test_stale_url_triggers_rediscovery())

def test_discovered_url_survives_restart():
    asyncio.run(_test_discovered_url_survives_restart())

def test_same_url_same_content_reports_no_change():
    asyncio.run(_test_same_url_same_content_reports_no_change())

def test_same_url_changed_content_emits_image_changed():
    asyncio.run(_test_same_url_changed_content_emits_image_changed())

def test_rotated_filename_same_content_is_not_a_content_change():
    asyncio.run(_test_rotated_filename_same_content_is_not_a_content_change())

def test_rotated_filename_changed_content_does_announce():
    asyncio.run(_test_rotated_filename_changed_content_does_announce())

def test_total_failure_is_degraded_not_crash():
    asyncio.run(_test_total_failure_is_degraded_not_crash())


REAL_MARKUP = (
    '<img class="bbhgblockimg" src="https://www.jokersupdates.com/'
    'ubbthreads/images/headers/bigbrother/hg/bbupdatesblock1786231774.png" '
    'width="175" height="266" border="0">'
)


async def _test_prefers_classed_img_over_decoy():
    """The bbhgblockimg class anchor must win over an earlier decoy."""

    decoy = '<img src="/old/bbupdatesblock1111111111.png">' + REAL_MARKUP
    monitor, _ = make_monitor(temp_storage(), decoy, {})

    assert await monitor.discover() == DISCOVERED


async def _test_falls_back_to_bare_filename_without_class():
    """Discovery still works if the class attribute ever disappears."""

    monitor, _ = make_monitor(temp_storage(), PAGE, {})

    assert await monitor.discover() == DISCOVERED


def test_prefers_classed_img_over_decoy():
    asyncio.run(_test_prefers_classed_img_over_decoy())


def test_falls_back_to_bare_filename_without_class():
    asyncio.run(_test_falls_back_to_bare_filename_without_class())
