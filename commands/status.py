"""
Julie ChenBot Status Command
=============================

Shows Julie's current production health: uptime, tick count, and
per-monitor status, sourced directly from ProductionEngine.health().
"""

from __future__ import annotations

import discord

from services.logger import ProductionLogger

logger = ProductionLogger.get("Status")

STATUS_ICONS = {
    "healthy": "🟢",
    "degraded": "🟡",
    "offline": "🔴",
}


def register(discord_service) -> None:
    """Registers the /status slash command."""

    @discord_service.command(
        name="status",
        description="Shows Julie's current production status.",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def status(interaction: discord.Interaction):

        engine = discord_service.scheduler.engine
        health = engine.health()
        info = engine.info()

        icon = STATUS_ICONS.get(health["status"], "⚪")

        embed = discord.Embed(
            title=f"{icon} Julie ChenBot — {health['status'].title()}",
            description=(
                f"Version {info['version']} ({info['phase']}) · "
                f"up {health['uptime']}"
            ),
            color=(
                0x2ECC71 if health["status"] == "healthy" else 0xF1C40F
            ),
        )

        embed.add_field(
            name="Production Cycles",
            value=(
                f"{health['tick_count']} tick(s)\n"
                f"Last: {health['last_tick_at'] or 'never'}"
            ),
            inline=True,
        )

        embed.add_field(
            name="Monitors",
            value=(
                f"{health['healthy_monitors']}/{health['monitor_count']} "
                "healthy"
            ),
            inline=True,
        )

        if health["last_error"]:
            embed.add_field(
                name="⚠️ Last Error",
                value=str(health["last_error"])[:1000],
                inline=False,
            )

        if health["monitors"]:

            lines = []

            for monitor in health["monitors"]:

                monitor_icon = STATUS_ICONS.get(
                    monitor["status"], "⚪"
                )

                changed = " · changed" if monitor["changed"] else ""

                lines.append(
                    f"{monitor_icon} **{monitor['name']}** — "
                    f"{monitor['duration_ms']:.0f} ms{changed}"
                )

            embed.add_field(
                name="Last Cycle Detail",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        await interaction.response.send_message(embed=embed)

        logger.info(
            "/status used by %s (%s)",
            interaction.user,
            interaction.user.id,
        )
