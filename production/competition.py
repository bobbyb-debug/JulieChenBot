"""
Julie ChenBot Competition Monitor
=================================

Tracks the current Big Brother competition.

The CompetitionMonitor evaluates parsed production data and
reports meaningful competition changes.

Responsibilities
----------------
• Head of Household competitions
• Power of Veto competitions
• AI Arena competitions
• Battle Back competitions
• Luxury competitions

This monitor does not download RSS feeds or images.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from production.events import (
    EventSeverity,
    EventType,
    ProductionEvent,
)

from production.monitors import (
    Monitor,
    MonitorResult,
    MonitorStatus,
)

from services.logger import ProductionLogger

logger = ProductionLogger.get("Competition")


# ==========================================================
# Competition Types
# ==========================================================


class CompetitionType(str, Enum):

    NONE = "None"

    HOH = "Head of Household"

    POV = "Power of Veto"

    AI_ARENA = "AI Arena"

    BATTLE_BACK = "Battle Back"

    LUXURY = "Luxury"

    UNKNOWN = "Unknown"


# ==========================================================
# Competition State
# ==========================================================


@dataclass(slots=True)
class CompetitionState:
    """
    Represents the current competition.
    """

    competition: CompetitionType = CompetitionType.NONE

    active: bool = False

    winner: str = ""

    started_at: Optional[datetime] = None

    ended_at: Optional[datetime] = None


# ==========================================================
# Competition Monitor
# ==========================================================


class CompetitionMonitor(Monitor):
    """
    Monitors Big Brother competitions.
    """

    def __init__(self) -> None:

        super().__init__()

        self.current = CompetitionState()

        self.pending_state: Optional[
            CompetitionState
        ] = None

        logger.info(
            "Competition monitor initialized."
        )

    # ======================================================
    # Feed New State
    # ======================================================

    def update(
        self,
        state: CompetitionState,
    ) -> None:
        """
        Supplies a newly parsed competition state.
        """

        self.pending_state = state

    # ======================================================
    # Monitor
    # ======================================================

    async def check(
        self,
    ) -> MonitorResult:

        if self.pending_state is None:

            return MonitorResult(

                monitor=self.name,

                status=MonitorStatus.HEALTHY,

                changed=False,

                detail="No competition update.",

            )

        new = self.pending_state

        self.pending_state = None

        #
        # First observation
        #

        if self.current.competition == CompetitionType.NONE:

            self.current = new

            return MonitorResult(

                monitor=self.name,

                status=MonitorStatus.HEALTHY,

                changed=False,

                detail="Initial competition state captured.",

            )

        #
        # No change
        #

        if new == self.current:

            return MonitorResult(

                monitor=self.name,

                status=MonitorStatus.HEALTHY,

                changed=False,

                detail="Competition unchanged.",

            )

        events: list[
            ProductionEvent
        ] = []

        #
        # Competition Started
        #

        if (
            not self.current.active
            and new.active
        ):

            events.append(

                ProductionEvent(

                    source=self.name,

                    event_type=EventType.COMPETITION_STARTED,

                    title=f"{new.competition.value} Started",

                    detail="Competition has begun.",

                    severity=EventSeverity.IMPORTANT,

                )

            )

        #
        # Competition Finished
        #

        if (
            self.current.active
            and not new.active
        ):

            events.append(

                ProductionEvent(

                    source=self.name,

                    event_type=EventType.COMPETITION_FINISHED,

                    title=f"{self.current.competition.value} Finished",

                    detail=f"Winner: {new.winner}",

                    severity=EventSeverity.IMPORTANT,

                )

            )

        #
        # Winner Changed
        #

        if (
            new.winner
            and new.winner != self.current.winner
        ):

            events.append(

                ProductionEvent(

                    source=self.name,

                    event_type=EventType.COMPETITION_WINNER,

                    title="Competition Winner",

                    detail=new.winner,

                )

            )

        #
        # Competition Type Changed
        #

        if new.competition != self.current.competition:

            events.append(

                ProductionEvent(

                    source=self.name,

                    event_type=EventType.COMPETITION_CHANGED,

                    title="Competition Changed",

                    detail=new.competition.value,

                )

            )

        self.current = new

        logger.info(
            "Competition state changed."
        )

        return MonitorResult(

            monitor=self.name,

            status=MonitorStatus.HEALTHY,

            changed=True,

            detail="Competition updated.",

            events=events,

        )

    # ======================================================
    # Snapshot
    # ======================================================

    def snapshot(self) -> dict:

        return {

            "competition":
                self.current.competition.value,

            "active":
                self.current.active,

            "winner":
                self.current.winner,

            "started_at": (
                self.current.started_at.isoformat()
                if self.current.started_at
                else None
            ),

            "ended_at": (
                self.current.ended_at.isoformat()
                if self.current.ended_at
                else None
            ),
        }

    # ======================================================
    # Properties
    # ======================================================

    @property
    def active(self) -> bool:
        return self.current.active

    @property
    def winner(self) -> str:
        return self.current.winner

    @property
    def competition(self) -> CompetitionType:
        return self.current.competition