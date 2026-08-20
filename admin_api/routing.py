"""
Julie ChenBot Admin API — Discord Routing Introspection
=========================================================

Builds a read-only view of where each production EventType is
delivered, by calling DiscordOutputRouter's own real routing decision
(_destinations()) for a synthetic event of each type -- not a
hand-maintained duplicate of that logic, so this can never silently
drift from the actual routing behavior in services/discord_output.py.

_destinations() is pure (reads only config channel constants and the
event's own fields; it never touches self.bot), so it is safe to call
against a router constructed with bot=None purely for introspection.
Nothing in this module ever sends a Discord message or touches
Discord's API.
"""

from __future__ import annotations

from config import (
    HOUSE_STATUS_CHANNEL,
    LIVE_UPDATES_CHANNEL,
    PRODUCTION_CHANNEL,
    PRODUCTION_LOG_CHANNEL,
)
from production.events import EventSeverity, EventType, ProductionEvent
from services.discord_output import DiscordOutputRouter

_CHANNEL_ID_BY_NAME = {
    "live-updates": LIVE_UPDATES_CHANNEL,
    "house-status": HOUSE_STATUS_CHANNEL,
    "production": PRODUCTION_CHANNEL,
    "production-log": PRODUCTION_LOG_CHANNEL,
}


def _channel_info(name: str) -> dict:
    channel_id = _CHANNEL_ID_BY_NAME.get(name, 0)
    return {
        "channel": f"#{name}",
        "channel_id": channel_id or None,
        "configured": bool(channel_id),
    }


def build_routing_table() -> dict:
    """Returns {event_type_value: {destinations, escalates_to_production_log_on_warning}}."""

    router = DiscordOutputRouter(bot=None)
    table: dict[str, dict] = {}

    for event_type in EventType:
        base_event = ProductionEvent(
            source="", event_type=event_type, title="", detail="",
            severity=EventSeverity.INFO,
        )
        warning_event = ProductionEvent(
            source="", event_type=event_type, title="", detail="",
            severity=EventSeverity.WARNING,
        )

        base_destinations = router._destinations(base_event)
        warning_destinations = router._destinations(warning_event)

        table[event_type.value] = {
            "destinations": [_channel_info(name) for _, name in base_destinations],
            "escalates_to_production_log_on_warning": (
                len(warning_destinations) > len(base_destinations)
            ),
        }

    return table


def channel_configuration() -> dict:
    """Returns the configured channel IDs (safe to expose -- Discord
    channel IDs, not secrets) for the dashboard's Discord admin page."""

    return {name: _channel_info(name) for name in _CHANNEL_ID_BY_NAME}
