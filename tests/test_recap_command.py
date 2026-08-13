"""Integration tests for the /recap slash command wiring.

Verifies commands/recap.py actually gathers game state, recent
Joker's Updates, and a relevant slice of Hamsterwatch history (via
player-name keyword retrieval, not the whole archive), and hands them
all to services.ai_service.generate_recap with clear provenance.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

import commands.recap as recap_module
from database.hamsterwatch_archive import HamsterwatchArchive
from production.competition import CompetitionState
from production.house_status import HouseStatus


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


class FakeHouseStatusMonitor:
    def __init__(self, current: HouseStatus) -> None:
        self.current = current


class FakeCompetitionMonitor:
    def __init__(self, current: CompetitionState) -> None:
        self.current = current


class FakeWatcher:
    def __init__(self, house_status: HouseStatus, competition: CompetitionState) -> None:
        self.house_status = FakeHouseStatusMonitor(house_status)
        self.competition = FakeCompetitionMonitor(competition)


class FakeEngine:
    def __init__(self, entries: list[str], house_status: HouseStatus, competition: CompetitionState) -> None:
        self._entries = entries
        self.watcher = FakeWatcher(house_status, competition)

    def recent_updates(self, limit: int = 20) -> list[str]:
        return self._entries[-limit:]


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


def test_recap_gathers_game_state_entries_and_relevant_hamsterwatch(monkeypatch, tmp_path):
    archive = HamsterwatchArchive(db_path=tmp_path / "archive.db")
    archive.upsert(
        page_url="http://hamsterwatch.com/bb28/081026.shtml",
        section_slug="day-37",
        heading="Day 37 - Wednesday - August 12, 2026",
        article_date="2026-08-12",
        bb_day=37,
        content=(
            "LaLa and Devens talked strategy in the HOH room about who to "
            "target next week if the veto isn't used."
        ),
        summary="LaLa and Devens talked strategy.",
    )
    # An irrelevant older article that should not be pulled in ahead of
    # the one that actually mentions a tracked player.
    archive.upsert(
        page_url="http://hamsterwatch.com/bb28/070926.shtml",
        section_slug="day-3",
        heading="Day 3 - Thursday - July 9, 2026",
        article_date="2026-07-09",
        bb_day=3,
        content="Move-in day chaos with no mention of current nominees at all.",
        summary="Move-in day chaos.",
    )
    monkeypatch.setattr(recap_module, "HamsterwatchArchive", lambda: archive)

    captured: dict = {}

    async def fake_generate_recap(entries, *, game_state="", hamsterwatch_entries=None):
        captured["entries"] = entries
        captured["game_state"] = game_state
        captured["hamsterwatch_entries"] = hamsterwatch_entries
        return "Julie's recap text"

    monkeypatch.setattr(recap_module, "generate_recap", fake_generate_recap)

    house_status = HouseStatus(hoh="LaLa", nominees=("Devens", "Kamu"))
    engine = FakeEngine(
        ["Kamu went to the DR.", "Feeds cut briefly."],
        house_status,
        CompetitionState(),
    )
    cmd = _register(engine)
    interaction = FakeInteraction()

    asyncio.run(cmd.callback(interaction))

    assert interaction.response.deferred is True
    assert captured["entries"] == ["Kamu went to the DR.", "Feeds cut briefly."]
    assert "LaLa" in captured["game_state"]
    assert "Devens" in captured["game_state"]

    # Both archived articles are short enough that recency backfill
    # (find_relevant fills unused slots with recent articles) pulls in
    # the irrelevant one too — what matters is that the actually
    # relevant one (mentions the tracked HOH/nominee) is present, and
    # ranked ahead of the unrelated one.
    entries = captured["hamsterwatch_entries"]
    assert any("Day 37" in entry and "LaLa and Devens talked strategy" in entry for entry in entries)
    assert entries[0].startswith("[Day 37")

    embed = interaction.followup.sent[0]["embed"]
    assert embed.description == "Julie's recap text"
    assert "Joker's Update" in embed.footer.text
    assert "Hamsterwatch" in embed.footer.text


def test_recap_hamsterwatch_entries_are_bounded_not_the_whole_archive(monkeypatch, tmp_path):
    archive = HamsterwatchArchive(db_path=tmp_path / "archive.db")
    for day in range(1, 21):
        archive.upsert(
            page_url="http://hamsterwatch.com/bb28/page.shtml",
            section_slug=f"day-{day}",
            heading=f"Day {day} recap",
            article_date=None,
            bb_day=day,
            content=f"Generic recap content for day {day}, no tracked names.",
            summary="s",
        )
    monkeypatch.setattr(recap_module, "HamsterwatchArchive", lambda: archive)

    captured: dict = {}

    async def fake_generate_recap(entries, *, game_state="", hamsterwatch_entries=None):
        captured["hamsterwatch_entries"] = hamsterwatch_entries
        return "recap"

    monkeypatch.setattr(recap_module, "generate_recap", fake_generate_recap)

    engine = FakeEngine([], HouseStatus(), CompetitionState())
    cmd = _register(engine)

    asyncio.run(cmd.callback(FakeInteraction()))

    assert 0 < len(captured["hamsterwatch_entries"]) <= recap_module.HAMSTERWATCH_CONTEXT_LIMIT
    assert len(captured["hamsterwatch_entries"]) < archive.count()


def test_recap_works_when_no_hamsterwatch_history_exists(monkeypatch, tmp_path):
    archive = HamsterwatchArchive(db_path=tmp_path / "archive.db")
    monkeypatch.setattr(recap_module, "HamsterwatchArchive", lambda: archive)

    captured: dict = {}

    async def fake_generate_recap(entries, *, game_state="", hamsterwatch_entries=None):
        captured["hamsterwatch_entries"] = hamsterwatch_entries
        return "recap"

    monkeypatch.setattr(recap_module, "generate_recap", fake_generate_recap)

    engine = FakeEngine(["An update."], HouseStatus(), CompetitionState())
    cmd = _register(engine)
    interaction = FakeInteraction()

    asyncio.run(cmd.callback(interaction))

    assert captured["hamsterwatch_entries"] == []
    embed = interaction.followup.sent[0]["embed"]
    assert "Hamsterwatch" not in (embed.footer.text or "")
