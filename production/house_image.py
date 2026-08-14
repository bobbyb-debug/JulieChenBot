"""Monitor the JokersUpdates house-status image for changes.

The image filename embeds a Unix timestamp
(bbupdatesblock<timestamp>.png) and rotates whenever Joker's
Updates regenerates it. A hard-coded filename therefore goes stale
and 404s, so this monitor discovers the current filename from the
house-status page and remembers it across restarts.

Two distinct things are tracked, deliberately kept separate:

    URL change      The filename rotated. This is a DISCOVERY
                    event and is NOT, by itself, a content change.
    Content change  The image bytes hash differently than the
                    previously stored hash. Only this emits
                    IMAGE_CHANGED.

Joker's regenerates the file (new filename) more often than the
board itself actually changes, so treating a rotation as a content
change would spam #house-status with identical images.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from config import HOUSE_STATUS_IMAGE, HOUSE_STATUS_PAGE
from database.storage import Storage
from production.events import EventSeverity, EventType, ProductionEvent
from production.monitors import Monitor, MonitorResult, MonitorStatus
from services.logger import ProductionLogger

logger = ProductionLogger.get("HouseImage")

Fetcher = Callable[[], Awaitable[bytes]]
PageFetcher = Callable[[], Awaitable[str]]

# Preferred anchor: Joker's tags the house-status image with a
# dedicated CSS class, so match that <img> tag's src first. This
# avoids picking up an unrelated bbupdatesblock*.png elsewhere on
# the page (archive thumbnails, etc.).
BLOCK_IMG_PATTERN = re.compile(
    r"""<img[^>]*class=["'][^"']*bbhgblockimg[^"']*["'][^>]*?"""
    r"""src=["']([^"']+)["']""",
    re.IGNORECASE,
)

# Fallback: any bbupdatesblock<digits>.png anywhere in the page markup,
# including inside src="..." attributes and inline CSS.
IMAGE_PATTERN = re.compile(
    r"[\w./:-]*bbupdatesblock\d+\.png",
    re.IGNORECASE,
)

_USER_AGENT = "JulieChenBot/1.0"


