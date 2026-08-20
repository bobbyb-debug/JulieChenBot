"""Monitor the official @JokersBBUpdates X account for new posts."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from database.storage import Storage
from production.events import EventSeverity, EventType, ProductionEvent
from production.monitors import Monitor, MonitorResult, MonitorStatus
from services.logger import ProductionLogger

logger = ProductionLogger.get("XUpdates")


class XUpdatesMonitor(Monitor):
    """Polls @JokersBBUpdates through the official X API v2."""

    USERNAME = "JokersBBUpdates"
    API_BASE = "https://api.x.com/2"
    USER_ID_KEY = "x_jokers_updates_user_id"
    LAST_POST_ID_KEY = "x_jokers_updates_last_post_id"

    def __init__(self, storage: Storage | None = None) -> None:
        super().__init__()
        self.storage = storage or Storage()
        self.bearer_token = os.getenv("X_BEARER_TOKEN", "").strip()
        if not self.bearer_token:
            self.enabled = False
            logger.info("X Updates monitor disabled: X_BEARER_TOKEN is not configured.")
        else:
            logger.info("X Updates monitor initialized for @%s.", self.USERNAME)

    def _request_json(self, path: str, params: dict[str, str] | None = None) -> dict:
        url = f"{self.API_BASE}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": "JulieChenBot/1.0",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get_user_id(self) -> str:
        cached = self.storage.get(self.USER_ID_KEY, "")
        if cached:
            return str(cached)

        payload = self._request_json(
            f"/users/by/username/{self.USERNAME}",
            {"user.fields": "id,username"},
        )
        user = payload.get("data") or {}
        user_id = str(user.get("id", ""))
        if not user_id:
            raise RuntimeError(f"X API returned no user ID for @{self.USERNAME}.")

        self.storage.set(self.USER_ID_KEY, user_id)
        return user_id

    def _get_latest_posts(self, user_id: str) -> list[dict]:
        payload = self._request_json(
            f"/users/{user_id}/tweets",
            {
                "max_results": "10",
                "tweet.fields": "created_at,public_metrics",
                "exclude": "retweets,replies",
            },
        )
        return payload.get("data") or []

    async def check(self) -> MonitorResult:
        try:
            user_id = self._get_user_id()
            posts = self._get_latest_posts(user_id)

            if not posts:
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail=f"No posts returned for @{self.USERNAME}.",
                    metadata={"username": self.USERNAME},
                )

            # X returns posts newest-first. The stored ID is the watermark.
            previous_id = str(self.storage.get(self.LAST_POST_ID_KEY, ""))
            newest_id = str(posts[0].get("id", ""))

            if not previous_id:
                self.storage.set(self.LAST_POST_ID_KEY, newest_id)
                logger.info("Created first X snapshot for @%s.", self.USERNAME)
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail=f"Initial X snapshot captured for @{self.USERNAME}.",
                    metadata={"username": self.USERNAME},
                )

            # The API response is newest-first. If the watermark is present,
            # everything before it is genuinely new. Preserve chronological
            # publication order when creating events.
            watermark_index = next(
                (
                    index
                    for index, post in enumerate(posts)
                    if str(post.get("id", "")) == previous_id
                ),
                None,
            )

            if watermark_index is None:
                # The watermark has fallen outside the requested API window.
                # Advance the watermark to the newest returned post and wait
                # for the next poll rather than replaying an arbitrary batch of
                # older content.
                if newest_id != previous_id:
                    self.storage.set(self.LAST_POST_ID_KEY, newest_id)
                    return MonitorResult(
                        monitor=self.name,
                        status=MonitorStatus.HEALTHY,
                        changed=False,
                        detail=f"X watermark advanced for @{self.USERNAME}; older posts were not replayed.",
                        metadata={"username": self.USERNAME},
                    )

                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail=f"No new posts from @{self.USERNAME}.",
                    metadata={"username": self.USERNAME},
                )

            new_posts = list(reversed(posts[:watermark_index]))
            if not new_posts:
                return MonitorResult(
                    monitor=self.name,
                    status=MonitorStatus.HEALTHY,
                    changed=False,
                    detail=f"No new posts from @{self.USERNAME}.",
                    metadata={"username": self.USERNAME},
                )

            events: list[ProductionEvent] = []
            for post in new_posts:
                post_id = str(post.get("id", ""))
                text = str(post.get("text", "")).strip()
                created_at = post.get("created_at")
                events.append(
                    ProductionEvent(
                        source="JokersBBUpdates",
                        event_type=EventType.RSS_UPDATE,
                        title="🐦 JOKER'S UPDATES ON X",
                        detail=text,
                        severity=EventSeverity.NOTICE,
                        metadata={
                            "link": f"https://x.com/{self.USERNAME}/status/{post_id}",
                            "post_id": post_id,
                            "published": created_at or "",
                            "username": self.USERNAME,
                        },
                    )
                )

            self.storage.set(self.LAST_POST_ID_KEY, newest_id)
            logger.info("Detected %d new X post(s) from @%s.", len(events), self.USERNAME)
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.HEALTHY,
                changed=True,
                detail=f"Detected {len(events)} new post(s) from @{self.USERNAME}.",
                events=events,
                metadata={"username": self.USERNAME},
            )

        except HTTPError as exc:
            if exc.code == 401:
                detail = "X API authentication failed (401). Check X_BEARER_TOKEN."
                logger.error("%s", detail)
            else:
                detail = f"X Updates check failed: {exc}"
                logger.exception("X Updates API request failed.")
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.DEGRADED,
                changed=False,
                detail=detail,
                metadata={"username": self.USERNAME, "http_status": exc.code},
            )
        except URLError as exc:
            logger.exception("X Updates API request failed.")
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.DEGRADED,
                changed=False,
                detail=f"X Updates check failed: {exc}",
                metadata={"username": self.USERNAME},
            )
        except Exception as exc:
            logger.exception("X Updates check failed.")
            return MonitorResult(
                monitor=self.name,
                status=MonitorStatus.DEGRADED,
                changed=False,
                detail=f"X Updates check failed: {exc}",
                metadata={"username": self.USERNAME},
            )
