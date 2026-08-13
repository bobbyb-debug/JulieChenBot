"""Tests for production/hamsterwatch.py (the monitor/orchestration layer).

Exercises discovery, bootstrap import, idempotency across ticks,
malformed/unavailable page handling, and significant-change-driven
Discord events — with parsing (test_hamsterwatch_parser.py) and
storage/retrieval (test_hamsterwatch_archive.py) covered separately.
"""

from __future__ import annotations

import asyncio

from database.hamsterwatch_archive import HamsterwatchArchive
from production.events import EventType
from production.hamsterwatch import HamsterwatchMonitor
from production.monitors import MonitorStatus
from production.watcher import ProductionWatcher

# HamsterwatchArchive's default database path is redirected to a temp
# file for every test in this session — see tests/conftest.py's
# _isolate_hamsterwatch_archive_file autouse fixture.


class FakeStorage:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


# ==========================================================
# Fixtures / fakes
# ==========================================================


def page_html(day: int, weekday: str, date_str: str, content: str) -> str:
    return (
        "<H1>Daily Feeds Recaps</H1>\n"
        f"<H3>Day {day} - {weekday} - {date_str}</H3>\n"
        f"{content}<BR>\n"
        "<H1>Season Stats</H1>"
    )


def index_html_for(urls: list[str]) -> str:
    return "\n".join(f'<A HREF="{url}">link</A>' for url in urls)


def index_fetcher(html: str = "", error: Exception | None = None):
    async def _fetch() -> str:
        if error is not None:
            raise error
        return html
    return _fetch


def page_fetcher(pages: dict[str, str | Exception]):
    async def _fetch(url: str) -> str:
        value = pages.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ConnectionError(f"no fixture registered for {url}")
        return value
    return _fetch


def make_monitor(
    tmp_path,
    *,
    seed_urls: tuple[str, ...] = (),
    index_html: str = "",
    index_error: Exception | None = None,
    pages: dict[str, str | Exception] | None = None,
):
    archive = HamsterwatchArchive(db_path=tmp_path / "archive.db")
    monitor = HamsterwatchMonitor(
        storage=FakeStorage(),
        archive=archive,
        index_fetcher=index_fetcher(index_html, index_error),
        page_fetcher=page_fetcher(pages or {}),
    )
    monitor.SEED_URLS = seed_urls
    return monitor, archive


URL_A = "http://hamsterwatch.com/bb28/070926.shtml"
URL_B = "http://hamsterwatch.com/bb28/071126.shtml"


# ==========================================================
# Historical import (bootstrap)
# ==========================================================


def test_bootstrap_imports_discovered_pages_silently(tmp_path):
    pages = {
        URL_A: page_html(3, "Thursday", "July 9, 2026", "Move-in chaos begins."),
        URL_B: page_html(5, "Saturday", "July 11, 2026", "First veto ceremony happens."),
    }
    monitor, archive = make_monitor(
        tmp_path, index_html=index_html_for([URL_A, URL_B]), pages=pages,
    )

    result = asyncio.run(monitor.check())

    assert result.status == MonitorStatus.HEALTHY
    assert result.changed is False
    assert result.events == []
    assert archive.count() == 2
    assert archive.known_page_urls() == {URL_A, URL_B}


def test_bootstrap_falls_back_to_seed_urls_when_index_unavailable(tmp_path):
    seed_url = "http://hamsterwatch.com/bb28/preseason.shtml"
    monitor, archive = make_monitor(
        tmp_path,
        seed_urls=(seed_url,),
        index_error=ConnectionError("index down"),
        pages={seed_url: page_html(1, "Tuesday", "July 7, 2026", "Move-in day.")},
    )

    result = asyncio.run(monitor.check())

    assert result.status == MonitorStatus.HEALTHY
    assert archive.count() == 1
    assert archive.known_page_urls() == {seed_url}


