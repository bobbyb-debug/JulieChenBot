"""Tests for Julie ChenBot's Discord production output router."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import services.discord_output as discord_output_module
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


def test_house_state_event_routes_to_live_updates_only(monkeypatch) -> None:
    """Structured game-state change events (HouseStatusMonitor,
    CompetitionMonitor) are not House Status image events -- they must
    reach #live-updates only, never #house-status. See _destinations()
    in services/discord_output.py for the routing-regression fix this
    guards against (HOH_CHANGED/COMPETITION_WINNER were previously
    also posted to #house-status)."""

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

    assert len(house.messages) == 0
    assert len(live.messages) == 1


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.HOUSE_STATUS_CHANGED,
        EventType.HOH_CHANGED,
        EventType.NOMINATIONS_CHANGED,
        EventType.POV_CHANGED,
        EventType.HAVE_NOTS_CHANGED,
        EventType.FEEDS_UP,
        EventType.FEEDS_DOWN,
    ],
)
def test_every_structured_state_event_type_excludes_house_status(
    event_type, monkeypatch
) -> None:
    """Every structured game-state change type -- not just HOH_CHANGED
    -- must route to live-updates only. Direct assertion on the
    routing table so a newly-added event type can't silently regress
    back into #house-status."""

    router = DiscordOutputRouter(bot=None)

    destinations = router._destinations(make_event(event_type=event_type))
    names = {name for _, name in destinations}

    assert "house-status" not in names
    assert names == {"live-updates"}


def test_only_image_changed_still_routes_to_house_status() -> None:
    """Direct assertion on the routing table: IMAGE_CHANGED is the
    only event type that still targets #house-status."""

    router = DiscordOutputRouter(bot=None)

    destinations = router._destinations(
        ProductionEvent(
            source="HouseImage",
            event_type=EventType.IMAGE_CHANGED,
            title="HOUSE STATUS IMAGE UPDATED",
            detail="changed",
            severity=EventSeverity.NOTICE,
        )
    )
    names = {name for _, name in destinations}

    assert "house-status" in names


