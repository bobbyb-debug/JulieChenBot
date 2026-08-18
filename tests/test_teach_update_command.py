"""Tests for /teach update -- manual current-state updates that write
through the SAME HouseStatus object /hoh, /noms, /nominees, and /veto
already read (see production/state_sync.py), with provenance recorded
in KnowledgeStore and a later automated update able to supersede the
manual one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

import commands.hoh as hoh_module
import commands.nominees as nominees_module
import commands.teach as teach_module
import commands.veto as veto_module
from database.storage import Storage
from production.engine import ProductionEngine
from production.house_status import HouseStatus
from production.knowledge import KnowledgeType


def _engine(tmp_path: Path, monkeypatch) -> ProductionEngine:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    return ProductionEngine(storage=Storage())


def _discord_service(engine: ProductionEngine) -> SimpleNamespace:
    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)
    ds.scheduler = SimpleNamespace(engine=engine)
    return ds


class FakeMessage:
    def __init__(self) -> None:
        self.edited: list[dict] = []

    async def edit(self, **kwargs) -> None:
        self.edited.append(kwargs)


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []

    async def send_message(self, *args, **kwargs) -> None:
        self.sent.append({"args": args, "kwargs": kwargs})

    async def edit_message(self, *args, **kwargs) -> None:
        self.edited.append({"args": args, "kwargs": kwargs})


class FakeInteraction:
    def __init__(self, user_id: int = 111) -> None:
        # Defaults to "administrator" so every test that isn't
        # specifically about permissions doesn't need to set this up
        # itself -- the dedicated permission tests below override
        # guild_permissions/roles explicitly to exercise the
        # non-administrator paths.
        self.user = SimpleNamespace(
            id=user_id,
            __str__=lambda self: "TestMod#0001",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        self.response = FakeResponse()
        self._original_response = FakeMessage()

    async def original_response(self):
        return self._original_response


def _reply_text(interaction: FakeInteraction) -> str:
    return interaction.response.sent[0]["args"][0]


async def _run_update_and_confirm(
    engine: ProductionEngine, text: str, *, reason: str | None = None, user_id: int = 111
):
    ds = _discord_service(engine)
    teach_module.register(ds)
    teach_group = ds.bot.tree.get_command("teach")
    update_cmd = teach_group.get_command("update")

    preview_interaction = FakeInteraction(user_id=user_id)
    await update_cmd.callback(preview_interaction, text, reason)
    view = preview_interaction.response.sent[0]["kwargs"].get("view")

    if view is None:
        return preview_interaction, None

    confirm_interaction = FakeInteraction(user_id=user_id)
    await view.handle_confirm(confirm_interaction)
    return preview_interaction, confirm_interaction


# ==========================================================
# Registration / permissions
# ==========================================================


def test_update_subcommand_is_registered(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    teach_module.register(ds)
    group = ds.bot.tree.get_command("teach")

    assert group.get_command("update") is not None


def test_update_carries_no_default_permissions_restriction(
    tmp_path: Path, monkeypatch
) -> None:
    """See commands/teach.py module docstring: /teach update uses the
    same trusted-moderator tier as /teach batch, enforced explicitly
    in-callback rather than via Discord's default_permissions."""

    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    teach_module.register(ds)
    group = ds.bot.tree.get_command("teach")

    assert group.get_command("update").default_permissions is None


def test_non_administrator_without_moderator_role_cannot_update_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("commands.teach.TRUSTED_MODERATOR_ROLE_ID", 0)

    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    teach_module.register(ds)
    update_cmd = ds.bot.tree.get_command("teach").get_command("update")

    interaction = FakeInteraction()
    interaction.user.guild_permissions = SimpleNamespace(administrator=False)
    interaction.user.roles = []

    asyncio.run(update_cmd.callback(interaction, "HOH: Yash", None))

    assert engine.watcher.house_status.current.hoh == ""
    assert engine.knowledge.all_items() == []
    assert "embed" not in interaction.response.sent[0]["kwargs"]


def test_configured_moderator_role_can_update_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("commands.teach.TRUSTED_MODERATOR_ROLE_ID", 4242)

    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    teach_module.register(ds)
    update_cmd = ds.bot.tree.get_command("teach").get_command("update")

    interaction = FakeInteraction()
    interaction.user.guild_permissions = SimpleNamespace(administrator=False)
    interaction.user.roles = [SimpleNamespace(id=4242)]

    asyncio.run(update_cmd.callback(interaction, "HOH: Yash", None))

    assert "embed" in interaction.response.sent[0]["kwargs"]


# ==========================================================
# Preview / no writes before confirm
# ==========================================================


