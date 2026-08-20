"""Tests for /teach batch -- the Discord wiring around
production/batch_teach.py's pure parse/plan/apply logic.

Covers: preview embed content, permission restriction, the
Confirm/Cancel view (including requester-only enforcement and
timeout), and that zero writes happen until Confirm is actually
clicked by the moderator who ran the command.
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
from production.knowledge import KnowledgeType


def _engine(tmp_path: Path, monkeypatch) -> ProductionEngine:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    return ProductionEngine(storage=Storage())


def _make_discord_service(engine: ProductionEngine) -> SimpleNamespace:
    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)
    ds.scheduler = SimpleNamespace(engine=engine)
    return ds


def _teach_group(engine: ProductionEngine):
    ds = _make_discord_service(engine)
    teach_module.register(ds)
    return ds.bot.tree.get_command("teach")


class FakeMessage:
    def __init__(self) -> None:
        self.edited: list[dict] = []
        self.raise_on_edit = False

    async def edit(self, **kwargs) -> None:
        if self.raise_on_edit:
            raise RuntimeError("simulated: message/webhook token gone")
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


# ==========================================================
# Registration / permissions
# ==========================================================


def test_batch_subcommand_is_registered(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    assert group.get_command("batch") is not None


def test_batch_carries_no_default_permissions_restriction(
    tmp_path: Path, monkeypatch
) -> None:
    """Deliberate: unlike fact/rule/correction/forget, batch is NOT
    gated by Discord's client-side default_permissions -- see
    commands/teach.py's module docstring and _is_trusted_moderator()
    for why. Authorization is enforced explicitly inside the callback
    instead (see the trusted-moderator tests below), since Discord's
    default_permissions has no way to express "one specific configured
    role" the way TRUSTED_MODERATOR_ROLE_ID needs."""

    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    assert group.get_command("batch").default_permissions is None


def test_non_administrator_without_moderator_role_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("commands.teach.TRUSTED_MODERATOR_ROLE_ID", 0)

    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction()
    interaction.user.guild_permissions = SimpleNamespace(administrator=False)
    interaction.user.roles = []

    asyncio.run(batch_cmd.callback(interaction, "FACT: one"))

    assert engine.knowledge.all_items() == []
    sent = interaction.response.sent[0]["kwargs"]
    assert sent["ephemeral"] is True
    assert "trusted moderator" in interaction.response.sent[0]["args"][0].lower()


def test_administrator_is_always_authorized_for_batch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("commands.teach.TRUSTED_MODERATOR_ROLE_ID", 0)

    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction()
    interaction.user.guild_permissions = SimpleNamespace(administrator=True)
    interaction.user.roles = []

    asyncio.run(batch_cmd.callback(interaction, "FACT: one"))

    assert "embed" in interaction.response.sent[0]["kwargs"]  # preview was sent


def test_user_with_configured_moderator_role_is_authorized_for_batch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("commands.teach.TRUSTED_MODERATOR_ROLE_ID", 4242)

    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction()
    interaction.user.guild_permissions = SimpleNamespace(administrator=False)
    interaction.user.roles = [SimpleNamespace(id=4242)]

    asyncio.run(batch_cmd.callback(interaction, "FACT: one"))

    assert "embed" in interaction.response.sent[0]["kwargs"]  # preview was sent


def test_user_with_a_different_role_is_still_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("commands.teach.TRUSTED_MODERATOR_ROLE_ID", 4242)

    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction()
    interaction.user.guild_permissions = SimpleNamespace(administrator=False)
    interaction.user.roles = [SimpleNamespace(id=9999)]  # not the configured role

    asyncio.run(batch_cmd.callback(interaction, "FACT: one"))

    assert engine.knowledge.all_items() == []
    assert "embed" not in interaction.response.sent[0]["kwargs"]


# ==========================================================
# Preview
# ==========================================================


