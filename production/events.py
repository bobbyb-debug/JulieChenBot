"""
Julie ChenBot Production Events
===============================

Defines Julie ChenBot's internal production event model.

Every monitor communicates with the Production Engine by
producing ProductionEvent objects.

The engine never needs to know where an event came from—
only what happened.

This module intentionally contains no business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ==========================================================
# Event Severity
# ==========================================================


class EventSeverity(str, Enum):
    """
    Represents the importance of a production event.
    """

    DEBUG = "debug"

    INFO = "info"

    NOTICE = "notice"

    WARNING = "warning"

    IMPORTANT = "important"

    CRITICAL = "critical"


# ==========================================================
# Event Type
# ==========================================================


class EventType(str, Enum):
    """
    Standard production events understood by Julie.
    """

    UNKNOWN = "unknown"

    RSS_UPDATE = "rss_update"

    IMAGE_CHANGED = "image_changed"

    HOUSE_STATUS_CHANGED = "house_status_changed"

    FEEDS_UP = "feeds_up"

    FEEDS_DOWN = "feeds_down"

    HOH_CHANGED = "hoh_changed"

    NOMINATIONS_CHANGED = "nominations_changed"

    POV_CHANGED = "pov_changed"

    HAVE_NOTS_CHANGED = "have_nots_changed"

    COMPETITION_STARTED = "competition_started"

    COMPETITION_FINISHED = "competition_finished"

    COMPETITION_CHANGED = "competition_changed"

    COMPETITION_WINNER = "competition_winner"

    EVICTION = "eviction"

    DOUBLE_EVICTION = "double_eviction"

    API_STATUS = "api_status"

    TIMELINE = "timeline"

    AI_SUMMARY = "ai_summary"

    SYSTEM = "system"


# ==========================================================
# Production Event
# ==========================================================


@dataclass(slots=True)
class ProductionEvent:
    """
    Represents one meaningful production event.

    Events are produced by monitors and consumed by the
    Production Engine.
    """

    source: str

    event_type: EventType

    title: str

    detail: str

    severity: EventSeverity = EventSeverity.INFO

    created_at: datetime = field(
        default_factory=datetime.utcnow
    )

    metadata: dict[str, Any] = field(
        default_factory=dict
    )

    announced: bool = False

    # ======================================================
    # Helpers
    # ======================================================

    @property
    def age_seconds(self) -> float:
        """
        Returns the age of the event.
        """

        return (
            datetime.utcnow() - self.created_at
        ).total_seconds()

    @property
    def is_critical(self) -> bool:
        """
        Returns True for important production events.
        """

        return self.severity in {

            EventSeverity.IMPORTANT,

            EventSeverity.CRITICAL,

        }

    def mark_announced(self) -> None:
        """
        Marks this event as announced.
        """

        self.announced = True

    # ======================================================
    # Serialization
    # ======================================================

    def to_dict(self) -> dict:
        """
        Converts the event into a JSON-safe dictionary.
        """

        return {

            "source": self.source,

            "event_type": self.event_type.value,

            "title": self.title,

            "detail": self.detail,

            "severity": self.severity.value,

            "created_at": self.created_at.isoformat(),

            "metadata": self.metadata,

            "announced": self.announced,

        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
    ) -> "ProductionEvent":
        """
        Creates an event from a dictionary.
        """

        return cls(

            source=data["source"],

            event_type=EventType(
                data["event_type"]
            ),

            title=data["title"],

            detail=data["detail"],

            severity=EventSeverity(
                data["severity"]
            ),

            created_at=datetime.fromisoformat(
                data["created_at"]
            ),

            metadata=data.get(
                "metadata",
                {},
            ),

            announced=data.get(
                "announced",
                False,
            ),
        )

    # ======================================================
    # Display
    # ======================================================

    def __str__(self) -> str:

        return (
            f"[{self.severity.value.upper()}] "
            f"{self.source}: "
            f"{self.title}"
        )

    def __repr__(self) -> str:

        return (
            "ProductionEvent("
            f"{self.event_type.value}, "
            f"{self.source!r})"
        )