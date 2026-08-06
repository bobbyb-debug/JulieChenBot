"""
Julie ChenBot House Status Monitor
=================================

Monitors the parsed Big Brother House Status.

The HouseStatusMonitor compares the newest parsed house state
against the previous state remembered by Julie.

This monitor does NOT download images or RSS feeds.
It simply evaluates parsed production data.

Future responsibilities include:

• Head of Household changes
• Nomination changes
• Power of Veto changes
• Have-Not changes
• Feed status changes
• Evictions
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from database.storage import Storage

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

logger = ProductionLogger.get("HouseStatus")


# ==========================================================
# House Status
# ==========================================================


@dataclass(slots=True)
class HouseStatus:
    """
    Represents the current Big Brother house state.
    """

    hoh: str = ""

    nominees: tuple[str, ...] = ()

    veto_holder: str = ""

    veto_used: bool = False

    have_nots: tuple[str, ...] = ()

    feeds: str = ""


# ==========================================================
# House Status Monitor
# ==========================================================


class HouseStatusMonitor(Monitor):
    """
    Monitors parsed House Status.

    Another module is responsible for producing a new
    HouseStatus object. This monitor simply compares it
    against the previously remembered state.
    """

    def __init__(
        self,
        storage: Optional[Storage] = None,
    ) -> None:

        super().__init__()

        self.storage = storage or Storage()

        self.current = HouseStatus()

        self.pending_status: Optional[
            HouseStatus
        ] = None

        logger.info(
            "House Status monitor initialized."
        )

    # ======================================================
    # Feed New Status
    # ======================================================

    def update(
        self,
        status: HouseStatus,
    ) -> None:
        """
        Supplies a newly parsed house status to be checked
        during the next production cycle.
        """

        self.pending_status = status

    # ======================================================
    # Monitor
    # ======================================================

    async def check(
        self,
    ) -> MonitorResult:

        if self.pending_status is None:

            return MonitorResult(

                monitor=self.name,

                status=MonitorStatus.HEALTHY,

                changed=False,

                detail="No new house status.",

            )

        new = self.pending_status

        self.pending_status = None

        #
        # First observation
        #

        if self.current == HouseStatus():

            self.current = new

            return MonitorResult(

                monitor=self.name,

                status=MonitorStatus.HEALTHY,

                changed=False,

                detail="Initial house status captured.",

            )

        #
        # No changes
        #

        if new == self.current:

            return MonitorResult(

                monitor=self.name,

                status=MonitorStatus.HEALTHY,

                changed=False,

                detail="House status unchanged.",

            )

        events: list[
            ProductionEvent
        ] = []

        #
        # HOH
        #

        if new.hoh != self.current.hoh:

            events.append(

                ProductionEvent(

                    source=self.name,

                    event_type=EventType.HOH_CHANGED,

                    title="Head of Household Changed",

                    detail=f"{self.current.hoh} → {new.hoh}",

                    severity=EventSeverity.IMPORTANT,

                )

            )

        #
        # Nominations
        #

        if new.nominees != self.current.nominees:

            events.append(

                ProductionEvent(

                    source=self.name,

                    event_type=EventType.NOMINATIONS_CHANGED,

                    title="Nominations Changed",

                    detail=", ".join(new.nominees),

                )

            )

        #
        # POV
        #

        if new.veto_holder != self.current.veto_holder:

            events.append(

                ProductionEvent(

                    source=self.name,

                    event_type=EventType.POV_CHANGED,

                    title="Power of Veto Updated",

                    detail=new.veto_holder,

                )

            )

        #
        # Have Nots
        #

        if new.have_nots != self.current.have_nots:

            events.append(

                ProductionEvent(

                    source=self.name,

                    event_type=EventType.HAVE_NOTS_CHANGED,

                    title="Have-Not List Updated",

                    detail=", ".join(new.have_nots),

                )

            )

        #
        # Feed Status
        #

        if new.feeds != self.current.feeds:

            if new.feeds.lower() == "down":

                events.append(

                    ProductionEvent(

                        source=self.name,

                        event_type=EventType.FEEDS_DOWN,

                        title="Live Feeds Down",

                        detail="Feeds are currently unavailable.",

                        severity=EventSeverity.NOTICE,

                    )

                )

            elif new.feeds.lower() == "up":

                events.append(

                    ProductionEvent(

                        source=self.name,

                        event_type=EventType.FEEDS_UP,

                        title="Live Feeds Returned",

                        detail="Feeds are back online.",

                        severity=EventSeverity.NOTICE,

                    )

                )

        #
        # Save new state
        #

        self.current = new

        logger.info(
            "House Status changed."
        )

        return MonitorResult(

            monitor=self.name,

            status=MonitorStatus.HEALTHY,

            changed=True,

            detail="House status updated.",

            events=events,

        )

    # ======================================================
    # Snapshot
    # ======================================================

    def snapshot(self) -> dict:

        return {

            "timestamp":
                datetime.utcnow().isoformat(),

            "hoh":
                self.current.hoh,

            "nominees":
                list(self.current.nominees),

            "veto_holder":
                self.current.veto_holder,

            "veto_used":
                self.current.veto_used,

            "have_nots":
                list(self.current.have_nots),

            "feeds":
                self.current.feeds,

        }

    # ======================================================
    # Convenience Properties
    # ======================================================

    @property
    def hoh(self) -> str:
        return self.current.hoh

    @property
    def nominees(self) -> tuple[str, ...]:
        return self.current.nominees

    @property
    def veto_holder(self) -> str:
        return self.current.veto_holder

    @property
    def veto_used(self) -> bool:
        return self.current.veto_used

    @property
    def have_nots(self) -> tuple[str, ...]:
        return self.current.have_nots

    @property
    def feeds(self) -> str:
        return self.current.feeds