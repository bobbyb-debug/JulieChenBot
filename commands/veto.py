"""
Julie ChenBot Veto Command
===========================

Shows the current Power of Veto holder and, if known, whether it's
been used -- from the official facts record (KnowledgeStore STATE
topics "VETO_WINNER" and "VETO_USED" -- see production/knowledge.py),
set only via /teach update or the admin dashboard. Deliberately does
NOT read the automated, live-feed-driven HouseStatus (production/
house_status.py) -- that value is an unverified observation, never
treated as confirmed fact by any Discord command.

VETO_USED has no dedicated /teach shortcut -- it's taught the same
generic way any other STATE topic is (e.g. a "VETO_USED: yes" line in
/teach update or the dashboard's Update State), since KnowledgeStore
already accepts any topic string for a STATE item. If it's never been
taught, this command simply omits the used/not-used clause rather
than guessing from an unverified source.
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("Veto")


def register(discord_service) -> None:
    """Registers the /veto slash command."""

    @discord_service.command(
        name="veto",
        description="Shows the current Power of Veto status.",
    )
    async def veto(interaction: discord.Interaction):

        knowledge = discord_service.scheduler.engine.knowledge
        # current_state() additionally enforces the current reporting
        # week's boundary (see production/knowledge.py KnowledgeStore.
        # current_state()) so a veto winner/used flag taught for a
        # previous week and never re-confirmed this week correctly
        # reads as "not confirmed yet." Falls back to active_state()
        # for a test double that doesn't define current_state().
        current_state = getattr(knowledge, "current_state", knowledge.active_state)
        holder = current_state("VETO_WINNER")

        if holder is None or not holder.content.strip():
            message = (
                "🔑 No Power of Veto winner has been confirmed yet "
                "this cycle."
            )
        else:
            used_state = current_state("VETO_USED")
            if used_state is not None and used_state.content.strip():
                used_yes = used_state.content.strip().lower() in (
                    "yes", "true", "used"
                )
                used = " (used)" if used_yes else " (not yet used)"
            else:
                used = ""
            message = f"🔑 **{holder.content}** holds the veto{used}."

        await interaction.response.send_message(message)

        logger.info(
            "/veto used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
