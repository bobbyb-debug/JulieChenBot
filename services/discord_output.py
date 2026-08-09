"""
Julie ChenBot Discord Production Output
=======================================

Routes ProductionEvents to the appropriate Discord channels.
"""

from __future__ import annotations

import discord

from config import (
    HOUSE_STATUS_CHANNEL,
    LIVE_UPDATES_CHANNEL,
    PRODUCTION_CHANNEL,
    PRODUCTION_LOG_CHANNEL,
)
from production.events import EventSeverity, EventType, ProductionEvent
from services.logger import ProductionLogger


class DiscordOutputRouter:
    """Publishes ProductionEvents to configured Discord channels."""

    _CHANNEL_NAMES = {
        "live_updates": "live-updates",
        "house_status": "house-status",
        "production": "production",
        "production_log": "production-log",
    }

    def __init__(self, bot) -> None:
        self.bot = bot
        self.logger = ProductionLogger.get("DiscordOutput")

    async def publish(self, event: ProductionEvent) -> None:
        """Routes and publishes one production event."""
        destinations = self._destinations(event)
        if not destinations:
            self.logger.warning(
                "No Discord destination configured for event: %s",
                event.event_type.value,
            )
            return

        sent = 0
        seen: set[int] = set()
        for channel_id, channel_name in destinations:
            channel = await self._resolve_channel(channel_id, channel_name)
            if channel is None:
                self.logger.warning("Discord channel unavailable: #%s", channel_name)
                continue
            if channel.id in seen:
                continue
            seen.add(channel.id)
            await channel.send(embed=self._build_embed(event))
            sent += 1
            self.logger.info(
                "Published %s to #%s.",
                event.event_type.value,
                getattr(channel, "name", channel_name),
            )

        if sent == 0:
            raise RuntimeError(
                f"Unable to publish {event.event_type.value}: no Discord destination was reachable."
            )

    def _destinations(self, event: ProductionEvent) -> list[tuple[int, str]]:
        destinations: list[tuple[int, str]] = []

        if event.event_type in {EventType.RSS_UPDATE, EventType.TIMELINE}:
            destinations.append(
                (LIVE_UPDATES_CHANNEL, self._CHANNEL_NAMES["live_updates"])
            )
        elif event.event_type in {
            EventType.HOUSE_STATUS_CHANGED,
            EventType.HOH_CHANGED,
            EventType.NOMINATIONS_CHANGED,
            EventType.POV_CHANGED,
            EventType.HAVE_NOTS_CHANGED,
            EventType.FEEDS_UP,
            EventType.FEEDS_DOWN,
        }:
            destinations.extend([
                (HOUSE_STATUS_CHANNEL, self._CHANNEL_NAMES["house_status"]),
                (LIVE_UPDATES_CHANNEL, self._CHANNEL_NAMES["live_updates"]),
            ])
        elif event.event_type in {
            EventType.COMPETITION_STARTED,
            EventType.COMPETITION_FINISHED,
            EventType.COMPETITION_CHANGED,
            EventType.COMPETITION_WINNER,
        }:
            destinations.extend([
                (PRODUCTION_CHANNEL, self._CHANNEL_NAMES["production"]),
                (LIVE_UPDATES_CHANNEL, self._CHANNEL_NAMES["live_updates"]),
            ])
        else:
            destinations.append(
                (PRODUCTION_CHANNEL, self._CHANNEL_NAMES["production"])
            )

        if event.severity in {
            EventSeverity.WARNING,
            EventSeverity.IMPORTANT,
            EventSeverity.CRITICAL,
        } and PRODUCTION_LOG_CHANNEL:
            destinations.append(
                (PRODUCTION_LOG_CHANNEL, self._CHANNEL_NAMES["production_log"])
            )

        return destinations

    async def _resolve_channel(self, channel_id: int, channel_name: str):
        if channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                return channel
            try:
                return await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                self.logger.exception("Failed to fetch Discord channel %s.", channel_id)

        return discord.utils.find(
            lambda channel: getattr(channel, "name", "") == channel_name,
            self.bot.get_all_channels(),
        )

    @staticmethod
    def _build_embed(event: ProductionEvent) -> discord.Embed:
        colors = {
            EventSeverity.DEBUG: 0x95A5A6,
            EventSeverity.INFO: 0x3498DB,
            EventSeverity.NOTICE: 0x2ECC71,
            EventSeverity.WARNING: 0xF1C40F,
            EventSeverity.IMPORTANT: 0xE67E22,
            EventSeverity.CRITICAL: 0xE74C3C,
        }

        if event.event_type == EventType.RSS_UPDATE:
            title = "🟦 LIVE FEED UPDATE"
        elif event.event_type == EventType.TIMELINE and event.source == "Hamsterwatch":
            title = "🐹 HAMSTERWATCH UPDATE"
        else:
            title = event.title

        embed = discord.Embed(
            title=title,
            description=event.detail,
            color=colors.get(event.severity, 0x3498DB),
            timestamp=event.created_at,
        )

        link = event.metadata.get("link") or event.metadata.get("url")
        if link:
            label = "Hamsterwatch" if event.source == "Hamsterwatch" else "Joker's Updates"
            embed.add_field(
                name="🔗 Read More",
                value=f"[{label}]({link})",
                inline=False,
            )

        published = event.metadata.get("published")
        if published:
            embed.add_field(name="🕒 Published", value=str(published), inline=False)

        embed.set_footer(text=f"Julie ChenBot • Source: {event.source}")
        return embed
