"""Monitor Hamsterwatch for meaningful page-content changes."""

from __future__ import annotations

import asyncio
import hashlib
import re
from html import unescape
from urllib.request import Request, urlopen

from database.storage import Storage
from production.events import EventSeverity, EventType, ProductionEvent
from production.monitors import Monitor, MonitorResult, MonitorStatus
from services.logger import ProductionLogger

logger = ProductionLogger.get("Hamsterwatch")


class HamsterwatchMonitor(Monitor):
    """Detects changes to the public Hamsterwatch Big Brother page."""

    URL = "https://hamsterwatch.com/"
    STORAGE_KEY = "hamsterwatch_last_hash"

    def __init__(self, storage: Storage | None = None) -> None:
        super().__init__()
        self.storage = storage or Storage()
        logger.info("Hamsterwatch monitor initialized.")

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

    async def fetch(self) -> str:
        """Fetch the page without blocking the production event loop."""
        return await asyncio.to_thread(self._fetch_sync, self.URL)

    async def check(self) -> MonitorResult:
        try:
            html = await self.fetch()
            visible = self.normalize(html)

            if not visible:
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.DEGRADED,
                    changed=False,
                    detail="Hamsterwatch returned no visible page content.",
                    metadata={"url": self.URL},
                )

            digest = hashlib.sha256(visible.encode("utf-8")).hexdigest()
            previous = self.storage.get(self.STORAGE_KEY, "")

            if not previous:
                self.storage.set(self.STORAGE_KEY, digest)
                logger.info("Created first Hamsterwatch snapshot.")
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail="Initial Hamsterwatch snapshot captured.",
                    metadata={"url": self.URL},
                )

            if digest == previous:
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail="Hamsterwatch unchanged.",
                    metadata={"url": self.URL},
                )

            self.storage.set(self.STORAGE_KEY, digest)
            event = ProductionEvent(
                source="Hamsterwatch",
                event_type=EventType.TIMELINE,
                title="🐹 HAMSTERWATCH UPDATED",
                detail="Hamsterwatch has published new or changed Big Brother information.",
                severity=EventSeverity.NOTICE,
                metadata={"url": self.URL},
            )

            logger.info("Hamsterwatch page changed.")
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.HEALTHY,
                changed=True,
                detail="Hamsterwatch page changed.",
                events=[event],
                metadata={"url": self.URL},
            )

        except Exception as exc:
            logger.exception("Hamsterwatch check failed.")
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.DEGRADED,
                changed=False,
                detail=f"Hamsterwatch check failed: {exc}",
                metadata={"url": self.URL},
            )
