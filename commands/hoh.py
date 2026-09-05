"""
Julie ChenBot HOH Command
==========================

Shows the current Head of Household, from the official facts record
(KnowledgeStore STATE topic "HOH" -- see production/knowledge.py),
set only via /teach update or the admin dashboard. Deliberately does
NOT read the automated, live-feed-driven HouseStatus (production/
house_status.py) -- that value is an unverified observation, never
treated as confirmed fact by any Discord command.

Reads current_state() rather than active_state() -- current_state()
additionally enforces the current reporting week's boundary (see
KnowledgeStore.current_state()/start_new_week()), so a value taught
for a previous week and never re-confirmed this week correctly reads
as "not confirmed yet" instead of silently continuing to answer with
stale data. Falls back to active_state() for a test double that
doesn't define current_state() -- current_state() is a superset of
active_state()'s behavior when no week boundary has been set.
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("HOH")


def register(discord_service) -> None:
    """Registers the /hoh slash command."""

    @discord_service.command(
        name="hoh",
        description="Shows the current Head of Household.",
    )
    async def hoh(interaction: discord.Interaction):

        knowledge = discord_service.scheduler.engine.knowledge
        current_state = getattr(knowledge, "current_state", knowledge.active_state)
        official = current_state("HOH")

        if official is None or not official.content.strip():
            message = (
                "👑 No Head of Household has been confirmed yet this cycle."
            )
        else:
            message = f"👑 **{official.content}** is the current Head of Household."

        await interaction.response.send_message(message)

        logger.info(
            "/hoh used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
