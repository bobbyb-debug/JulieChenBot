"""Tests for Julie ChenBot's Discord production output router."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from production.events import EventSeverity, EventType, ProductionEvent
from services.discord_output import DiscordOutputRouter


class FakeChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.messages = []

    async def send(self, **kwargs) -> None:
        self.messages.append(kwargs)


class FlakyChannel(FakeChannel):
    """A channel whose send() fails a fixed number of times before
    succeeding -- simulates a transient discord.HTTPException-style
    failure on one specific destination, isolated from the others."""

    def __init__(self, channel_id: int, name: str, fail_times: int = 0) -> None:
        super().__init__(channel_id, name)
        self.fail_times = fail_times
        self.send_calls = 0

    async def send(self, **kwargs) -> None:
        self.send_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated Discord send failure")
        await super().send(**kwargs)


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
    """When two destinations (house-status, live-updates) are
    configured to the SAME real channel ID, the router must send to
    that physical channel once, not twice -- and both destinations
    count as delivered, so the call succeeds without raising.

    (This previously used HOUSE_STATUS_CHANNEL=0/LIVE_UPDATES_CHANNEL=0
    with only a "live-updates"-named fake channel present, which
    doesn't actually set up a shared-channel scenario at all -- house-
    status was simply never resolvable. That incidentally relied on
    the pre-fix bug this test file now guards against: an unresolvable
    destination alongside a successful one used to be silently treated
    as a full success. Fixed here to a real shared-ID scenario, which
    is what "duplicate channel configuration" actually means.)
    """

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 42)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 42)

    channel = FakeChannel(42, "shared-channel")
    router = DiscordOutputRouter(FakeBot([channel]))

    asyncio.run(
        router.publish(
            make_event(event_type=EventType.HOH_CHANGED)
        )
    )

    assert len(channel.messages) == 1


def test_house_image_update_routes_to_house_status_and_embeds_image(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    router = DiscordOutputRouter(FakeBot([house, live]))

    # Stub the download so this test never touches the network. Without
    # this the result depends on whether jokersupdates.com is reachable:
    # a successful download attaches the file, a failed one falls back to
    # hot-linking, and the test would assert different things on
    # different machines.
    async def fake_download(url):
        return b"fake-png-bytes"

    monkeypatch.setattr(router, "_download", fake_download)

    event = ProductionEvent(
        source="HouseImage",
        event_type=EventType.IMAGE_CHANGED,
        title="HOUSE STATUS IMAGE UPDATED",
        detail="JokersUpdates house-status image changed after episode air.",
        severity=EventSeverity.NOTICE,
        metadata={
            "url": "http://www.jokersupdates.com/ubbthreads/images/headers/bigbrother/hg/bbupdatesblock1786231774.png"
        },
    )

    asyncio.run(router.publish(event))

    assert len(house.messages) == 1
    assert len(live.messages) == 1
    house_embed = house.messages[0]["embed"]
    assert house_embed.title == "🏠 HOUSE STATUS UPDATED"
    # The image is uploaded as an attachment rather than hot-linked, so
    # the post cannot break when the source filename rotates away.
    assert house_embed.image.url == "attachment://house_status.png"
    assert house.messages[0]["file"] is not None
    # The original URL is still reachable in the Source field.
    assert event.metadata["url"] in str(house_embed.fields[0].value)


def test_house_image_falls_back_to_hotlink_when_download_fails(monkeypatch) -> None:
    """A failed download must still post, using the plain URL."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    router = DiscordOutputRouter(FakeBot([house, live]))

    async def failed_download(url):
        return None

    monkeypatch.setattr(router, "_download", failed_download)

    url = "http://www.jokersupdates.com/x/bbupdatesblock1786231774.png"
    event = ProductionEvent(
        source="HouseImage",
        event_type=EventType.IMAGE_CHANGED,
        title="HOUSE STATUS IMAGE UPDATED",
        detail="changed",
        severity=EventSeverity.NOTICE,
        metadata={"url": url},
    )

    asyncio.run(router.publish(event))

    assert len(house.messages) == 1
    assert house.messages[0].get("file") is None
    assert house.messages[0]["embed"].image.url == url