def test_batch_preview_shows_correct_counts(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    text = (
        "FACT: Yash has won several competitions.\n"
        "FACT: Angela is known for strategic gameplay.\n"
        "RULE: Never invent live-feed information.\n"
        "STATE: HOH = Yash\n"
    )
    interaction = FakeInteraction()

    asyncio.run(batch_cmd.callback(interaction, text))

    assert len(interaction.response.sent) == 1
    sent = interaction.response.sent[0]["kwargs"]
    assert sent["ephemeral"] is True
    embed = sent["embed"]
    assert "2 Fact(s)" in embed.description
    assert "1 Rule(s)" in embed.description
    assert "1 Current-State Update(s)" in embed.description
    view = sent["view"]
    assert view.plan.valid  # confirm button not disabled


def test_batch_preview_with_no_lines_sends_plain_message(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction()
    asyncio.run(batch_cmd.callback(interaction, "   \n\n  "))

    assert len(interaction.response.sent) == 1
    sent = interaction.response.sent[0]
    assert "view" not in sent["kwargs"]
    assert engine.knowledge.all_items() == []


def test_batch_preview_shows_malformed_lines(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction()
    asyncio.run(
        batch_cmd.callback(interaction, "FACT: good one\nthis has no prefix at all")
    )

    embed = interaction.response.sent[0]["kwargs"]["embed"]
    parse_field = next(f for f in embed.fields if "parse" in f.name.lower())
    assert "Line 2" in parse_field.value


def test_batch_preview_shows_state_conflicts(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    engine.knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction()
    asyncio.run(batch_cmd.callback(interaction, "STATE: HOH = Barrett"))

    embed = interaction.response.sent[0]["kwargs"]["embed"]
    conflict_field = next(f for f in embed.fields if "change" in f.name.lower())
    assert "Yash" in conflict_field.value
    assert "Barrett" in conflict_field.value

    # Nothing written yet -- only the preview was sent.
    assert engine.knowledge.active_state("HOH").content == "Yash"


def test_nothing_is_written_before_confirmation(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction()
    asyncio.run(
        batch_cmd.callback(interaction, "FACT: one\nRULE: two\nSTATE: HOH = Yash")
    )

    assert engine.knowledge.all_items() == []


# ==========================================================
# Confirm / Cancel view
# ==========================================================


def test_confirm_by_requester_writes_plan_and_edits_message(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction(user_id=111)
    asyncio.run(batch_cmd.callback(interaction, "FACT: one\nRULE: two"))
    view = interaction.response.sent[0]["kwargs"]["view"]

    confirm_interaction = FakeInteraction(user_id=111)
    asyncio.run(view.handle_confirm(confirm_interaction))

    assert len(engine.knowledge.all_items()) == 2
    assert confirm_interaction.response.edited[0]["kwargs"]["content"] == "✅ Trained 2 item(s)."
    assert view.confirm_button.disabled is True
    assert view.cancel_button.disabled is True


def test_confirm_by_non_requester_is_rejected_and_writes_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction(user_id=111)
    asyncio.run(batch_cmd.callback(interaction, "FACT: one"))
    view = interaction.response.sent[0]["kwargs"]["view"]

    other_user_interaction = FakeInteraction(user_id=999)
    asyncio.run(view.handle_confirm(other_user_interaction))

    assert engine.knowledge.all_items() == []
    assert len(other_user_interaction.response.sent) == 1
    assert other_user_interaction.response.sent[0]["kwargs"]["ephemeral"] is True


def test_cancel_writes_nothing_and_edits_message(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction(user_id=111)
    asyncio.run(batch_cmd.callback(interaction, "FACT: one\nSTATE: HOH = Yash"))
    view = interaction.response.sent[0]["kwargs"]["view"]

    cancel_interaction = FakeInteraction(user_id=111)
    asyncio.run(view.handle_cancel(cancel_interaction))

    assert engine.knowledge.all_items() == []
    assert (
        cancel_interaction.response.edited[0]["kwargs"]["content"]
        == "❌ Batch cancelled. Nothing was written."
    )


def test_cancel_by_non_requester_is_rejected(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction(user_id=111)
    asyncio.run(batch_cmd.callback(interaction, "FACT: one"))
    view = interaction.response.sent[0]["kwargs"]["view"]

    other_user_interaction = FakeInteraction(user_id=999)
    asyncio.run(view.handle_cancel(other_user_interaction))

    assert len(other_user_interaction.response.sent) == 1
    assert other_user_interaction.response.sent[0]["kwargs"]["ephemeral"] is True


def test_confirm_button_disabled_when_batch_has_nothing_valid(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction()
    asyncio.run(batch_cmd.callback(interaction, "no prefix at all here"))

    view = interaction.response.sent[0]["kwargs"]["view"]
    assert view.confirm_button.disabled is True


def test_state_conflict_batch_confirm_supersedes_correctly(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: preview a STATE conflict, confirm, and verify the
    old state is superseded exactly like a direct teach() call."""

    engine = _engine(tmp_path, monkeypatch)
    old = engine.knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction(user_id=111)
    asyncio.run(batch_cmd.callback(interaction, "STATE: HOH = Barrett"))
    view = interaction.response.sent[0]["kwargs"]["view"]

    confirm_interaction = FakeInteraction(user_id=111)
    asyncio.run(view.handle_confirm(confirm_interaction))

    assert engine.knowledge.get(old.id).active is False
    assert engine.knowledge.active_state("HOH").content == "Barrett"


# ==========================================================
# Timeout: zero writes, best-effort cosmetic cleanup
# ==========================================================


def test_timeout_disables_buttons_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction(user_id=111)
    asyncio.run(batch_cmd.callback(interaction, "FACT: one\nSTATE: HOH = Yash"))
    view = interaction.response.sent[0]["kwargs"]["view"]

    asyncio.run(view.on_timeout())

    assert engine.knowledge.all_items() == []
    assert view.confirm_button.disabled is True
    assert view.cancel_button.disabled is True
    assert view.message.edited[0]["content"] == "⌛ Batch timed out. Nothing was written."


def test_timeout_is_safe_when_message_edit_fails(tmp_path: Path, monkeypatch) -> None:
    """A dead webhook token/deleted message during on_timeout() must
    never raise -- the writes-nothing guarantee already held before
    this cosmetic cleanup even runs."""

    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    batch_cmd = group.get_command("batch")

    interaction = FakeInteraction(user_id=111)
    asyncio.run(batch_cmd.callback(interaction, "FACT: one"))
    view = interaction.response.sent[0]["kwargs"]["view"]
    view.message.raise_on_edit = True

    asyncio.run(view.on_timeout())  # must not raise

    assert engine.knowledge.all_items() == []
