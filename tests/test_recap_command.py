"""Integration tests for the /recap slash command wiring.

/recap's only job is: take engine.recent_updates() (already RSS-only,
already time-windowed -- see production/engine.py) and hand it to
services.ai_service.generate_recap with nothing else mixed in. These
tests verify commands/recap.py does exactly that and nothing more --
no game state, no Hamsterwatch, no taught knowledge is gathered or
passed, and a FakeEngine exposing only recent_updates() is sufficient
proof of that decoupling.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

import commands.recap as recap_module


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False

    async def defer(self) -> None:
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, **kwargs) -> None:
        self.sent.append(kwargs)


class FakeInteraction:
    def __init__(self) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.user = SimpleNamespace(id=42)
        self.user.__str__ = lambda: "Tester#0001"  # type: ignore[method-assign]


class FakeEngine:
    """Deliberately exposes ONLY recent_updates() -- no watcher, no
    knowledge store, no Hamsterwatch archive. If commands/recap.py
    tried to reach for any of those, these tests would fail with an
    AttributeError rather than silently passing."""

    RECAP_WINDOW_HOURS = 24

    def __init__(self, entries: list[str]) -> None:
        self._entries = entries

    def recent_updates(self) -> list[str]:
        return list(self._entries)


class FakeDiscordService:
    def __init__(self, engine: FakeEngine) -> None:
        self.scheduler = SimpleNamespace(engine=engine)
        self.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())

    def command(self, *args, **kwargs):
        return self.bot.tree.command(*args, **kwargs)


def _register(engine: FakeEngine):
    ds = FakeDiscordService(engine)
    recap_module.register(ds)
    return ds.bot.tree.get_command("recap")


def test_recap_passes_recent_updates_straight_to_generate_recap(monkeypatch) -> None:
    captured: dict = {}

    async def fake_generate_recap(entries):
        captured["entries"] = entries
        return "Julie's recap text"

    monkeypatch.setattr(recap_module, "generate_recap", fake_generate_recap)

    engine = FakeEngine(["Kamu went to the DR.", "Feeds cut briefly."])
    cmd = _register(engine)
    interaction = FakeInteraction()

    asyncio.run(cmd.callback(interaction))

    assert interaction.response.deferred is True
    assert captured["entries"] == ["Kamu went to the DR.", "Feeds cut briefly."]

    embed = interaction.followup.sent[0]["embed"]
    assert embed.description == "Julie's recap text"
    assert embed.title == "📼 Recent Live-Feed Recap"
    assert "2 Joker's Update(s)" in embed.footer.text
    assert "24h" in embed.footer.text


def test_recap_with_no_recent_updates_skips_ai_call_entirely(monkeypatch) -> None:
    """No qualifying live-feed events -> a sensible, deterministic
    message, with no AI round-trip at all."""

    calls: list = []

    async def fake_generate_recap(entries):
        calls.append(entries)
        return "should not be reached"

    monkeypatch.setattr(recap_module, "generate_recap", fake_generate_recap)

    engine = FakeEngine([])
    cmd = _register(engine)
    interaction = FakeInteraction()

    asyncio.run(cmd.callback(interaction))

    assert calls == []  # generate_recap never called
    embed = interaction.followup.sent[0]["embed"]
    assert "No live-feed activity" in embed.description
    assert "24h" in embed.description
    assert embed.footer.text is None  # no footer when there's nothing to cite


def test_recap_works_with_an_engine_exposing_only_recent_updates(monkeypatch) -> None:
    """Structural proof of decoupling: commands/recap.py must not
    reach for watcher/knowledge/Hamsterwatch state at all -- FakeEngine
    here has no such attributes, so any such access would raise."""

    async def fake_generate_recap(entries):
        return "recap"

    monkeypatch.setattr(recap_module, "generate_recap", fake_generate_recap)

    engine = FakeEngine(["An update."])
    cmd = _register(engine)

    asyncio.run(cmd.callback(FakeInteraction()))  # must not raise
