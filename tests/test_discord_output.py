"""Tests for Julie ChenBot's Discord production output router."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from production.events import EventSeverity, EventType, ProductionEvent
from services.discord_output import DiscordOutputRouter


class FakeChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.messages = []

    async def send(self, **kwargs) -> None:
        self.messages.append(kwargs)


class FakeBot:
    def __init__(self, channels: list[FakeChannel]) -> None:
        self.channels = channels

    def get_channel(self, channel_id: int):
        return next(
            (channel for channel in self.channels if channel.id == channel_id),
            None,
        )

    async def fetch_channel(self, channel_id: int):
        return self.get_channel(channel_id)

    def get_all_channels(self):
        return iter(self.channels)


def make_event(
    event_type: EventType = EventType.RSS_UPDATE,
    severity: EventSeverity = EventSeverity.INFO,
) -> ProductionEvent:
    return ProductionEvent(
        source="Joker's Updates",
        event_type=event_type,
        title="LIVE FEED UPDATE",
        detail="Mallory & Melody in Pod BR.",
        severity=severity,
        metadata={
            "link": "https://forums.jokersupdates.com/example",
            "published": "Sat, 08 Aug 2026 10:00:00 -0700",
        },
    )


def test_rss_update_routes_to_live_updates_by_channel_name(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    channel = FakeChannel(123, "live-updates")
    router = DiscordOutputRouter(FakeBot([channel]))

    asyncio.run(router.publish(make_event()))

    assert len(channel.messages) == 1
    embed = channel.messages[0]["embed"]
    assert embed.title == "🟦 LIVE FEED UPDATE"
    assert "Mallory & Melody" in embed.description
    assert "Joker's Updates" in embed.footer.text


def test_house_event_routes_to_house_status_and_live_updates(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    router = DiscordOutputRouter(FakeBot([house, live]))

    asyncio.run(
        router.publish(
            make_event(
                event_type=EventType.HOH_CHANGED,
                severity=EventSeverity.IMPORTANT,
            )
        )
    )

    assert len(house.messages) == 1
    assert len(live.messages) == 1


def test_duplicate_channel_configuration_only_sends_once(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    channel = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([channel]))

    asyncio.run(
        router.publish(
            make_event(event_type=EventType.HOH_CHANGED)
        )
    )

    assert len(channel.messages) == 1