def test_production_symptom_reproduction_hoh_and_competition_winner_never_reach_house_status(
    monkeypatch,
) -> None:
    """Direct reproduction of the reported production symptom: a
    'Head of Household Changed: Yash -> Kamu' event and a separate
    'Competition Winner: Kamu' event must both reach #live-updates
    only, never #house-status -- with a real IMAGE_CHANGED event
    published in the same batch still correctly reaching
    #house-status, proving the fix doesn't over-correct and starve
    the channel entirely."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    router = DiscordOutputRouter(FakeBot([house, live]))
    monkeypatch.setattr(router, "_download", _stub_download)

    hoh_event = ProductionEvent(
        source="HouseStatus",
        event_type=EventType.HOH_CHANGED,
        title="Head of Household Changed",
        detail="Yash → Kamu",
        severity=EventSeverity.IMPORTANT,
    )
    competition_event = ProductionEvent(
        source="Competition",
        event_type=EventType.COMPETITION_WINNER,
        title="Competition Winner",
        detail="Kamu",
    )
    image_event = ProductionEvent(
        source="HouseImage",
        event_type=EventType.IMAGE_CHANGED,
        title="HOUSE STATUS IMAGE UPDATED",
        detail="JokersUpdates house-status image changed after episode air.",
        severity=EventSeverity.NOTICE,
        metadata={"url": "http://www.jokersupdates.com/x/house.png"},
    )

    asyncio.run(router.publish(hoh_event))
    asyncio.run(router.publish(competition_event))
    asyncio.run(router.publish(image_event))

    assert len(house.messages) == 1  # only the image event
    assert house.messages[0]["embed"].title == "🏠 HOUSE STATUS UPDATED"
    assert len(live.messages) == 3  # all three events


def test_duplicate_channel_configuration_only_sends_once(monkeypatch) -> None:
    """When two destinations (house-status, live-updates) are
    configured to the SAME real channel ID, the router must send to
    that physical channel once, not twice -- and both destinations
    count as delivered, so the call succeeds without raising.

    IMAGE_CHANGED is the only remaining event type routed to both
    house-status and live-updates (see _destinations()), so it's the
    one used here to exercise a genuine shared-channel scenario.
    """

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 42)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 42)

    channel = FakeChannel(42, "shared-channel")
    router = DiscordOutputRouter(FakeBot([channel]))

    event = ProductionEvent(
        source="HouseImage",
        event_type=EventType.IMAGE_CHANGED,
        title="HOUSE STATUS IMAGE UPDATED",
        detail="changed",
        severity=EventSeverity.NOTICE,
        metadata={"url": "http://www.jokersupdates.com/x/house.png"},
    )

    async def failed_download(url):
        return None

    monkeypatch.setattr(router, "_download", failed_download)

    asyncio.run(router.publish(event))

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
# Hamsterwatch NEW vs UPDATED presentation (metadata["is_new"], see
# production/hamsterwatch.py HamsterwatchMonitor._build_event())
# ==========================================================


def test_hamsterwatch_first_announcement_uses_update_title(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    channel = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([channel]))

    asyncio.run(router.publish(_hamsterwatch_event(is_new=True)))

    embed = channel.messages[0]["embed"]
    assert embed.title == "🐹 HAMSTERWATCH UPDATE — Day 37"


def test_hamsterwatch_substantial_edit_uses_updated_title(monkeypatch) -> None:
    """A re-announcement of a section Julie already archived (a real
    content edit, not a duplicate) must render distinguishably from a
    first-time announcement -- see production/hamsterwatch.py's
    UpsertOutcome.is_new."""

    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    channel = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([channel]))

    asyncio.run(router.publish(_hamsterwatch_event(is_new=False)))

    embed = channel.messages[0]["embed"]
    assert embed.title == "🐹 HAMSTERWATCH UPDATED — Day 37"


def test_hamsterwatch_missing_is_new_metadata_defaults_to_update_title(monkeypatch) -> None:
    """Backward compatibility: an event persisted before metadata["is_new"]
    existed (e.g. recovered from A3 pending-event durability) must not
    crash and must render exactly as it did before this feature --
    the original "UPDATE" wording, not "UPDATED"."""

    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    channel = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([channel]))

    event = _hamsterwatch_event()
    assert "is_new" not in event.metadata

    asyncio.run(router.publish(event))

    embed = channel.messages[0]["embed"]
    assert embed.title == "🐹 HAMSTERWATCH UPDATE — Day 37"


def test_hamsterwatch_multi_day_updated_batch_uses_updated_recaps_wording(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    channel = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([channel]))

    asyncio.run(router.publish(_hamsterwatch_event(count=2, is_new=False)))

    embed = channel.messages[0]["embed"]
    assert embed.title == "🐹 HAMSTERWATCH UPDATED — 2 updated recaps"


# ==========================================================
# Per-destination delivery tracking (partial multi-destination
# failure must not duplicate successful destinations or silently
# drop failed ones)
# ==========================================================


def _multi_destination_event() -> ProductionEvent:
    """An event routed to two destinations (house-status,
    live-updates). IMAGE_CHANGED is the only event type still routed
    to both (see _destinations() in services/discord_output.py) --
    structured game-state events like HOH_CHANGED route to
    live-updates only, so they no longer exercise multi-destination
    delivery tracking."""

    return ProductionEvent(
        source="HouseImage",
        event_type=EventType.IMAGE_CHANGED,
        title="HOUSE STATUS IMAGE UPDATED",
        detail="changed",
        severity=EventSeverity.NOTICE,
        metadata={"url": "http://www.jokersupdates.com/x/house.png"},
    )


async def _stub_download(url: str) -> None:
    """Deterministic no-network stand-in for these delivery-tracking
    tests -- they exercise destination routing/retry semantics, not
    image handling, so a real network call must never be involved.
    Either outcome (hit or miss) still sends exactly one message per
    destination attempt; returning None (a "miss") is simplest."""

    return None


def test_success_then_failure_retry_does_not_resend_succeeded_destination(monkeypatch) -> None:
    """Destination A (house-status) succeeds, destination B
    (live-updates) fails. On retry, A must not be sent again, and B
    must be attempted again and eventually succeed."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FlakyChannel(2, "live-updates", fail_times=1)
    router = DiscordOutputRouter(FakeBot([house, live]))
    monkeypatch.setattr(router, "_download", _stub_download)

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
    monkeypatch.setattr(router, "_download", _stub_download)

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
    monkeypatch.setattr(router, "_download", _stub_download)

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
    monkeypatch.setattr(router, "_download", _stub_download)

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
    monkeypatch.setattr(router, "_download", _stub_download)

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
    monkeypatch.setattr(router, "_download", _stub_download)

    event = _multi_destination_event()

    with pytest.raises(RuntimeError):
        asyncio.run(router.publish(event))

    asyncio.run(router.publish(event))  # retry

    assert len(house.messages) == 1, "successful destination must not be resent on retry"


