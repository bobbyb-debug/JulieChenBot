"""Monitor JokersUpdates quickview pages for meaningful change."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from html import unescape
from urllib.request import Request, urlopen

from database.storage import Storage
from production.events import EventSeverity, EventType, ProductionEvent
from production.monitors import Monitor, MonitorResult, MonitorStatus
from services.logger import ProductionLogger

logger = ProductionLogger.get("Quickview")

Fetcher = Callable[[], Awaitable[str]]


class QuickviewMonitor(Monitor):
    """Detects changes on one public JokersUpdates quickview page."""

    URL = "http://forums.jokersupdates.com/ubbthreads/quickview/updates.php"
    STORAGE_KEY = "quickview_updates_last_hash"
    TITLE = "JOKERSUPDATES QUICKVIEW UPDATED"
    SOURCE = "JokersUpdates Quickview"

    def __init__(
        self,
        storage: Storage | None = None,
        fetcher: Fetcher | None = None,
        *,
        url: str | None = None,
        storage_key: str | None = None,
        source: str | None = None,
    ) -> None:
        super().__init__()
        self.storage = storage or Storage()
        self.url = url or self.URL
        self.storage_key = storage_key or self.STORAGE_KEY
        self.source = source or self.SOURCE
        self.fetcher = fetcher or self._fetch
        logger.info("Quickview monitor initialized: %s", self.url)

    @staticmethod
    def normalize(html: str) -> str:
        """Return stable visible text suitable for hashing."""
        html = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
        html = re.sub(r"(?is)<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", unescape(html)).strip()

    @staticmethod
    def _fetch_sync(url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": "JulieChenBot/1.0",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", errors="replace")

    async def _fetch(self) -> str:
        return await asyncio.to_thread(self._fetch_sync, self.url)

    async def check(self) -> MonitorResult:
        try:
            html = await self.fetcher()
            visible = self.normalize(html)

            if not visible:
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.DEGRADED,
                    changed=False,
                    detail="Quickview returned no visible page content.",
                    metadata={"url": self.url},
                )

            digest = hashlib.sha256(visible.encode("utf-8")).hexdigest()
            previous = self.storage.get(self.storage_key, "")

            if not previous:
                self.storage.set(self.storage_key, digest)
                logger.info("Created first Quickview snapshot: %s", self.url)
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail="Initial Quickview snapshot captured.",
                    metadata={"url": self.url},
                )

            if digest == previous:
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail="Quickview unchanged.",
                    metadata={"url": self.url},
                )

            self.storage.set(self.storage_key, digest)
            event = ProductionEvent(
                source=self.source,
                event_type=EventType.TIMELINE,
                title=self.TITLE,
                detail="JokersUpdates quickview content changed.",
                severity=EventSeverity.NOTICE,
                metadata={"url": self.url},
            )

            logger.info("Quickview page changed: %s", self.url)
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.HEALTHY,
                changed=True,
                detail="Quickview page changed.",
                events=[event],
                metadata={"url": self.url},
            )

        except Exception as exc:
            logger.exception("Quickview check failed.")
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.DEGRADED,
                changed=False,
                detail=f"Quickview check failed: {exc}",
                metadata={"url": self.url},
            )


class BBUpdatesMonitor(QuickviewMonitor):
    """Monitors the legacy JokersUpdates BB updates quickview page."""

    URL = "http://forums.jokersupdates.com/ubbthreads/quickview/bbupdates.php"
    STORAGE_KEY = "bbupdates_last_hash"
    TITLE = "JOKERSUPDATES BB UPDATES UPDATED"
    SOURCE = "JokersUpdates BB Updates"