# ==========================================================
# Hamsterwatch notifications
# ==========================================================


def _hamsterwatch_event(**metadata_overrides) -> ProductionEvent:
    metadata = {
        "url": "http://hamsterwatch.com/bb28/081026.shtml",
        "link": "http://hamsterwatch.com/bb28/081026.shtml",
        "bb_day": 37,
        "article_date": "2026-08-12",
        "published": "2026-08-12",
        "heading": "Day 37 - Wednesday - August 12, 2026",
        "summary": "LaLa and Devens talked strategy about next week's veto.",
        "count": 1,
    }
    metadata.update(metadata_overrides)
    return ProductionEvent(
        source="Hamsterwatch",
        event_type=EventType.TIMELINE,
        title="HAMSTERWATCH UPDATED",
        detail="Day 37 - Wednesday - August 12, 2026\n\nLaLa and Devens talked strategy.",
        severity=EventSeverity.NOTICE,
        metadata=metadata,
    )


def test_hamsterwatch_event_routes_to_live_updates_only(monkeypatch) -> None:
    """Hamsterwatch commentary is feed-adjacent content, not house
    state — it must not be confused with house-status routing."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 999)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(999, "house-status")
    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([house, live]))

    asyncio.run(router.publish(_hamsterwatch_event()))

    assert len(live.messages) == 1
    assert len(house.messages) == 0


def test_hamsterwatch_embed_shows_day_title_bb_day_field_and_link(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    channel = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([channel]))

    asyncio.run(router.publish(_hamsterwatch_event()))

    embed = channel.messages[0]["embed"]
    assert embed.title == "🐹 HAMSTERWATCH UPDATE — Day 37"
    assert any(
        field.name == "📅 BB Day" and field.value == "37" for field in embed.fields
    )
    assert any(
        field.name == "🕒 Published" and field.value == "2026-08-12"
        for field in embed.fields
    )
    source_field = next(field for field in embed.fields if field.name == "🔗 Source")
    assert "hamsterwatch.com/bb28/081026.shtml" in source_field.value
    assert "Hamsterwatch" in source_field.value


def test_hamsterwatch_multi_day_event_shows_count_in_title(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    channel = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([channel]))

    asyncio.run(router.publish(_hamsterwatch_event(count=2, bb_day=38)))

    embed = channel.messages[0]["embed"]
    assert embed.title == "🐹 HAMSTERWATCH UPDATE — 2 new recaps"


def test_hamsterwatch_event_without_bb_day_uses_plain_title(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    channel = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([channel]))

    asyncio.run(router.publish(_hamsterwatch_event(bb_day=None, count=1)))

    embed = channel.messages[0]["embed"]
    assert embed.title == "🐹 HAMSTERWATCH UPDATE"
    assert not any(field.name == "📅 BB Day" for field in embed.fields)


# ==========================================================
# Per-destination delivery tracking (partial multi-destination
# failure must not duplicate successful destinations or silently
# drop failed ones)
# ==========================================================


def _multi_destination_event() -> ProductionEvent:
    """An event routed to two destinations (house-status,
    live-updates) via HOH_CHANGED, matching real routing."""

    return make_event(event_type=EventType.HOH_CHANGED, severity=EventSeverity.IMPORTANT)


def test_success_then_failure_retry_does_not_resend_succeeded_destination(monkeypatch) -> None:
    """Destination A (house-status) succeeds, destination B
    (live-updates) fails. On retry, A must not be sent again, and B
    must be attempted again and eventually succeed."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FlakyChannel(2, "live-updates", fail_times=1)
    router = DiscordOutputRouter(FakeBot([house, live]))

    event = _multi_destination_event()

    with pytest.raises(RuntimeError):
        asyncio.run(router.publish(event))

    assert len(house.messages) == 1
    assert live.send_calls == 1
    assert len(live.messages) == 0
    assert event.delivered_to == {"house-status"}

    # Retry: same event instance, live-updates now succeeds.
    asyncio.run(router.publish(event))

    assert len(house.messages) == 1, "successful destination must not be resent"
    assert len(live.messages) == 1
    assert event.delivered_to == {"house-status", "live-updates"}