# ==========================================================
# Competition event routing (A3 incident: COMPETITION_* used to
# target a "production" destination with no real deployed Discord
# channel behind it -- see config.py PRODUCTION_CHANNEL, which has
# no hardcoded default unlike LIVE_UPDATES_CHANNEL/HOUSE_STATUS_CHANNEL.
# Competition results must reach only real, deployed channels.)
# ==========================================================


def _competition_event(event_type: EventType) -> ProductionEvent:
    return ProductionEvent(
        source="Competition",
        event_type=event_type,
        title="Competition Winner",
        detail="Yash",
        severity=EventSeverity.IMPORTANT,
    )


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.COMPETITION_STARTED,
        EventType.COMPETITION_FINISHED,
        EventType.COMPETITION_CHANGED,
        EventType.COMPETITION_WINNER,
    ],
)
def test_competition_events_route_to_live_updates_only(
    event_type, monkeypatch
) -> None:
    """Competition events must reach live-updates -- never a
    "production" destination (never existed in the deployed Discord
    server), and never #house-status either: that channel is reserved
    for the actual House Status image (IMAGE_CHANGED). This is the
    routing-regression fix -- competition results (e.g. "Competition
    Winner") were previously also posted to #house-status."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    router = DiscordOutputRouter(FakeBot([house, live]))

    asyncio.run(router.publish(_competition_event(event_type)))  # must not raise

    assert len(house.messages) == 0
    assert len(live.messages) == 1


def test_competition_event_destinations_never_include_production_or_house_status() -> None:
    """Direct assertion on the routing table itself: no destination
    named "production" or "house-status" is ever computed for a
    competition event, regardless of what a bot happens to have
    channels for."""

    router = DiscordOutputRouter(bot=None)

    for event_type in (
        EventType.COMPETITION_STARTED,
        EventType.COMPETITION_FINISHED,
        EventType.COMPETITION_CHANGED,
        EventType.COMPETITION_WINNER,
    ):
        destinations = router._destinations(_competition_event(event_type))
        names = {name for _, name in destinations}
        assert "production" not in names
        assert "house-status" not in names
        assert names == {"live-updates"}


def test_competition_event_no_longer_permanently_blocks_the_announcement_queue(
    monkeypatch,
) -> None:
    """Reproduces the actual production incident end-to-end at the
    router level: previously, a competition event's "production" leg
    could never resolve (no such channel exists), which made
    publish() raise every single time -- and since
    ProductionEngine.announce() requeues a failed event at the front
    of pending_events and stops for that tick (see
    production/engine.py), nothing queued behind a competition event
    was ever attempted again. With competition events now routed only
    to real channels, publish() succeeds and no longer blocks
    anything queued after it."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    # Deliberately no channel named/ID'd "production" anywhere on this
    # bot -- matching the real deployed Discord server exactly.
    router = DiscordOutputRouter(FakeBot([house, live]))

    competition_event = _competition_event(EventType.COMPETITION_WINNER)
    later_event = make_event()  # a later, unrelated RSS_UPDATE event

    asyncio.run(router.publish(competition_event))  # must not raise
    asyncio.run(router.publish(later_event))  # never reached before the fix

    assert len(live.messages) == 2  # both events reached live-updates
    assert len(house.messages) == 0  # competition events do not target house-status


# ==========================================================
# RSS (IMG) live-feed image attachment (separate from, and must not
# affect, the House Status image system above)
# ==========================================================