def test_bootstrap_never_fetches_more_than_max_pages_per_tick(tmp_path):
    """H1 fix: bootstrap must be capped exactly like steady-state,
    even when far more than MAX_PAGES_PER_TICK pages are discovered
    on the very first run."""

    # Real dated-page pattern (/bb28/MMDDYY.shtml) — extract_archive_links
    # only recognizes 6-digit dated pages or preseason*.shtml, so the
    # fixture URLs must match that shape or discovery silently drops them.
    urls = [f"http://hamsterwatch.com/bb28/{700000 + i:06d}.shtml" for i in range(15)]
    fetched: list[str] = []

    pages = {}
    for i, url in enumerate(urls):
        pages[url] = page_html(i + 1, "Monday", f"July {i + 1}, 2026", f"Recap {i}.")

    def counting_page_fetcher(pages):
        async def _fetch(url: str) -> str:
            fetched.append(url)
            return pages[url]
        return _fetch

    monitor, archive = make_monitor(
        tmp_path, index_html=index_html_for(urls), pages=pages,
    )
    monitor.page_fetcher = counting_page_fetcher(pages)

    result = asyncio.run(monitor.check())

    assert len(fetched) == HamsterwatchMonitor.MAX_PAGES_PER_TICK
    assert archive.count() == HamsterwatchMonitor.MAX_PAGES_PER_TICK
    assert result.status == MonitorStatus.HEALTHY
    assert result.changed is False  # still mid-backfill, silent


def test_full_historical_backfill_completes_silently_across_ticks_then_resumes_announcing(tmp_path):
    """A season's worth of history (more than MAX_PAGES_PER_TICK pages)
    must import completely without ever announcing to Discord, no
    matter how many ticks it takes — and once caught up, genuinely
    new content afterward must announce normally again."""

    urls = [f"http://hamsterwatch.com/bb28/{710000 + i:06d}.shtml" for i in range(14)]

    def page_for(url: str) -> str:
        i = urls.index(url)
        return page_html(i + 1, "Monday", f"July {i + 1}, 2026", f"Recap content {i}.")

    monitor, archive = make_monitor(tmp_path, index_html=index_html_for(urls), pages={})
    monitor.page_fetcher = page_fetcher({url: page_for(url) for url in urls})

    seen_events = []
    for _ in range(4):  # 14 pages / 6 per tick -> 3 ticks to finish, +1 extra
        result = asyncio.run(monitor.check())
        seen_events.extend(result.events)

    assert archive.count() == 14
    assert archive.known_page_urls() == set(urls)
    assert seen_events == []  # never announced during backfill

    # Now something genuinely new shows up after the backlog is caught up.
    new_url = "http://hamsterwatch.com/bb28/999999.shtml"
    urls_with_new = urls + [new_url]
    monitor.index_fetcher = index_fetcher(index_html_for(urls_with_new))
    monitor.page_fetcher = page_fetcher({
        **{url: page_for(url) for url in urls},
        new_url: page_html(15, "Tuesday", "July 15, 2026", "Fresh news for real."),
    })

    result = asyncio.run(monitor.check())

    assert result.changed is True
    assert len(result.events) == 1
    assert "Fresh news for real." in result.events[0].detail


# ==========================================================
# Idempotency across ticks / unchanged content
# ==========================================================


def test_second_run_does_not_duplicate_or_reannounce_unchanged_content(tmp_path):
    monitor, archive = make_monitor(
        tmp_path,
        index_html=index_html_for([URL_A]),
        pages={URL_A: page_html(3, "Thursday", "July 9, 2026", "Move-in chaos begins.")},
    )

    first = asyncio.run(monitor.check())
    assert first.changed is False  # bootstrap: silent

    second = asyncio.run(monitor.check())

    assert second.changed is False
    assert second.events == []
    assert archive.count() == 1


# ==========================================================
# Changed articles / discovery of growth on the "current" page
# ==========================================================