def test_failure_then_success_retry_does_not_resend_succeeded_destination(monkeypatch) -> None:
    """Reversed order from the previous test: destination A
    (house-status) fails first, destination B (live-updates)
    succeeds immediately. Same semantics must hold regardless of
    which destination in the list failed."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FlakyChannel(1, "house-status", fail_times=1)
    live = FakeChannel(2, "live-updates")
    router = DiscordOutputRouter(FakeBot([house, live]))

    event = _multi_destination_event()

    with pytest.raises(RuntimeError):
        asyncio.run(router.publish(event))

    assert len(live.messages) == 1
    assert house.send_calls == 1
    assert len(house.messages) == 0
    assert event.delivered_to == {"live-updates"}

    asyncio.run(router.publish(event))

    assert len(live.messages) == 1, "successful destination must not be resent"
    assert len(house.messages) == 1
    assert event.delivered_to == {"house-status", "live-updates"}


def test_unresolvable_channel_stays_retryable_without_resending_successful_one(monkeypatch) -> None:
    """One destination resolves and succeeds; the other cannot be
    resolved at all (channel doesn't exist yet on the bot). The
    resolvable one must not be resent once the other becomes
    resolvable on a later retry."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    # No "live-updates"-named channel exists yet -> unresolvable.
    router = DiscordOutputRouter(FakeBot([house]))

    event = _multi_destination_event()

    with pytest.raises(RuntimeError):
        asyncio.run(router.publish(event))

    assert len(house.messages) == 1
    assert event.delivered_to == {"house-status"}

    # The live-updates channel becomes available before the retry.
    live = FakeChannel(2, "live-updates")
    router.bot.channels.append(live)

    asyncio.run(router.publish(event))

    assert len(house.messages) == 1, "successful destination must not be resent"
    assert len(live.messages) == 1
    assert event.delivered_to == {"house-status", "live-updates"}


def test_all_destinations_fail_raises_and_marks_nothing_delivered(monkeypatch) -> None:
    """Existing total-failure behavior: if every destination fails,
    publish() must still raise, and nothing is marked delivered."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    router = DiscordOutputRouter(FakeBot([]))  # nothing resolvable at all

    event = _multi_destination_event()

    with pytest.raises(RuntimeError):
        asyncio.run(router.publish(event))

    assert event.delivered_to == set()


def test_all_destinations_succeed_marks_all_delivered_without_raising(monkeypatch) -> None:
    """Existing normal-path behavior: every destination succeeding
    must not raise, and every destination is recorded delivered."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    router = DiscordOutputRouter(FakeBot([house, live]))

    event = _multi_destination_event()

    asyncio.run(router.publish(event))  # must not raise

    assert len(house.messages) == 1
    assert len(live.messages) == 1
    assert event.delivered_to == {"house-status", "live-updates"}


def test_no_duplicate_send_to_successful_destination_across_retry(monkeypatch) -> None:
    """Explicit send-count assertion: first destination succeeds,
    second raises, then the event is retried. The successful
    destination's send count must remain exactly 1 throughout."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FlakyChannel(2, "live-updates", fail_times=1)
    router = DiscordOutputRouter(FakeBot([house, live]))

    event = _multi_destination_event()

    with pytest.raises(RuntimeError):
        asyncio.run(router.publish(event))

    asyncio.run(router.publish(event))  # retry

    assert len(house.messages) == 1, "successful destination must not be resent on retry"
