"""
Julie ChenBot Monitor Framework
===============================

Defines the common monitoring framework used throughout
Julie ChenBot.

Every monitor inherits from Monitor and reports a
MonitorResult after each check.

The ProductionWatcher coordinates monitors.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from production.events import ProductionEvent


# ==========================================================
# Monitor Status
# ==========================================================


class MonitorStatus(str, Enum):
    """
    Operational state of a monitor.
    """

    HEALTHY = "healthy"

    DEGRADED = "degraded"

    UNHEALTHY = "unhealthy"

    DISABLED = "disabled"


# ==========================================================
# Monitor Result
# ==========================================================


@dataclass(slots=True)
class MonitorResult:
    """
    Result produced by every monitor execution.
    """

    monitor: str

    status: MonitorStatus

    changed: bool

    detail: str

    checked_at: datetime = field(
        default_factory=datetime.utcnow
    )

    duration_ms: float = 0.0

    events: list[ProductionEvent] = field(
        default_factory=list
    )

    metadata: dict = field(
        default_factory=dict
    )

    # ======================================================
    # Helpers
    # ======================================================

    @property
    def healthy(self) -> bool:

        return self.status == MonitorStatus.HEALTHY

    @property
    def event_count(self) -> int:

        return len(self.events)

    def to_dict(self) -> dict:

        return {

            "monitor": self.monitor,

            "status": self.status.value,

            "changed": self.changed,

            "detail": self.detail,

            "checked_at": self.checked_at.isoformat(),

            "duration_ms": self.duration_ms,

            "event_count": self.event_count,

            "metadata": self.metadata,
        }


# ==========================================================
# Base Monitor
# ==========================================================


class Monitor(ABC):
    """
    Base class for every Julie ChenBot monitor.

    Every monitor owns exactly one responsibility.

    Examples

        RSSMonitor

        ImageMonitor

        HouseStatusMonitor

        CompetitionMonitor

        TimelineMonitor

        ApiMonitor

        AIMonitor
    """

    def __init__(self) -> None:

        self.enabled = True

        self.last_result: MonitorResult | None = None

    # ======================================================
    # Properties
    # ======================================================

    @property
    def name(self) -> str:

        return self.__class__.__name__

    # ======================================================
    # Public API
    # ======================================================

    async def run(self) -> MonitorResult:
        """
        Executes this monitor.

        The ProductionWatcher calls this method.
        """

        if not self.enabled:

            result = MonitorResult(

                monitor=self.name,

                status=MonitorStatus.DISABLED,

                changed=False,

                detail="Monitor disabled.",
            )

            self.last_result = result

            return result

        result = await self.check()

        self.last_result = result

        return result

    # ======================================================
    # Required
    # ======================================================

    @abstractmethod
    async def check(self) -> MonitorResult:
        """
        Checks this production source.

        Returns
        -------
        MonitorResult
        """

    # ======================================================
    # Utilities
    # ======================================================

    def disable(self) -> None:

        self.enabled = False

    def enable(self) -> None:

        self.enabled = True