def test_new_day_appended_to_current_page_produces_one_event(tmp_path):
    monitor, archive = make_monitor(
        tmp_path,
        index_html=index_html_for([URL_A]),
        pages={URL_A: page_html(3, "Thursday", "July 9, 2026", "Move-in chaos begins." * 20)},
    )
    asyncio.run(monitor.check())  # bootstrap

    grown_page = (
        "<H1>Daily Feeds Recaps</H1>\n"
        "<H3>Day 4 - Friday - July 10, 2026</H3>\n"
        "Yash won the first HOH of the season.<BR>\n"
        "<H3>Day 3 - Thursday - July 9, 2026</H3>\n"
        + ("Move-in chaos begins." * 20) + "<BR>\n"
        "<H1>Season Stats</H1>"
    )
    monitor.page_fetcher = page_fetcher({URL_A: grown_page})

    result = asyncio.run(monitor.check())

    assert result.status == MonitorStatus.HEALTHY
    assert result.changed is True
    assert len(result.events) == 1
    event = result.events[0]
    assert event.source == "Hamsterwatch"
    assert event.event_type == EventType.TIMELINE
    assert event.metadata["bb_day"] == 4
    assert event.metadata["url"] == URL_A
    assert "Yash" in event.detail
    assert archive.count() == 2  # day 3 (unchanged) + day 4 (new)


def test_new_page_discovered_from_index_gets_imported(tmp_path):
    monitor, archive = make_monitor(
        tmp_path,
        index_html=index_html_for([URL_A]),
        pages={URL_A: page_html(3, "Thursday", "July 9, 2026", "Move-in chaos.")},
    )
    asyncio.run(monitor.check())  # bootstrap
    assert archive.known_page_urls() == {URL_A}

    monitor.index_fetcher = index_fetcher(index_html_for([URL_A, URL_B]))
    monitor.page_fetcher = page_fetcher({
        URL_A: page_html(3, "Thursday", "July 9, 2026", "Move-in chaos."),
        URL_B: page_html(5, "Saturday", "July 11, 2026", "Ashley evicted this week."),
    })

    result = asyncio.run(monitor.check())

    assert result.changed is True
    assert archive.known_page_urls() == {URL_A, URL_B}
    assert result.events[0].metadata["bb_day"] == 5


# ==========================================================
# Malformed / unavailable pages
# ==========================================================


def test_malformed_page_never_crashes_but_is_reported_degraded(tmp_path):
    """A page that fetches fine but parses to zero recap sections must
    not crash the monitor — and, since H1 of the code review, must be
    reported as degraded (not silently healthy) so a persistent
    markup/parsing mismatch is visible rather than invisible."""

    monitor, archive = make_monitor(
        tmp_path,
        index_html=index_html_for([URL_A]),
        pages={URL_A: "<html><body>Not a recap page at all.</body></html>"},
    )

    result = asyncio.run(monitor.check())

    assert result.status == MonitorStatus.DEGRADED
    assert result.changed is False
    assert result.events == []
    assert archive.count() == 0


def test_repeated_zero_section_parsing_stays_capped_and_degraded_forever(tmp_path):
    """H1 fix, end to end: if every page in a large discovered set
    permanently parses to zero sections (e.g. a site redesign), the
    monitor must never fetch more than MAX_PAGES_PER_TICK pages in a
    single check(), on any tick — and must keep reporting degraded
    rather than settling into a silent, misleading 'healthy'."""

    urls = [f"http://hamsterwatch.com/bb28/{900000 + i:06d}.shtml" for i in range(20)]
    fetch_counts: list[int] = []

    def counting_page_fetcher():
        async def _fetch(url: str) -> str:
            fetch_counts.append(1)
            return "<html><body>redesigned site, no recap markup</body></html>"
        return _fetch

    monitor, archive = make_monitor(tmp_path, index_html=index_html_for(urls), pages={})
    monitor.page_fetcher = counting_page_fetcher()

    for _ in range(4):
        fetch_counts.clear()
        result = asyncio.run(monitor.check())

        assert len(fetch_counts) <= HamsterwatchMonitor.MAX_PAGES_PER_TICK
        assert result.status == MonitorStatus.DEGRADED
        assert result.changed is False
        assert result.events == []
        assert archive.count() == 0


