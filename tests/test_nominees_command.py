"""Tests for /nominees and its short alias /noms (commands/nominees.py).

Both commands must share exactly one implementation -- see
commands/nominees.py's module docstring -- so these tests prove that
directly: same data source, same output, same "no nominees yet"
wording, same permissions, for both command names, and that both
literally invoke the same shared function rather than two independent
copies that merely happen to look alike today.

Data source is the official facts record (KnowledgeStore STATE topic
"NOMINEES"), never the automated HouseStatus -- see commands/
nominees.py's module docstring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

import commands.nominees as nominees_module
from database.storage import Storage
from production.knowledge import KnowledgeStore, KnowledgeType


class FakeInteraction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=42)
        self.user.__str__ = lambda: "Tester#0001"  # type: ignore[method-assign]
        self.sent: list[str] = []

        async def send_message(message, *args, **kwargs):
            self.sent.append(message)

        self.response = SimpleNamespace(send_message=send_message)


class _FakeStateItem:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeKnowledgeStore:
    """Mutable stand-in for KnowledgeStore.active_state("NOMINEES") --
    mutating .nominees_content and re-reading through active_state()
    is what test_nominees_and_noms_use_the_same_data_source relies on
    to prove both commands read the same live object."""

    def __init__(self, nominees_content: str | None) -> None:
        self.nominees_content = nominees_content

    def active_state(self, topic: str):
        assert topic == "NOMINEES"
        if self.nominees_content is None:
            return None
        return _FakeStateItem(self.nominees_content)


def _register(nominees_content: str | None):
    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)

    knowledge = _FakeKnowledgeStore(nominees_content)
    engine = SimpleNamespace(knowledge=knowledge)
    ds.scheduler = SimpleNamespace(engine=engine)

    nominees_module.register(ds)
    return ds.bot.tree, knowledge


# ==========================================================
# Registration: exactly one of each
# ==========================================================


def test_nominees_command_registered_exactly_once() -> None:
    tree, _ = _register(None)
    matches = [c for c in tree.get_commands() if c.name == "nominees"]
    assert len(matches) == 1


def test_noms_command_registered_exactly_once() -> None:
    tree, _ = _register(None)
    matches = [c for c in tree.get_commands() if c.name == "noms"]
    assert len(matches) == 1


# ==========================================================
# Equivalent output for both commands
# ==========================================================


def test_nominees_and_noms_produce_equivalent_output_with_nominees() -> None:
    tree, _ = _register("Alex, Jordan")

    nominees_interaction = FakeInteraction()
    asyncio.run(tree.get_command("nominees").callback(nominees_interaction))

    noms_interaction = FakeInteraction()
    asyncio.run(tree.get_command("noms").callback(noms_interaction))

    assert nominees_interaction.sent == noms_interaction.sent
    assert "Alex" in nominees_interaction.sent[0]
    assert "Jordan" in nominees_interaction.sent[0]


def test_nominees_and_noms_produce_equivalent_output_with_no_nominees() -> None:
    tree, _ = _register(None)

    nominees_interaction = FakeInteraction()
    asyncio.run(tree.get_command("nominees").callback(nominees_interaction))

    noms_interaction = FakeInteraction()
    asyncio.run(tree.get_command("noms").callback(noms_interaction))

    assert nominees_interaction.sent == noms_interaction.sent
    assert "No nominees" in nominees_interaction.sent[0]


# ==========================================================
# Both commands genuinely share one implementation, not two
# independently-maintained copies that happen to match today
# ==========================================================


def test_nominees_and_noms_invoke_the_same_shared_function(monkeypatch) -> None:
    """Patches the one shared function commands/nominees.py exposes
    and proves BOTH command names route through it -- if either
    command had its own copy-pasted logic instead, only one of the two
    calls below would show up in `calls`."""

    tree, _ = _register(None)

    calls: list[str] = []

    async def spy(interaction, discord_service, command_name) -> None:
        calls.append(command_name)
        await interaction.response.send_message(f"spy:{command_name}")

    monkeypatch.setattr(nominees_module, "_show_nominees", spy)

    asyncio.run(tree.get_command("nominees").callback(FakeInteraction()))
    asyncio.run(tree.get_command("noms").callback(FakeInteraction()))

    assert calls == ["nominees", "noms"]


def test_nominees_and_noms_use_the_same_data_source() -> None:
    """Changing the tracked house status must be reflected identically
    by both commands -- proving they read the same live object, not a
    snapshot or a second copy."""

    tree, knowledge = _register("Alex")

    first_nominees = FakeInteraction()
    asyncio.run(tree.get_command("nominees").callback(first_nominees))
    first_noms = FakeInteraction()
    asyncio.run(tree.get_command("noms").callback(first_noms))
    assert first_nominees.sent == first_noms.sent
    assert "Alex" in first_nominees.sent[0]

    knowledge.nominees_content = "Taylor, Morgan"

    second_nominees = FakeInteraction()
    asyncio.run(tree.get_command("nominees").callback(second_nominees))
    second_noms = FakeInteraction()
    asyncio.run(tree.get_command("noms").callback(second_noms))
    assert second_nominees.sent == second_noms.sent
    assert "Taylor" in second_nominees.sent[0]
    assert "Morgan" in second_nominees.sent[0]


# ==========================================================
# Equivalent permissions
# ==========================================================


def test_nominees_and_noms_have_equivalent_permissions() -> None:
    tree, _ = _register(None)

    nominees_cmd = tree.get_command("nominees")
    noms_cmd = tree.get_command("noms")

    assert nominees_cmd.default_permissions == noms_cmd.default_permissions
    # /nominees has always been open to everyone -- /noms must match.
    assert nominees_cmd.default_permissions is None


# ==========================================================
# Weekly boundary: nominees taught for a PREVIOUS reporting week must
# not survive as "current" once a new week has started -- see
# production/knowledge.py KnowledgeStore.current_state(). This is the
# production bug this closes: stale Week 7 nominees (Drew, LaLa,
# Taylor) being served forever as "confirmed."
# ==========================================================


def test_nominees_reports_none_confirmed_for_a_value_taught_last_week(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    knowledge = KnowledgeStore(storage=Storage())
    knowledge.teach(
        KnowledgeType.STATE, "Drew, LaLa, Taylor", author_id=1, topic="NOMINEES"
    )
    knowledge.start_new_week(9)

    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)
    ds.scheduler = SimpleNamespace(engine=SimpleNamespace(knowledge=knowledge))
    nominees_module.register(ds)

    interaction = FakeInteraction()
    asyncio.run(ds.bot.tree.get_command("nominees").callback(interaction))

    assert "No nominees" in interaction.sent[0]
    assert "Drew" not in interaction.sent[0]


def test_nominees_reports_the_value_re_taught_this_week(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    knowledge = KnowledgeStore(storage=Storage())
    knowledge.teach(
        KnowledgeType.STATE, "Drew, LaLa, Taylor", author_id=1, topic="NOMINEES"
    )
    knowledge.start_new_week(9)
    knowledge.teach(
        KnowledgeType.STATE, "Angela, Dee, Devens", author_id=1, topic="NOMINEES"
    )

    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)
    ds.scheduler = SimpleNamespace(engine=SimpleNamespace(knowledge=knowledge))
    nominees_module.register(ds)

    interaction = FakeInteraction()
    asyncio.run(ds.bot.tree.get_command("nominees").callback(interaction))

    assert "Angela" in interaction.sent[0]
    assert "Devens" in interaction.sent[0]
    assert "Drew" not in interaction.sent[0]