_FAKE_JPEG = b"\xff\xd8\xff" + b"\x00" * 32  # passes the magic-number sniff
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _rss_event_with_image(image_url: str = "https://example.test/photo.jpg") -> ProductionEvent:
    event = make_event()
    event.metadata["image_url"] = image_url
    return event


def test_rss_update_without_image_url_sends_text_only(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    asyncio.run(router.publish(make_event()))  # no image_url in metadata at all

    assert len(live.messages) == 1
    assert live.messages[0].get("file") is None
    assert live.messages[0]["embed"].image.url is None or not live.messages[0]["embed"].image.url


def test_rss_update_with_image_url_attempts_download_and_attaches(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    downloaded = {}

    async def fake_download(url, **kwargs):
        downloaded["url"] = url
        return _FAKE_JPEG

    monkeypatch.setattr(router, "_download", fake_download)

    event = _rss_event_with_image("https://example.test/photo.jpg")
    asyncio.run(router.publish(event))

    assert downloaded["url"] == "https://example.test/photo.jpg"
    assert len(live.messages) == 1
    assert live.messages[0]["file"] is not None
    assert live.messages[0]["embed"].image.url == "attachment://live_feed_image.jpg"
    # The existing text/embed fields (title, detail, source, published)
    # must be completely unaffected.
    assert live.messages[0]["embed"].description == "Mallory & Melody in Pod BR."
    assert live.messages[0]["embed"].title == "🟦 LIVE FEED UPDATE"


def test_rss_update_image_filename_matches_detected_format(monkeypatch) -> None:
    """A PNG payload gets a .png attachment filename, not a hardcoded
    .jpg -- verifies the format is sniffed, not assumed."""

    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    async def fake_download(url, **kwargs):
        return _FAKE_PNG

    monkeypatch.setattr(router, "_download", fake_download)

    asyncio.run(router.publish(_rss_event_with_image()))

    assert live.messages[0]["embed"].image.url == "attachment://live_feed_image.png"


def test_rss_update_image_download_failure_still_sends_text(monkeypatch) -> None:
    """The optional image failing must not lose the event or prevent
    the text update from posting -- and unlike House Status, must NOT
    fall back to hot-linking the (unverified, third-party) URL."""

    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    async def failing_download(url, **kwargs):
        return None

    monkeypatch.setattr(router, "_download", failing_download)

    event = _rss_event_with_image()
    asyncio.run(router.publish(event))  # must not raise

    assert len(live.messages) == 1
    assert live.messages[0].get("file") is None
    embed = live.messages[0]["embed"]
    assert not embed.image.url  # no hotlink fallback for RSS images
    assert event.delivered_to == {"live-updates"}  # event itself is NOT lost


def test_rss_update_oversized_image_is_rejected(monkeypatch) -> None:
    """Exercises the real _download() (not a stub), proving the size
    cap is actually enforced, not just documented."""

    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    class HugeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self, n=-1):
            return self._payload[:n] if n and n > 0 else self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    oversized = _FAKE_JPEG + b"\x00" * discord_output_module._MAX_IMAGE_BYTES

    monkeypatch.setattr(
        "services.discord_output.urlopen",
        lambda request, timeout=None: HugeResponse(oversized),
    )

    asyncio.run(router.publish(_rss_event_with_image()))

    assert live.messages[0].get("file") is None  # rejected, fell back to text-only


def test_rss_update_non_image_response_is_rejected(monkeypatch) -> None:
    """Exercises the real _download(): an HTML error page (or any
    non-image response) must never become a Discord attachment."""

    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    class HtmlResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self, n=-1):
            return self._payload[:n] if n and n > 0 else self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "services.discord_output.urlopen",
        lambda request, timeout=None: HtmlResponse(b"<html>404 Not Found</html>"),
    )

    asyncio.run(router.publish(_rss_event_with_image()))

    assert live.messages[0].get("file") is None


def _rss_event_with_images(image_urls: list[str]) -> ProductionEvent:
    event = make_event()
    event.metadata["image_urls"] = image_urls
    return event


