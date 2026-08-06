"""
Julie ChenBot Production Announcer
==================================

Julie ChenBot's voice.

Every production event ultimately becomes
a Discord announcement through this module.
"""

from __future__ import annotations

import random

import discord

from config import LIVE_UPDATES_CHANNEL
from production.events import (
    EventType,
    ProductionEvent,
)
from services.logger import ProductionLogger


# ==========================================================
# Julie Personality
# ==========================================================

INTRO_LINES = [

    "Good evening, Houseguests...",

    "Houseguests...",

    "Attention, Houseguests...",

    "Production has an announcement.",

    "The Big Brother house never sleeps...",

    "Expect the unexpected...",

    "Previously on Big Brother...",

    "Julie ChenBot here with the latest from inside the house.",

    "The cameras have captured something new...",

    "Let's check in with our Houseguests...",


]

ENDING_LINES = [

    "Expect the unexpected.",

    "Stay tuned.",

    "We'll be watching.",

    "The game never stops.",

    "Julie ChenBot will continue monitoring the house.",

    "Another chapter inside the Big Brother house.",

    "Until the next production announcement...",


]


class ProductionAnnouncer:

    def __init__(self, bot: discord.Client):

        self.bot = bot

        self.logger = ProductionLogger.get(
            "Announcer"
        )

    # ======================================================
    # Public
    # ======================================================

    async def announce(
        self,
        event: ProductionEvent,
    ) -> bool:

        channel = self.bot.get_channel(
            LIVE_UPDATES_CHANNEL
        )

        if channel is None:

            self.logger.warning(
                "Live Updates channel not found."
            )

            return False

        embed = self.build_embed(
            event
        )

        try:

            await channel.send(
                embed=embed
            )

            self.logger.info(
                "Announcement sent: %s",
                event.title,
            )

            return True

        except Exception:

            self.logger.exception(
                "Failed sending announcement."
            )

            return False

    # ======================================================
    # Embed
    # ======================================================

    def build_embed(
        self,
        event: ProductionEvent,
    ) -> discord.Embed:

        embed = discord.Embed(

            title=self.title(event),

            description=self.description(event),

            colour=self.colour(event),

        )

        embed.set_author(

            name="🎥 Julie ChenBot",

        )

        embed.set_footer(

            text=random.choice(
                ENDING_LINES
            )

        )

        if event.url:

            embed.add_field(

                name="📰 Source",

                value=f"[View Update]({event.url})",

                inline=False,

            )

        embed.add_field(

            name="📺 Event",

            value=event.event_type.value.replace(
                "_",
                " "
            ).title(),

            inline=True,

        )

        embed.timestamp = event.timestamp

        return embed

    # ======================================================
    # Title
    # ======================================================

    def title(
        self,
        event: ProductionEvent,
    ) -> str:

        match event.event_type:

            case EventType.RSS_UPDATE:
                return "🎥 PRODUCTION ANNOUNCEMENT"

            case EventType.FEEDS_DOWN:
                return "🚫 FEEDS ARE DOWN"

            case EventType.FEEDS_RETURNED:
                return "📺 FEEDS HAVE RETURNED"

            case EventType.COMPETITION_STARTED:
                return "🏆 COMPETITION UNDERWAY"

            case EventType.COMPETITION_FINISHED:
                return "🏁 COMPETITION COMPLETE"

            case EventType.HOH_CROWNED:
                return "👑 NEW HEAD OF HOUSEHOLD"

            case EventType.NOMINATIONS:
                return "🎯 NOMINATION CEREMONY"

            case EventType.POV_PLAYED:
                return "💎 POWER OF VETO"

            case EventType.POV_USED:
                return "💎 VETO CEREMONY"

            case EventType.EVICTION:
                return "🚪 HOUSEGUEST EVICTED"

            case EventType.TWIST:
                return "🌪️ BIG BROTHER TWIST"

            case _:
                return event.title

    # ======================================================
    # Description
    # ======================================================

    def description(
        self,
        event: ProductionEvent,
    ) -> str:

        intro = random.choice(
            INTRO_LINES
        )

        lines = [

            f"**{intro}**",

            "",

            event.message,

        ]

        return "\n".join(
            lines
        )

    # ======================================================
    # Colors
    # ======================================================

    def colour(
        self,
        event: ProductionEvent,
    ) -> discord.Colour:

        match event.event_type:

            case EventType.RSS_UPDATE:
                return discord.Colour.blue()

            case EventType.FEEDS_DOWN:
                return discord.Colour.red()

            case EventType.FEEDS_RETURNED:
                return discord.Colour.green()

            case EventType.COMPETITION_STARTED:
                return discord.Colour.gold()

            case EventType.COMPETITION_FINISHED:
                return discord.Colour.orange()

            case EventType.HOH_CROWNED:
                return discord.Colour.purple()

            case EventType.NOMINATIONS:
                return discord.Colour.dark_red()

            case EventType.POV_PLAYED:
                return discord.Colour.teal()

            case EventType.POV_USED:
                return discord.Colour.dark_teal()

            case EventType.EVICTION:
                return discord.Colour.dark_grey()

            case _:
                return discord.Colour.blurple()