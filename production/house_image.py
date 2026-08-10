"""Monitor the JokersUpdates house-status image for changes."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from urllib.request import Request, urlopen

from database.storage import Storage
from production.events import EventSeverity, EventType, ProductionEvent
from production.monitors import Monitor, MonitorResult, MonitorStatus
from services.logger import ProductionLogger

logger = ProductionLogger.get("HouseImage")

Fetcher = Callable[[], Awaitable[bytes]]


class HouseImageMonitor(Monitor):
    """Detects changes in the JokersUpdates house-status image."""

    URL = "http://www.jokersupdates.com/ubbthreads/images/headers/bigbrother/hg/bbupdatesblock1786231774.png"
    STORAGE_KEY = "house_image_last_hash"
    CHANNEL_ID_KEY = "house_updates_channel_id"

    def __init__(
        self,
        storage: Storage | None = None,
        fetcher: Fetcher | None = None,
        *,
        image_url: str | None = None,
        storage_key: str | None = None,
    ) -> None:
        super().__init__()
        self.storage = storage or Storage()
        self.image_url = image_url or self.URL
        self.storage_key = storage_key or self.STORAGE_KEY
        self.fetcher = fetcher or self._fetch
        logger.info("House image monitor initialized: %s", self.image_url)

    @staticmethod
    def _fetch_sync(url: str) -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": "JulieChenBot/1.0",
                "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
            },
        )
        with urlopen(request, timeout=20) as response:
            return response.read()

    async def _fetch(self) -> bytes:
        return await asyncio.to_thread(self._fetch_sync, self.image_url)

    async def check(self) -> MonitorResult:
        try:
            image = await self.fetcher()
            if not image:
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.DEGRADED,
                    changed=False,
                    detail="House image returned no bytes.",
                    metadata={"url": self.image_url},
                )

            digest = hashlib.sha256(image).hexdigest()
            previous = self.storage.get(self.storage_key, "")

            if not previous:
                self.storage.set(self.storage_key, digest)
                logger.info("Created first house image snapshot: %s", self.image_url)
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail="Initial house image snapshot captured.",
                    metadata={"url": self.image_url},
                )

            if digest == previous:
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail="House image unchanged.",
                    metadata={"url": self.image_url},
                )

            self.storage.set(self.storage_key, digest)
            event = ProductionEvent(
                source="HouseImage",
                event_type=EventType.IMAGE_CHANGED,
                title="HOUSE STATUS IMAGE UPDATED",
                detail="JokersUpdates house-status image changed after episode air.",
                severity=EventSeverity.NOTICE,
                metadata={"url": self.image_url},
            )

            logger.info("House image changed: %s", self.image_url)
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.HEALTHY,
                changed=True,
                detail="House image changed.",
                events=[event],
                metadata={"url": self.image_url},
            )

        except Exception as exc:
            logger.exception("House image check failed.")
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.DEGRADED,
                changed=False,
                detail=f"House image check failed: {exc}",
                metadata={"url": self.image_url},
            )
