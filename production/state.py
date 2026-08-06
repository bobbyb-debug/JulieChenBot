"""
Julie ChenBot Production State
==============================

Represents the CURRENT state of the Big Brother house.

Unlike ProductionTracker, which remembers past events,
ProductionState represents what is true RIGHT NOW.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class ProductionState:
    """
    Current production state.
    """

    # ==========================================================
    # Feed Status
    # ==========================================================

    feeds_live: bool = False

    feeds_message: str = "Unknown"

    last_feed_change: datetime | None = None

    # ==========================================================
    # RSS
    # ==========================================================

    last_rss_title: str = ""

    last_rss_guid: str = ""

    last_rss_time: str = ""

    # ==========================================================
    # House
    # ==========================================================

    hoh: str = ""

    nominees: list[str] = field(
        default_factory=list
    )

    veto_holder: str = ""

    veto_used: bool = False

    evicted: list[str] = field(
        default_factory=list
    )

    # ==========================================================
    # Competition
    # ==========================================================

    competition_running: bool = False

    competition_name: str = ""

    competition_started: datetime | None = None

    # ==========================================================
    # Statistics
    # ==========================================================

    rss_updates_today: int = 0

    feed_interruptions: int = 0

    announcements_sent: int = 0

    # ==========================================================
    # Helpers
    # ==========================================================

    def feeds_down(self) -> None:

        self.feeds_live = False

        self.last_feed_change = datetime.now()

    def feeds_returned(self) -> None:

        self.feeds_live = True

        self.last_feed_change = datetime.now()

    def start_competition(
        self,
        name: str = "",
    ) -> None:

        self.competition_running = True

        self.competition_name = name

        self.competition_started = datetime.now()

    def end_competition(self) -> None:

        self.competition_running = False

        self.competition_name = ""

        self.competition_started = None

    def new_rss(
        self,
        title: str,
        guid: str,
        published: str,
    ) -> None:

        self.last_rss_title = title

        self.last_rss_guid = guid

        self.last_rss_time = published

        self.rss_updates_today += 1

    def announcement_sent(self) -> None:

        self.announcements_sent += 1

    # ==========================================================
    # Status
    # ==========================================================

    @property
    def status(self) -> str:

        if self.competition_running:
            return "Competition"

        if self.feeds_live:
            return "Live"

        return "Feeds Down"

    # ==========================================================
    # Summary
    # ==========================================================

    def summary(self) -> dict:

        return {

            "feeds_live": self.feeds_live,

            "status": self.status,

            "rss_updates_today": self.rss_updates_today,

            "feed_interruptions": self.feed_interruptions,

            "competition_running": self.competition_running,

            "competition_name": self.competition_name,

            "hoh": self.hoh,

            "nominees": self.nominees,

            "veto_holder": self.veto_holder,

            "announcements_sent": self.announcements_sent,
        }