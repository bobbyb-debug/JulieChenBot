"""
Julie ChenBot Recap Command
============================

Summarizes recent Joker's Updates live-feed activity in Julie's voice
-- approximately the last RECAP_WINDOW_HOURS hours of what was
actually reported on the raw live feed, nothing else.

Deliberately does NOT gather or pass in current tracked game state,
Hamsterwatch history, or taught knowledge: those are separate systems
(see production/house_status.py, production/hamsterwatch.py,
production/knowledge.py) and blending them into "what happened on the
feeds recently" is exactly the cross-source contamination /recap must
avoid. /chat (services/discord.py generate_ai_reply()) is where a
human asks a specific question and gets Julie's full context instead
-- /recap answers one narrower question: what did Joker's Updates
actually report recently.

engine.recent_updates() (see production/engine.py) is already scoped
to real RSS_UPDATE events only and already applies the time window --
this command does no filtering of its own.
"""

from __future__ import annotations

import discord

from services.ai_service import generate_recap
from services.logger import ProductionLogger

logger = ProductionLogger.get("Recap")


def register(discord_service) -> None:
    """Registers the /recap slash command."""

    @discord_service.command(
        name="recap",
        description="Summarizes recent Joker's Updates live-feed activity.",
    )
    async def recap(interaction: discord.Interaction):

        await interaction.response.defer()

        engine = discord_service.scheduler.engine
        entries = engine.recent_updates()
        window_hours = engine.RECAP_WINDOW_HOURS

        if entries:
            summary = await generate_recap(entries)
        else:
            # No AI call needed -- there is nothing to summarize.
            summary = (
                f"No live-feed activity in the last ~{window_hours}h, Houseguest."
            )

        embed = discord.Embed(
            title="📼 Recent Live-Feed Recap",
            description=summary,
            color=0x9B59B6,
        )

        if entries:
            embed.set_footer(
                text=(
                    f"Based on {len(entries)} Joker's Update(s) "
                    f"from the last ~{window_hours}h"
                )
            )

        await interaction.followup.send(embed=embed)

        logger.info(
            "/recap used by %s (%s): %s update(s) in the last ~%sh",
            interaction.user,
            interaction.user.id,
            len(entries),
            window_hours,
        )
