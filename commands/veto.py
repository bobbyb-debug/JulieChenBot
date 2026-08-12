"""
Julie ChenBot Veto Command
===========================

Shows the current Power of Veto holder and whether it's been used,
from tracked house status.
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

        status = discord_service.scheduler.engine.watcher.house_status.current

        if not status.veto_holder:
            message = (
                "🔑 No Power of Veto winner has been confirmed yet "
                "this cycle."
            )
        else:
            used = " (used)" if status.veto_used else " (not yet used)"
            message = f"🔑 **{status.veto_holder}** holds the veto{used}."

        await interaction.response.send_message(message)

        logger.info(
            "/veto used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
