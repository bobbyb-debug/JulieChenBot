"""Hamsterwatch: a persistent BB28 knowledge source, not just a page-changed monitor.

Discovers and archives Dingo's Hamsterwatch BB28 daily recaps (see
production/hamsterwatch_parser.py for page structure) into a durable,
queryable store (database/hamsterwatch_archive.py), and announces
genuinely new or meaningfully changed recap content to Discord.

Fetching (this module) stays deliberately separate from parsing
(hamsterwatch_parser) and storage/retrieval (hamsterwatch_archive) —
this module's only job is deciding *what* to fetch and *when*, and
turning archive outcomes into a ProductionEvent.

Discovery and "don't hammer the site"
--------------------------------------
The Hamsterwatch BB28 daily index (/bb28/days.shtml) lists every
dated recap page for the season and is the source of truth for
finding new pages — far more reliable than guessing at date-based
filenames. Each tick fetches that one index page, plus only:

    - pages listed there that the archive has never seen, and
    - the single most-recently-known page (since Hamsterwatch keeps
      appending new day-sections to its "current" page rather than
      always minting a new URL).

always bounded by MAX_PAGES_PER_TICK — including the very first
(bootstrap) run. A brand new season's historical backlog is imported
silently (no Discord spam for 17 pages of history), spread across as
many ticks as it takes rather than fetched in one uncapped burst.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from urllib.request import Request, urlopen

from database.hamsterwatch_archive import HamsterwatchArchive, UpsertOutcome
from production.events import EventSeverity, EventType, ProductionEvent
from production.hamsterwatch_parser import extract_archive_links, parse_recap_sections
from production.monitors import Monitor, MonitorResult, MonitorStatus
from services.logger import ProductionLogger

logger = ProductionLogger.get("Hamsterwatch")

IndexFetcher = Callable[[], Awaitable[str]]
PageFetcher = Callable[[str], Awaitable[str]]

DEFAULT_INDEX_URL = "http://hamsterwatch.com/bb28/days.shtml"

# The BB28 archive sources Julie was seeded with. extract_archive_links()
# discovers these same URLs (and any future ones) directly from the
# live days.shtml index; this literal list is a fallback so the
# initial backfill still works even if that index is briefly
# unreachable on Julie's very first run.
SEED_URLS: tuple[str, ...] = (
    "http://hamsterwatch.com/bb28/081026.shtml",
    "http://hamsterwatch.com/bb28/080826.shtml",
    "http://hamsterwatch.com/bb28/080626.shtml",
    "http://hamsterwatch.com/bb28/080326.shtml",
    "http://hamsterwatch.com/bb28/080126.shtml",
    "http://hamsterwatch.com/bb28/073026.shtml",
    "http://hamsterwatch.com/bb28/072726.shtml",
    "http://hamsterwatch.com/bb28/072526.shtml",
    "http://hamsterwatch.com/bb28/072326.shtml",
    "http://hamsterwatch.com/bb28/072026.shtml",
    "http://hamsterwatch.com/bb28/071826.shtml",
    "http://hamsterwatch.com/bb28/071626.shtml",
    "http://hamsterwatch.com/bb28/071326.shtml",
    "http://hamsterwatch.com/bb28/071126.shtml",
    "http://hamsterwatch.com/bb28/070926.shtml",
    "http://hamsterwatch.com/bb28/preseason2.shtml",
    "http://hamsterwatch.com/bb28/preseason.shtml",
)

_USER_AGENT = "JulieChenBot/1.0"


class HamsterwatchMonitor(Monitor):
    """Archives Hamsterwatch BB28 recaps and announces meaningful updates."""

    INDEX_URL = DEFAULT_INDEX_URL
    SEED_URLS = SEED_URLS

    # Safety cap on fetches within a single tick, even if a burst of
    # newly discovered pages shows up at once (e.g. after downtime).
    MAX_PAGES_PER_TICK = 6

    def __init__(
        self,
        storage=None,
        archive: HamsterwatchArchive | None = None,
        index_fetcher: IndexFetcher | None = None,
        page_fetcher: PageFetcher | None = None,
        *,
        index_url: str | None = None,
    ) -> None:
        super().__init__()

        # Accepted for constructor compatibility with ProductionWatcher,
        # which wires every monitor with storage=self.storage. Archive
        # state now lives in HamsterwatchArchive's own SQLite store, so
        # this is otherwise unused.
        self.storage = storage

        self.archive = archive or HamsterwatchArchive()
        self.index_url = index_url or self.INDEX_URL
        self.index_fetcher = index_fetcher or self._fetch_index
        self.page_fetcher = page_fetcher or self._fetch_page

        # True only when this process started with a genuinely empty
        # archive. Historical backfill is now spread across multiple
        # capped ticks (MAX_PAGES_PER_TICK), so "is this tick part of
        # the initial silent catch-up" can no longer be re-derived each
        # tick from archive.count() == 0 — that flips to False as soon
        # as the first page of backlog is stored, which would make
        # every subsequent backfill tick announce to Discord as if it
        # were fresh news. This in-memory flag (not persisted — no
        # archive schema change) stays True until a tick clears the
        # entire discovered backlog, then flips permanently to False.
        self._bootstrapping = self.archive.count() == 0

        logger.info(
            "Hamsterwatch monitor initialized (archive size=%d).",
            self.archive.count(),
        )

    # ==========================================================
    # Network (off the event loop via asyncio.to_thread)
    # ==========================================================

    @staticmethod
    def _fetch_sync(url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(request, timeout=20) as response:
            return response.read().decode("windows-1252", errors="replace")

    async def _fetch_index(self) -> str:
        return await asyncio.to_thread(self._fetch_sync, self.index_url)

    async def _fetch_page(self, url: str) -> str:
        return await asyncio.to_thread(self._fetch_sync, url)

    # ==========================================================
    # Check
    # ==========================================================

    async def check(self) -> MonitorResult:
        try:
            discovered, index_error = await self._discover_pages()
            targets = self._select_targets(discovered)

            if not targets:
                status = MonitorStatus.DEGRADED if index_error else MonitorStatus.HEALTHY
                detail = (
                    f"Hamsterwatch index unavailable and no pages to check: {index_error}"
                    if index_error
                    else "Hamsterwatch: no BB28 pages to check."
                )
                return MonitorResult(
                    monitor=self.name,
                    status=status,
                    changed=False,
                    detail=detail,
                    metadata={"index_url": self.index_url},
                )

            outcomes, failed = await self._import_pages(targets)
            fetched_count = len(targets) - len(failed)

            if not outcomes and failed and len(failed) == len(targets):
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.DEGRADED,
                    changed=False,
                    detail=(
                        f"Hamsterwatch check failed: could not fetch any of "
                        f"{len(targets)} page(s)."
                    ),
                    metadata={"index_url": self.index_url, "failed_pages": failed},
                )

            if not outcomes and fetched_count > 0:
                # Every fetched page returned HTML but produced zero
                # recognizable recap sections — a markup/parsing mismatch,
                # not a one-off fluke. Reported as degraded (rather than
                # falling through to "healthy, imported 0 sections") so a
                # persistent site-structure change is visible instead of
                # silently reporting healthy while retrying forever.
                logger.warning(
                    "Hamsterwatch fetched %d page(s) but parsed zero recap sections.",
                    fetched_count,
                )
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.DEGRADED,
                    changed=False,
                    detail=(
                        f"Hamsterwatch check failed: fetched {fetched_count} "
                        "page(s) but found no recognizable recap sections."
                    ),
                    metadata={"index_url": self.index_url, "targets": targets},
                )

            metadata = {
                "index_url": self.index_url,
                "pages_checked": len(targets) - len(failed),
                "pages_failed": len(failed),
                "archive_size": self.archive.count(),
            }

            # Snapshot before possibly clearing it below: this tick's own
            # silence decision must reflect whether backfill was still in
            # progress when the tick *started*, not whether it happens to
            # finish as a side effect of this tick's own imports.
            importing_backlog = self._bootstrapping
            if self._bootstrapping and not (discovered - self.archive.known_page_urls()):
                self._bootstrapping = False

            if importing_backlog:
                logger.info(
                    "Hamsterwatch archive bootstrap imported %d section(s) from %d page(s).",
                    len(outcomes), len(targets),
                )
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail=(
                        f"Imported {len(outcomes)} historical Hamsterwatch "
                        f"section(s) from {len(targets)} page(s)."
                    ),
                    metadata=metadata,
                )

            significant = [o for o in outcomes if o.is_new or o.significant_change]

            if not significant:
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail="Hamsterwatch unchanged.",
                    metadata=metadata,
                )

            event = self._build_event(significant)
            logger.info("Hamsterwatch: %d new/updated section(s).", len(significant))

            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.HEALTHY,
                changed=True,
                detail=event.detail,
                events=[event],
                metadata=metadata,
            )

        except Exception as exc:
            logger.exception("Hamsterwatch check failed.")
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.DEGRADED,
                changed=False,
                detail=f"Hamsterwatch check failed: {exc}",
                metadata={"index_url": self.index_url},
            )

    # ==========================================================
    # Discovery / target selection
    # ==========================================================

    async def _discover_pages(self) -> tuple[set[str], str | None]:
        """Returns (discovered page URLs, index error message or None).

        Always includes SEED_URLS regardless of index outcome, so the
        initial historical backfill is robust even if days.shtml is
        briefly unreachable.
        """

        discovered = set(self.SEED_URLS)

        try:
            index_html = await self.index_fetcher()
        except Exception as exc:
            logger.warning("Hamsterwatch index fetch failed: %s", exc)
            return discovered, str(exc)

        discovered |= set(extract_archive_links(index_html, self.index_url))
        return discovered, None

    def _select_targets(self, discovered: set[str]) -> list[str]:
        """Selects which pages to fetch this tick.

        Always bounded by MAX_PAGES_PER_TICK — including on the very
        first (bootstrap) run, when the archive is empty and
        known_page_urls()/latest_page_url() are naturally empty/None.
        A large historical backlog is simply spread across as many
        ticks as it takes rather than fetched in one uncapped burst;
        this also means a permanently-empty archive (e.g. caused by a
        parsing failure) can never trigger more than
        MAX_PAGES_PER_TICK requests in a single check().
        """

        known = self.archive.known_page_urls()
        new_urls = sorted(discovered - known)
        latest = self.archive.latest_page_url()

        targets = list(new_urls)
        if latest and latest not in targets:
            targets.append(latest)

        return targets[: self.MAX_PAGES_PER_TICK]

    async def _import_pages(
        self, targets: list[str],
    ) -> tuple[list[UpsertOutcome], list[str]]:
        outcomes: list[UpsertOutcome] = []
        failed: list[str] = []

        for url in targets:
            try:
                html = await self.page_fetcher(url)
            except Exception as exc:
                failed.append(url)
                logger.warning("Hamsterwatch page fetch failed for %s: %s", url, exc)
                continue

            sections = parse_recap_sections(url, html)
            if not sections:
                logger.info(
                    "Hamsterwatch page had no recognizable recap sections: %s", url
                )
                continue

            for section in sections:
                outcome = self.archive.upsert(
                    page_url=section.page_url,
                    section_slug=section.section_slug,
                    heading=section.heading,
                    article_date=section.article_date,
                    bb_day=section.bb_day,
                    content=section.content,
                    summary=section.summary,
                )
                outcomes.append(outcome)

        return outcomes, failed

    # ==========================================================
    # Event construction
    # ==========================================================

    @staticmethod
    def _build_event(outcomes: list[UpsertOutcome]) -> ProductionEvent:
        """Builds one consolidated ProductionEvent from newly-significant
        outcomes, so a burst of new content (e.g. after downtime) posts
        as a single Discord message rather than one per day-section.

        metadata["is_new"] distinguishes a section's first appearance
        from a later substantial edit to a section Julie already
        archived (see UpsertOutcome.is_new in database/
        hamsterwatch_archive.py) -- this is what lets
        DiscordOutputRouter._hamsterwatch_title() render "HAMSTERWATCH
        UPDATE" vs "HAMSTERWATCH UPDATED" instead of an identical
        title either way. True if at least one outcome in this batch
        is a brand-new section; a batch of purely re-edited sections
        (no new ones at all) is the only case marked False.
        """

        ordered = sorted(
            outcomes,
            key=lambda o: (
                o.article.bb_day if o.article.bb_day is not None else -1,
                o.article.article_date or "",
            ),
        )
        primary = ordered[-1].article
        is_new = any(outcome.is_new for outcome in ordered)

        if len(ordered) == 1:
            detail_lines = [primary.heading]
            if primary.summary:
                detail_lines.extend(["", primary.summary])
            detail = "\n".join(detail_lines)
        else:
            detail = "\n".join(f"• {outcome.article.heading}" for outcome in ordered)

        metadata = {
            "url": primary.page_url,
            "link": primary.page_url,
            "bb_day": primary.bb_day,
            "article_date": primary.article_date,
            "heading": primary.heading,
            "published": primary.article_date or "",
            "summary": primary.summary,
            "count": len(ordered),
            "is_new": is_new,
        }

        return ProductionEvent(
            source="Hamsterwatch",
            event_type=EventType.TIMELINE,
            title="HAMSTERWATCH UPDATED",
            detail=detail,
            severity=EventSeverity.NOTICE,
            metadata=metadata,
        )