class HouseImageMonitor(Monitor):
    """Detects changes in the JokersUpdates house-status image."""

    URL = HOUSE_STATUS_IMAGE
    PAGE = HOUSE_STATUS_PAGE
    STORAGE_KEY = "house_image_last_hash"
    URL_STORAGE_KEY = "house_image_last_url"

    def __init__(
        self,
        storage: Storage | None = None,
        fetcher: Fetcher | None = None,
        *,
        image_url: str | None = None,
        storage_key: str | None = None,
        page_url: str | None = None,
        page_fetcher: PageFetcher | None = None,
    ) -> None:
        super().__init__()
        self.storage = storage or Storage()
        self.storage_key = storage_key or self.STORAGE_KEY
        self.url_storage_key = self.URL_STORAGE_KEY
        self.page_url = page_url or self.PAGE

        # An explicitly supplied image_url pins the monitor to that URL
        # and disables discovery. This keeps existing tests, which inject
        # a fixed URL plus a fetcher, working exactly as before.
        self._pinned_url = image_url
        self.image_url = (
            image_url
            or self.storage.get(self.url_storage_key, "")
            or self.URL
        )

        self.fetcher = fetcher
        self.page_fetcher = page_fetcher

        logger.info("House image monitor initialized: %s", self.image_url)

    # ==========================================================
    # Network
    # ==========================================================

    def _fetch_sync(self, url: str) -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
            },
        )
        with urlopen(request, timeout=20) as response:
            return response.read()

    async def _fetch_image(self, url: str) -> bytes:
        """Downloads image bytes, honouring an injected fetcher."""

        if self.fetcher is not None:
            return await self.fetcher()

        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_page_sync(self) -> str:
        request = Request(
            self.page_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", errors="replace")

    async def _fetch_page(self) -> str:
        if self.page_fetcher is not None:
            return await self.page_fetcher()

        return await asyncio.to_thread(self._fetch_page_sync)

    # ==========================================================
    # Discovery
    # ==========================================================

    async def discover(self) -> str | None:
        """Finds the current bbupdatesblock*.png URL from the page.

        Returns the absolute image URL, or None when discovery fails.
        """

        try:
            markup = await self._fetch_page()
        except Exception as exc:
            logger.warning(
                "House image discovery failed to load %s: %s",
                self.page_url,
                exc,
            )
            return None

        tagged = BLOCK_IMG_PATTERN.search(markup or "")

        if tagged is not None:
            candidate = tagged.group(1)
        else:
            match = IMAGE_PATTERN.search(markup or "")

            if match is None:
                logger.warning(
                    "House image discovery found no bbupdatesblock image on %s.",
                    self.page_url,
                )
                return None

            candidate = match.group(0)

        discovered = urljoin(self.page_url, candidate)

        logger.info("Discovered house image URL: %s", discovered)

        return discovered

    def _remember_url(self, url: str) -> None:
        if url != self.image_url:
            logger.info(
                "House image URL changed: %s -> %s",
                self.image_url,
                url,
            )

        self.image_url = url
        self.storage.set(self.url_storage_key, url)

    # ==========================================================
    # Acquisition
    # ==========================================================

    async def _acquire(self) -> tuple[bytes, bool]:
        """Returns (image_bytes, url_changed).

        Discovers the current filename from the house-status page on
        every check() -- not only when the remembered URL fails to
        fetch. Joker's does not always 404 the previous file
        immediately after rotating (see module docstring), so a
        remembered URL that still returns bytes successfully is not
        proof nothing changed: it can silently leave Julie hashing a
        stale image forever. When discovery finds a URL different
        from the remembered one, that URL's bytes are fetched and
        used directly. When discovery agrees with the remembered URL,
        or the page itself is unreachable, behavior falls through
        unchanged from before: fetch the remembered URL, and only
        rediscover-and-retry if that fetch itself fails.
        """

        original_url = self.image_url

        if self._pinned_url is None:
            discovered = await self.discover()

            if discovered is not None and discovered != self.image_url:
                image = await self._fetch_image(discovered)

                if not image:
                    raise RuntimeError(
                        "Rediscovered house image returned no bytes."
                    )

                self._remember_url(discovered)

                return image, True

        try:
            image = await self._fetch_image(self.image_url)
            if image:
                return image, False
            raise ValueError("empty image response")

        except Exception as exc:
            if self._pinned_url is not None:
                raise

            logger.warning(
                "House image fetch failed for %s (%s). Rediscovering.",
                self.image_url,
                exc,
            )

        discovered = await self.discover()

        if discovered is None:
            raise RuntimeError(
                "House image unavailable and rediscovery failed."
            )

        image = await self._fetch_image(discovered)

        if not image:
            raise RuntimeError(
                "Rediscovered house image returned no bytes."
            )

        self._remember_url(discovered)

        return image, discovered != original_url

    # ==========================================================
    # Check
    # ==========================================================

    async def check(self) -> MonitorResult:
        try:
            image, url_changed = await self._acquire()

            digest = hashlib.sha256(image).hexdigest()
            previous = self.storage.get(self.storage_key, "")

            metadata = {
                "url": self.image_url,
                "url_changed": url_changed,
                "hash": digest,
            }

            if not previous:
                self.storage.set(self.storage_key, digest)
                logger.info(
                    "Created first house image snapshot: %s (hash %s)",
                    self.image_url,
                    digest[:12],
                )
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail="Initial house image snapshot captured.",
                    metadata=metadata,
                )

            if digest == previous:
                # A rotated filename with identical bytes is a discovery
                # event only — deliberately NOT an announcement.
                logger.info(
                    "House image unchanged (url_changed=%s, hash %s).",
                    url_changed,
                    digest[:12],
                )
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail=(
                        "House image URL rotated; content unchanged."
                        if url_changed
                        else "House image unchanged."
                    ),
                    metadata=metadata,
                )

            self.storage.set(self.storage_key, digest)
            event = ProductionEvent(
                source="HouseImage",
                event_type=EventType.IMAGE_CHANGED,
                title="HOUSE STATUS IMAGE UPDATED",
                detail="JokersUpdates house-status image changed after episode air.",
                severity=EventSeverity.NOTICE,
                metadata=metadata,
            )

            logger.info(
                "House image changed: %s (hash %s)",
                self.image_url,
                digest[:12],
            )
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.HEALTHY,
                changed=True,
                detail="House image changed.",
                events=[event],
                metadata=metadata,
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