def test_rss_update_with_multiple_images_attaches_all(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    downloaded = []

    async def fake_download(url, **kwargs):
        downloaded.append(url)
        return _FAKE_JPEG if len(downloaded) == 1 else _FAKE_PNG

    monkeypatch.setattr(router, "_download", fake_download)

    event = _rss_event_with_images(
        ["https://i.imgur.com/one.jpg", "https://i.imgur.com/two.jpg"]
    )
    asyncio.run(router.publish(event))

    assert downloaded == ["https://i.imgur.com/one.jpg", "https://i.imgur.com/two.jpg"]
    assert len(live.messages) == 1
    files = live.messages[0]["files"]
    assert len(files) == 2
    assert files[0].filename == "live_feed_image_1.jpg"
    assert files[1].filename == "live_feed_image_2.png"
    assert live.messages[0]["embed"].image.url == "attachment://live_feed_image_1.jpg"


def test_rss_update_one_of_multiple_images_fails_posts_the_rest(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    async def fake_download(url, **kwargs):
        if url == "https://i.imgur.com/bad.jpg":
            return None
        return _FAKE_JPEG

    monkeypatch.setattr(router, "_download", fake_download)

    event = _rss_event_with_images(
        ["https://i.imgur.com/good.jpg", "https://i.imgur.com/bad.jpg"]
    )
    asyncio.run(router.publish(event))

    assert len(live.messages) == 1
    assert live.messages[0].get("file") is not None
    assert live.messages[0].get("files") is None
    # Numbered by the image's original position in the post (there were
    # two URLs, the first succeeded), not renumbered down to "1 of 1
    # successful" -- so a filename always refers to the same source
    # image regardless of which other images in the same post failed.
    assert live.messages[0]["file"].filename == "live_feed_image_1.jpg"


def test_rss_update_all_of_multiple_images_fail_sends_text_only(monkeypatch) -> None:
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    async def failing_download(url, **kwargs):
        return None

    monkeypatch.setattr(router, "_download", failing_download)

    event = _rss_event_with_images(
        ["https://i.imgur.com/one.jpg", "https://i.imgur.com/two.jpg"]
    )
    asyncio.run(router.publish(event))

    assert len(live.messages) == 1
    assert live.messages[0].get("file") is None
    assert live.messages[0].get("files") is None
    assert not live.messages[0]["embed"].image.url


def test_rss_update_image_urls_takes_priority_over_legacy_image_url(monkeypatch) -> None:
    """When both keys are present (shouldn't normally happen, but
    proves the precedence), the new list wins."""

    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    live = FakeChannel(1, "live-updates")
    router = DiscordOutputRouter(FakeBot([live]))

    downloaded = []

    async def fake_download(url, **kwargs):
        downloaded.append(url)
        return _FAKE_JPEG

    monkeypatch.setattr(router, "_download", fake_download)

    event = make_event()
    event.metadata["image_url"] = "https://example.test/legacy.jpg"
    event.metadata["image_urls"] = ["https://i.imgur.com/new.jpg"]

    asyncio.run(router.publish(event))

    assert downloaded == ["https://i.imgur.com/new.jpg"]


def test_house_status_image_behavior_is_unaffected_by_rss_image_attachment(
    monkeypatch,
) -> None:
    """Regression guard: the House Status image path (hotlink fallback
    on failure, fixed "house_status.png" filename) must be byte-for-
    byte the same after generalizing _send()/_download() for RSS
    images."""

    monkeypatch.setattr("services.discord_output.HOUSE_STATUS_CHANNEL", 0)
    monkeypatch.setattr("services.discord_output.LIVE_UPDATES_CHANNEL", 0)

    house = FakeChannel(1, "house-status")
    live = FakeChannel(2, "live-updates")
    router = DiscordOutputRouter(FakeBot([house, live]))

    async def fake_download(url, **kwargs):
        return b"fake-png-bytes"  # deliberately NOT a real magic number

    monkeypatch.setattr(router, "_download", fake_download)

    event = ProductionEvent(
        source="HouseImage",
        event_type=EventType.IMAGE_CHANGED,
        title="HOUSE STATUS IMAGE UPDATED",
        detail="changed",
        severity=EventSeverity.NOTICE,
        metadata={"url": "http://www.jokersupdates.com/x/house.png"},
    )

    asyncio.run(router.publish(event))

    assert house.messages[0]["embed"].image.url == "attachment://house_status.png"
    assert house.messages[0]["file"] is not None