def test_update_preview_shows_planned_change(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    teach_module.register(ds)
    update_cmd = ds.bot.tree.get_command("teach").get_command("update")

    interaction = FakeInteraction()
    asyncio.run(update_cmd.callback(interaction, "HOH: Yash", None))

    embed = interaction.response.sent[0]["kwargs"]["embed"]
    assert "HOH" in embed.description
    assert "Yash" in embed.description
    assert engine.watcher.house_status.current.hoh == ""  # not applied yet


def test_update_with_no_lines_sends_plain_message(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    teach_module.register(ds)
    update_cmd = ds.bot.tree.get_command("teach").get_command("update")

    interaction = FakeInteraction()
    asyncio.run(update_cmd.callback(interaction, "   ", None))

    assert "view" not in interaction.response.sent[0]["kwargs"]
    assert engine.knowledge.all_items() == []


# ==========================================================
# Confirm applies HouseStatus immediately, structured commands see it
# ==========================================================


def test_confirm_applies_hoh_and_hoh_command_reflects_it(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_run_update_and_confirm(engine, "HOH: Yash"))

    assert engine.watcher.house_status.current.hoh == "Yash"

    ds = _discord_service(engine)
    hoh_module.register(ds)
    hoh_cmd = ds.bot.tree.get_command("hoh")
    interaction = FakeInteraction()
    asyncio.run(hoh_cmd.callback(interaction))

    assert "Yash" in _reply_text(interaction)


def test_confirm_applies_nominees_and_both_nominee_commands_reflect_it(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_run_update_and_confirm(engine, "Nominees: Angela, Dee"))

    assert engine.watcher.house_status.current.nominees == ("Angela", "Dee")

    ds = _discord_service(engine)
    nominees_module.register(ds)
    tree = ds.bot.tree

    for name in ("nominees", "noms"):
        interaction = FakeInteraction()
        asyncio.run(tree.get_command(name).callback(interaction))
        text = _reply_text(interaction)
        assert "Angela" in text and "Dee" in text


def test_confirm_applies_veto_winner_and_veto_command_reflects_it(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_run_update_and_confirm(engine, "VETO_WINNER: Barrett"))

    assert engine.watcher.house_status.current.veto_holder == "Barrett"
    assert engine.watcher.house_status.current.veto_used is False

    ds = _discord_service(engine)
    veto_module.register(ds)
    veto_cmd = ds.bot.tree.get_command("veto")
    interaction = FakeInteraction()
    asyncio.run(veto_cmd.callback(interaction))

    assert "Barrett" in _reply_text(interaction)


def test_confirm_persists_game_state(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_run_update_and_confirm(engine, "HOH: Yash"))

    persisted = engine.storage.get(engine.GAME_STATE_KEY)
    assert persisted is not None
    assert persisted["house_status"]["hoh"] == "Yash"


def test_confirm_survives_simulated_restart(tmp_path: Path, monkeypatch) -> None:
    engine_a = _engine(tmp_path, monkeypatch)
    asyncio.run(_run_update_and_confirm(engine_a, "HOH: Yash"))

    engine_b = ProductionEngine(storage=Storage())

    assert engine_b.watcher.house_status.current.hoh == "Yash"


# ==========================================================
# Automated update supersedes manual state
# ==========================================================


def test_automated_update_supersedes_manual_state(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_run_update_and_confirm(engine, "HOH: Yash"))
    assert engine.watcher.house_status.current.hoh == "Yash"

    # A real automated RSS-parsed update flows through the same
    # HouseStatusMonitor.update()/check() path production/engine.py's
    # tick() already uses.
    monitor = engine.watcher.house_status
    monitor.update(HouseStatus(hoh="Barrett", nominees=("Angela", "Dee")))
    result = asyncio.run(monitor.check())

    assert result.changed is True
    assert monitor.current.hoh == "Barrett"


# ==========================================================
# Provenance
# ==========================================================


def test_provenance_records_author_timestamp_and_reason(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(
        _run_update_and_confirm(
            engine, "HOH: Yash", reason="confirmed via live feed replay", user_id=555
        )
    )

    items = [
        item
        for item in engine.knowledge.all_items()
        if item.type == KnowledgeType.STATE and item.topic == "HOH"
    ]
    assert len(items) == 1
    item = items[0]
    assert item.author_id == 555
    assert item.note == "confirmed via live feed replay"
    assert item.content == "Yash"
    assert item.created_at is not None


def test_provenance_reason_is_optional(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_run_update_and_confirm(engine, "HOH: Yash", reason=None))

    item = next(
        i for i in engine.knowledge.all_items()
        if i.type == KnowledgeType.STATE and i.topic == "HOH"
    )
    assert item.note is None


# ==========================================================
# Unrecognized topics: recorded, but do not affect structured state
# ==========================================================


def test_unrecognized_topic_is_recorded_but_does_not_touch_house_status(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    preview_interaction, confirm_interaction = asyncio.run(
        _run_update_and_confirm(engine, "FAVORITE_SNACK: pretzels")
    )

    embed = preview_interaction.response.sent[0]["kwargs"]["embed"]
    unrecognized_field = next(
        f for f in embed.fields if "not linked" in f.name.lower()
    )
    assert "FAVORITE_SNACK" in unrecognized_field.value

    assert engine.watcher.house_status.current == HouseStatus()  # untouched
    assert engine.knowledge.active_state("FAVORITE_SNACK").content == "pretzels"


# ==========================================================
# Only the requesting moderator may confirm/cancel
# ==========================================================


def test_only_requester_can_confirm(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    teach_module.register(ds)
    update_cmd = ds.bot.tree.get_command("teach").get_command("update")

    interaction = FakeInteraction(user_id=111)
    asyncio.run(update_cmd.callback(interaction, "HOH: Yash", None))
    view = interaction.response.sent[0]["kwargs"]["view"]

    other = FakeInteraction(user_id=999)
    asyncio.run(view.handle_confirm(other))

    assert engine.watcher.house_status.current.hoh == ""
    assert engine.knowledge.all_items() == []
