"""Tests for /teach historical-hoh -- the Phase 1 administrator
workflow for recording verified structured historical HOH events.
Follows the exact registration/preview/confirm test pattern already
established in tests/test_teach_update_command.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

import commands.teach as teach_module
from database.storage import Storage
from production.engine import ProductionEngine


def _engine(tmp_path: Path, monkeypatch) -> ProductionEngine:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    from database.historical_events import HistoricalEventStore
    engine = ProductionEngine(storage=Storage())
    # Isolate the historical-events DB per test the same way Storage
    # is isolated above -- avoids one test's data leaking into another
    # via the default database/ path.
    engine.historical_events = HistoricalEventStore(
        db_path=tmp_path / "historical_events.db"
    )
    return engine


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

    async def send_message(self, *args, **kwargs) -> None:
        self.sent.append({"args": args, "kwargs": kwargs})

    async def edit_message(self, *args, **kwargs) -> None:
        self.sent.append({"args": args, "kwargs": kwargs, "edit": True})


class FakeInteraction:
    def __init__(self, user_id: int = 111) -> None:
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


def _get_command(ds):
    teach_module.register(ds)
    return ds.bot.tree.get_command("teach").get_command("historical-hoh")


async def _preview_and_confirm(
    engine, *, season=28, cycle=6, winner="Melody", source="manual test",
    week=6, excerpt=None, user_id=111,
):
    ds = _discord_service(engine)
    cmd = _get_command(ds)

    preview = FakeInteraction(user_id=user_id)
    await cmd.callback(preview, season, cycle, winner, source, week, excerpt)
    view = preview.response.sent[0]["kwargs"].get("view")

    if view is None:
        return preview, None

    confirm = FakeInteraction(user_id=user_id)
    await view.handle_confirm(confirm)
    return preview, confirm


# ==========================================================
# Registration / permissions
# ==========================================================


def test_historical_hoh_subcommand_is_registered(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    teach_module.register(ds)

    assert ds.bot.tree.get_command("teach").get_command("historical-hoh") is not None


def test_historical_hoh_carries_no_default_permissions_restriction(tmp_path, monkeypatch):
    """Same trusted-moderator tier as /teach update and /teach batch --
    enforced explicitly in-callback, not via Discord's
    default_permissions (see commands/teach.py module docstring)."""

    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    teach_module.register(ds)
    cmd = ds.bot.tree.get_command("teach").get_command("historical-hoh")

    assert cmd.default_permissions is None


def test_non_moderator_cannot_record_a_historical_hoh(tmp_path, monkeypatch):
    monkeypatch.setattr("commands.teach.TRUSTED_MODERATOR_ROLE_ID", 0)
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    cmd = _get_command(ds)

    interaction = FakeInteraction()
    interaction.user.guild_permissions = SimpleNamespace(administrator=False)
    interaction.user.roles = []

    asyncio.run(cmd.callback(interaction, 28, 6, "Melody", "src", 6, None))

    assert engine.historical_events.verified_hoh_for_cycle(
        season=28, cycle_sequence_number=6
    ) is None
    assert "embed" not in interaction.response.sent[0]["kwargs"]


# ==========================================================
# Preview / confirm -- nothing written until Confirm
# ==========================================================


def test_preview_writes_nothing(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    cmd = _get_command(ds)

    interaction = FakeInteraction()
    asyncio.run(cmd.callback(interaction, 28, 6, "Melody", "src", 6, None))

    assert engine.historical_events.verified_hoh_for_cycle(
        season=28, cycle_sequence_number=6
    ) is None


def test_confirm_records_and_verifies_the_hoh(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_preview_and_confirm(engine, season=28, cycle=6, winner="Melody"))

    event = engine.historical_events.verified_hoh_for_cycle(
        season=28, cycle_sequence_number=6
    )
    assert event is not None
    assert event.participants[0].houseguest == "MELODY"


def test_cancel_writes_nothing(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    cmd = _get_command(ds)

    preview = FakeInteraction()
    asyncio.run(cmd.callback(preview, 28, 6, "Melody", "src", 6, None))
    view = preview.response.sent[0]["kwargs"]["view"]

    cancel = FakeInteraction()
    asyncio.run(view.handle_cancel(cancel))

    assert engine.historical_events.verified_hoh_for_cycle(
        season=28, cycle_sequence_number=6
    ) is None


def test_only_the_requesting_moderator_can_confirm(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    cmd = _get_command(ds)

    preview = FakeInteraction(user_id=111)
    asyncio.run(cmd.callback(preview, 28, 6, "Melody", "src", 6, None))
    view = preview.response.sent[0]["kwargs"]["view"]

    other_user = FakeInteraction(user_id=999)
    asyncio.run(view.handle_confirm(other_user))

    assert engine.historical_events.verified_hoh_for_cycle(
        season=28, cycle_sequence_number=6
    ) is None


# ==========================================================
# Validation
# ==========================================================


def test_blank_winner_is_rejected_before_any_write(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    cmd = _get_command(ds)

    interaction = FakeInteraction()
    asyncio.run(cmd.callback(interaction, 28, 6, "   ", "src", 6, None))

    assert "embed" not in interaction.response.sent[0]["kwargs"]
    assert engine.historical_events.verified_hoh_for_cycle(
        season=28, cycle_sequence_number=6
    ) is None


def test_blank_source_is_rejected_before_any_write(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    ds = _discord_service(engine)
    cmd = _get_command(ds)

    interaction = FakeInteraction()
    asyncio.run(cmd.callback(interaction, 28, 6, "Melody", "  ", 6, None))

    assert "embed" not in interaction.response.sent[0]["kwargs"]


# ==========================================================
# Duplicate detection / correction
# ==========================================================


def test_re_entering_the_same_winner_is_a_no_op(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_preview_and_confirm(engine, cycle=6, winner="Melody"))

    preview2, confirm2 = asyncio.run(
        _preview_and_confirm(engine, cycle=6, winner="Melody")
    )

    candidates = engine.historical_events.find_hoh_candidates(
        season=28, cycle_sequence_number=6
    )
    verified = [c for c in candidates if c.verification_status == "ADMIN_VERIFIED"]
    assert len(verified) == 1  # still exactly one verified record, not two


def test_entering_a_different_winner_requires_explicit_correction_confirmation(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_preview_and_confirm(engine, cycle=6, winner="Melody"))

    preview, confirm = asyncio.run(
        _preview_and_confirm(engine, cycle=6, winner="ActualWinner")
    )

    # The preview embed must have warned this is a correction.
    embed = preview.response.sent[0]["kwargs"]["embed"]
    field_names = [f.name for f in embed.fields]
    assert any("corrects" in name.lower() for name in field_names)

    current = engine.historical_events.verified_hoh_for_cycle(
        season=28, cycle_sequence_number=6
    )
    assert current.participants[0].houseguest == "ACTUALWINNER"


def test_correction_preserves_the_original_record(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    asyncio.run(_preview_and_confirm(engine, cycle=6, winner="Melody"))
    asyncio.run(_preview_and_confirm(engine, cycle=6, winner="ActualWinner"))

    candidates = engine.historical_events.find_hoh_candidates(
        season=28, cycle_sequence_number=6
    )
    original = next(c for c in candidates if c.participants[0].houseguest == "MELODY")
    assert original.verification_status == "CORRECTED"
    assert original.active is True