def test_unavailable_page_is_skipped_while_others_still_import(tmp_path):
    monitor, archive = make_monitor(
        tmp_path,
        index_html=index_html_for([URL_A, URL_B]),
        pages={
            URL_A: page_html(3, "Thursday", "July 9, 2026", "Move-in chaos begins."),
            URL_B: ConnectionError("offline"),
        },
    )

    result = asyncio.run(monitor.check())

    assert result.status == MonitorStatus.HEALTHY
    assert archive.count() == 1
    assert archive.known_page_urls() == {URL_A}


def test_total_fetch_failure_is_degraded(tmp_path):
    monitor, archive = make_monitor(
        tmp_path,
        index_html=index_html_for([URL_A]),
        pages={URL_A: ConnectionError("offline")},
    )

    result = asyncio.run(monitor.check())

    assert result.status == MonitorStatus.DEGRADED
    assert result.changed is False
    assert result.events == []
    assert archive.count() == 0


class _BrokenArchive:
    """Minimal archive stand-in whose upsert() always raises.

    Index and page fetch failures are already caught locally (inside
    _discover_pages and _import_pages respectively), so neither can
    ever reach check()'s own outer `except Exception`. An archive
    write failure (e.g. a corrupted database file) is not caught
    anywhere closer — this is what actually exercises that backstop.
    """

    def count(self) -> int:
        return 0

    def known_page_urls(self) -> set[str]:
        return set()

    def latest_page_url(self) -> str | None:
        return None

    def upsert(self, **kwargs):
        raise RuntimeError("archive is corrupted")


def test_unexpected_archive_error_is_caught_and_reported_degraded(tmp_path):
    monitor = HamsterwatchMonitor(
        storage=FakeStorage(),
        archive=_BrokenArchive(),
        index_fetcher=index_fetcher(index_html_for([URL_A])),
        page_fetcher=page_fetcher(
            {URL_A: page_html(3, "Thursday", "July 9, 2026", "Move-in chaos.")}
        ),
    )
    monitor.SEED_URLS = ()

    result = asyncio.run(monitor.check())

    assert result.status == MonitorStatus.DEGRADED
    assert result.changed is False
    assert result.events == []
    assert "archive is corrupted" in result.detail


# ==========================================================
# Watcher integration
# ==========================================================


def test_hamsterwatch_is_registered_with_watcher():
    watcher = ProductionWatcher(storage=FakeStorage())
    assert watcher.hamsterwatch in watcher.monitors
    assert watcher.total_monitors == 5


def test_hamsterwatch_event_flows_through_watcher(tmp_path):
    watcher = ProductionWatcher(storage=FakeStorage())

    archive = HamsterwatchArchive(db_path=tmp_path / "archive.db")
    watcher.hamsterwatch.archive = archive
    watcher.hamsterwatch.SEED_URLS = ()
    watcher.hamsterwatch.index_fetcher = index_fetcher(index_html_for([URL_A]))
    watcher.hamsterwatch.page_fetcher = page_fetcher(
        {URL_A: page_html(3, "Thursday", "July 9, 2026", "Move-in chaos.")}
    )

    asyncio.run(watcher.run())  # bootstrap, silent

    grown_page = (
        "<H1>Daily Feeds Recaps</H1>\n"
        "<H3>Day 4 - Friday - July 10, 2026</H3>\nYash won HOH.<BR>\n"
        "<H3>Day 3 - Thursday - July 9, 2026</H3>\nMove-in chaos.<BR>\n"
        "<H1>Season Stats</H1>"
    )
    watcher.hamsterwatch.page_fetcher = page_fetcher({URL_A: grown_page})

    _, events = asyncio.run(watcher.run())
    hamster_events = [event for event in events if event.source == "Hamsterwatch"]

    assert len(hamster_events) == 1
    assert hamster_events[0].event_type == EventType.TIMELINE
