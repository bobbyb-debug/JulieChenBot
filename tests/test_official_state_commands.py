"""Tests for /hoh and /veto reading exclusively from official facts
(KnowledgeStore STATE items), never the automated HouseStatus -- see
commands/hoh.py and commands/veto.py module docstrings. /nominees and
/noms have their own dedicated coverage in test_nominees_command.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

import commands.hoh as hoh_module
import commands.veto as veto_module


class FakeInteraction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=42, __str__=lambda self: "Tester#0001")
        self.sent: list[str] = []

        async def send_message(message, *args, **kwargs):
            self.sent.append(message)

        self.response = SimpleNamespace(send_message=send_message)


class _FakeStateItem:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeKnowledgeStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    def active_state(self, topic: str):
        content = self._values.get(topic)
        return None if content is None else _FakeStateItem(content)


def _register(module, values: dict[str, str] | None = None):
    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)
    engine = SimpleNamespace(knowledge=_FakeKnowledgeStore(values))
    ds.scheduler = SimpleNamespace(engine=engine)
    module.register(ds)
    return ds.bot.tree


# ==========================================================
# /hoh
# ==========================================================


def test_hoh_reports_official_value() -> None:
    tree = _register(hoh_module, {"HOH": "Yash"})
    interaction = FakeInteraction()
    asyncio.run(tree.get_command("hoh").callback(interaction))

    assert "Yash" in interaction.sent[0]


def test_hoh_reports_not_confirmed_when_untaught() -> None:
    tree = _register(hoh_module, {})
    interaction = FakeInteraction()
    asyncio.run(tree.get_command("hoh").callback(interaction))

    assert "No Head of Household" in interaction.sent[0]


# ==========================================================
# /veto
# ==========================================================


def test_veto_reports_official_holder_and_used_state() -> None:
    tree = _register(veto_module, {"VETO_WINNER": "Barrett", "VETO_USED": "yes"})
    interaction = FakeInteraction()
    asyncio.run(tree.get_command("veto").callback(interaction))

    assert "Barrett" in interaction.sent[0]
    assert "(used)" in interaction.sent[0]


def test_veto_reports_not_used_when_taught_no() -> None:
    tree = _register(veto_module, {"VETO_WINNER": "Barrett", "VETO_USED": "no"})
    interaction = FakeInteraction()
    asyncio.run(tree.get_command("veto").callback(interaction))

    assert "(not yet used)" in interaction.sent[0]


def test_veto_omits_used_clause_when_untaught() -> None:
    """VETO_USED has no dedicated teach shortcut -- if it was never
    taught, /veto must not guess at a used/not-used state."""

    tree = _register(veto_module, {"VETO_WINNER": "Barrett"})
    interaction = FakeInteraction()
    asyncio.run(tree.get_command("veto").callback(interaction))

    assert "Barrett" in interaction.sent[0]
    assert "used" not in interaction.sent[0].lower()


def test_veto_reports_not_confirmed_when_untaught() -> None:
    tree = _register(veto_module, {})
    interaction = FakeInteraction()
    asyncio.run(tree.get_command("veto").callback(interaction))

    assert "No Power of Veto winner" in interaction.sent[0]
