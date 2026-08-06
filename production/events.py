"""
Julie ChenBot Production Events
===============================

Defines every event Julie can announce.

Every production system (RSS, image watcher,
competitions, ceremonies, etc.) creates one of these.

Discord only has to announce ProductionEvents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ==========================================================
# Event Types
# ==========================================================


class EventType(str, Enum):
    """
    Every event Julie can recognize.
    """

    RSS_UPDATE = "rss_update"

    FEEDS_DOWN = "feeds_down"
    FEEDS_RETURNED = "feeds_returned"

    COMPETITION_STARTED = "competition_started"
    COMPETITION_FINISHED = "competition_finished"

    HOH_CROWNED = "hoh_crowned"

    NOMINATIONS = "nominations"

    POV_PLAYED = "pov_played"
    POV_USED = "pov_used"

    EVICTION = "eviction"

    TWIST = "twist"

    MANUAL = "manual"

    SYSTEM = "system"


# ==========================================================
# Production Event
# ==========================================================


@dataclass(slots=True)
class ProductionEvent:
    """
    Represents a single production event.

    Every watcher produces these.

    Every Discord announcement consumes these.
    """

    event_type: EventType

    title: str

    message: str

    source: str

    timestamp: datetime = field(
        default_factory=datetime.now
    )

    url: str = ""

    guid: str = ""

    image_hash: str = ""

    metadata: dict[str, Any] = field(
        default_factory=dict
    )

    # ------------------------------------------------------

    @property
    def is_rss(self) -> bool:

        return self.event_type == EventType.RSS_UPDATE

    @property
    def is_house_status(self) -> bool:

        return self.event_type in (
            EventType.FEEDS_DOWN,
            EventType.FEEDS_RETURNED,
        )

    @property
    def is_competition(self) -> bool:

        return self.event_type in (
            EventType.COMPETITION_STARTED,
            EventType.COMPETITION_FINISHED,
        )

    # ------------------------------------------------------

    def to_dict(self) -> dict:

        return {

            "event_type": self.event_type.value,

            "title": self.title,

            "message": self.message,

            "source": self.source,

            "timestamp": self.timestamp.isoformat(),

            "url": self.url,

            "guid": self.guid,

            "image_hash": self.image_hash,

            "metadata": self.metadata,
        }

    # ------------------------------------------------------

    @classmethod
    def from_dict(
        cls,
        data: dict,
    ) -> "ProductionEvent":

        return cls(

            event_type=EventType(
                data["event_type"]
            ),

            title=data.get(
                "title",
                "",
            ),

            message=data.get(
                "message",
                "",
            ),

            source=data.get(
                "source",
                "",
            ),

            timestamp=datetime.fromisoformat(
                data["timestamp"]
            ),

            url=data.get(
                "url",
                "",
            ),

            guid=data.get(
                "guid",
                "",
            ),

            image_hash=data.get(
                "image_hash",
                "",
            ),

            metadata=data.get(
                "metadata",
                {},
            ),
        )

    # ------------------------------------------------------

    def __str__(self) -> str:

        return (
            f"[{self.event_type.value}] "
            f"{self.title}"
        )


# ==========================================================
# Factory Helpers
# ==========================================================


def rss_event(
    *,
    title: str,
    message: str,
    guid: str,
    url: str,
) -> ProductionEvent:
    """
    Create a ProductionEvent from a Jokers RSS item.
    """

    return ProductionEvent(

        event_type=EventType.RSS_UPDATE,

        title=title,

        message=message,

        source="Jokers RSS",

        guid=guid,

        url=url,
    )


def feeds_down_event() -> ProductionEvent:
    """
    Cameras have gone down.
    """

    return ProductionEvent(

        event_type=EventType.FEEDS_DOWN,

        title="Feeds Down",

        message="The Big Brother live feeds have gone offline.",

        source="House Status",
    )


def feeds_returned_event() -> ProductionEvent:
    """
    Cameras have returned.
    """

    return ProductionEvent(

        event_type=EventType.FEEDS_RETURNED,

        title="Feeds Returned",

        message="The Big Brother live feeds are back.",

        source="House Status",
    )


def manual_event(
    title: str,
    message: str,
) -> ProductionEvent:
    """
    Event created manually.
    """

    return ProductionEvent(

        event_type=EventType.MANUAL,

        title=title,

        message=message,

        source="Manual",
    )


def system_event(
    title: str,
    message: str,
) -> ProductionEvent:
    """
    Internal Julie system event.
    """

    return ProductionEvent(

        event_type=EventType.SYSTEM,

        title=title,

        message=message,

        source="System",
    )