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
from datetime import UTC, datetime
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

    All event timestamps are stored as timezone-aware UTC.
    This prevents Discord and other output adapters from
    interpreting a UTC timestamp as local time.
    """

    source: str

    event_type: EventType

    title: str

    detail: str

    severity: EventSeverity = EventSeverity.INFO

    created_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )

    metadata: dict[str, Any] = field(
        default_factory=dict
    )

    announced: bool = False

    # Names of destinations (see DiscordOutputRouter._CHANNEL_NAMES)
    # that have already successfully received this event. A single
    # event can route to multiple Discord channels; when one succeeds
    # and another fails, engine.announce() requeues this exact same
    # ProductionEvent instance for retry (see production/engine.py),
    # so tracking delivery here -- rather than anywhere in the
    # output layer -- means a retry naturally skips destinations that
    # already got it, without a separate lookup table to keep in
    # sync.
    delivered_to: set[str] = field(
        default_factory=set
    )

    def __post_init__(self) -> None:
        """Normalize event timestamps to timezone-aware UTC."""

        if self.created_at.tzinfo is None:
            # Backward compatibility for events created by older code
            # with naive UTC timestamps.
            self.created_at = self.created_at.replace(tzinfo=UTC)
        else:
            self.created_at = self.created_at.astimezone(UTC)

    # ======================================================
    # Helpers
    # ======================================================

    @property
    def age_seconds(self) -> float:
        """
        Returns the age of the event.
        """

        return (
            datetime.now(UTC) - self.created_at
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

            # Sorted for deterministic JSON output; reconstructed as a
            # set on the way back in. See from_dict() below.
            "delivered_to": sorted(self.delivered_to),

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

            delivered_to=set(
                data.get("delivered_to", [])
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
