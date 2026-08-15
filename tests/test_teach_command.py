"""Tests for commands/teach.py -- the /teach command family.

Follows the exact existing pattern for verifying admin-only slash
commands (see tests/test_interactive_ai.py
test_admin_only_commands_have_administrator_permission_set): construct
a bare DiscordService, register the real command module against a real
discord.ext.commands.Bot's command tree, then inspect the registered
commands directly. Discord itself enforces default_permissions
client-side (the same mechanism /forget, /posttest, and /status
already rely on) -- this repo does not simulate a full Discord
interaction to prove rejection, and neither does this file, for
consistency with that established pattern.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
import discord.ext.commands as dc
import pytest

from database.storage import Storage
from production.engine import ProductionEngine
from production.knowledge import KnowledgeType
from services.ai_service import format_learned_knowledge

import commands.teach as teach_module


def _make_discord_service(engine: ProductionEngine) -> SimpleNamespace:
    """Minimal DiscordService stand-in -- same pattern as
    test_admin_only_commands_have_administrator_permission_set, plus a
    real ProductionEngine so commands/teach.py's
    discord_service.scheduler.engine.knowledge access works."""

    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)
    ds.scheduler = SimpleNamespace(engine=engine)
    return ds


def _teach_group(engine: ProductionEngine):
    ds = _make_discord_service(engine)
    teach_module.register(ds)
    return ds.bot.tree.get_command("teach")


class FakeInteraction:
    """Minimal discord.Interaction stand-in: records what would have
    been sent to Discord without any real network/gateway access."""

    def __init__(self, user_id: int = 111) -> None:
        self.user = SimpleNamespace(id=user_id, __str__=lambda self: "TestAdmin#0001")
        self.response = SimpleNamespace(send_message=self._send_message)
        self.sent: list[dict] = []

    async def _send_message(self, *args, **kwargs):
        self.sent.append({"args": args, "kwargs": kwargs})


def _engine(tmp_path: Path, monkeypatch) -> ProductionEngine:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    return ProductionEngine(storage=Storage())


# ==========================================================
# Authorization: matches the existing default_permissions pattern
# ==========================================================


def test_mutating_subcommands_are_administrator_restricted(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    for name in ("fact", "rule", "correction", "forget"):
        cmd = group.get_command(name)
        assert cmd.default_permissions is not None, f"{name} should be restricted"
        assert cmd.default_permissions.administrator is True


def test_list_subcommand_stays_open_to_everyone(
    tmp_path: Path, monkeypatch
) -> None:
    """Reading knowledge is safe; only modification/deletion is
    protected -- per the explicit requirement, /teach list must not
    be administrator-gated."""

    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    list_cmd = group.get_command("list")
    assert list_cmd.default_permissions is None


# ==========================================================
# 1-3. Authorized admin can teach fact / rule / correction
# ==========================================================


def test_teach_fact_creates_active_fact(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    interaction = FakeInteraction()

    asyncio.run(
        group.get_command("fact").callback(
            interaction, "Yash is the current Head of Household."
        )
    )

    active = engine.knowledge.active_items()
    assert len(active) == 1
    assert active[0].type == KnowledgeType.FACT
    assert active[0].content == "Yash is the current Head of Household."
    assert active[0].author_id == 111
    assert len(interaction.sent) == 1
    embed = interaction.sent[0]["kwargs"]["embed"]
    assert "Yash" in embed.description
    assert "#1" in embed.fields[0].value


def test_teach_rule_creates_active_rule(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    interaction = FakeInteraction()

    asyncio.run(
        group.get_command("rule").callback(
            interaction,
            "The house-status image is authoritative for Have-Nots.",
        )
    )

    active = engine.knowledge.active_items()
    assert len(active) == 1
    assert active[0].type == KnowledgeType.RULE


def test_teach_correction_creates_active_correction(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    interaction = FakeInteraction()

    asyncio.run(
        group.get_command("correction").callback(
            interaction, "Yash is HoH, not Barrett."
        )
    )

    active = engine.knowledge.active_items()
    assert len(active) == 1
    assert active[0].type == KnowledgeType.CORRECTION


def test_teach_rejects_blank_text(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)
    interaction = FakeInteraction()

    asyncio.run(group.get_command("fact").callback(interaction, "   "))

    assert engine.knowledge.active_items() == []


# ==========================================================
# 7-8. /teach list and /teach forget
# ==========================================================


def test_teach_list_shows_active_knowledge(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    engine.knowledge.teach(KnowledgeType.FACT, "Yash is HoH.", author_id=1)
    engine.knowledge.teach(KnowledgeType.RULE, "Trust the image.", author_id=1)

    interaction = FakeInteraction()
    asyncio.run(group.get_command("list").callback(interaction))

    embed = interaction.sent[0]["kwargs"]["embed"]
    assert "Yash is HoH." in embed.description
    assert "Trust the image." in embed.description
    assert "#1" in embed.description
    assert "#2" in embed.description


def test_teach_list_reports_when_empty(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    interaction = FakeInteraction()
    asyncio.run(group.get_command("list").callback(interaction))

    assert "hasn't been taught" in interaction.sent[0]["args"][0]


def test_teach_forget_deactivates_item(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    item = engine.knowledge.teach(KnowledgeType.FACT, "stale", author_id=1)

    interaction = FakeInteraction()
    asyncio.run(group.get_command("forget").callback(interaction, item.id))

    assert engine.knowledge.active_items() == []
    assert "Forgot" in interaction.sent[0]["args"][0]


def test_teach_forget_unknown_id_reports_cleanly(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    interaction = FakeInteraction()
    asyncio.run(group.get_command("forget").callback(interaction, 999))

    assert "no active knowledge" in interaction.sent[0]["args"][0]


# ==========================================================
# 5-6. Persistence and restart survival, via the real engine
# ==========================================================


def test_taught_knowledge_persists_and_survives_engine_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    engine_a = ProductionEngine(storage=storage)
    group_a = _teach_group(engine_a)
    interaction = FakeInteraction()
    asyncio.run(
        group_a.get_command("fact").callback(interaction, "Yash is HoH.")
    )

    # Simulated Railway restart: fresh Storage + fresh engine.
    engine_b = ProductionEngine(storage=Storage())

    active = engine_b.knowledge.active_items()
    assert len(active) == 1
    assert active[0].content == "Yash is HoH."


# ==========================================================
# 9-11. AI context integration
# ==========================================================


def test_forgotten_knowledge_is_not_injected_into_ai_context(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    item = engine.knowledge.teach(KnowledgeType.FACT, "Barrett is HoH.", author_id=1)
    engine.knowledge.forget(item.id)

    context = format_learned_knowledge(engine.knowledge.active_items())

    assert context == ""


def test_active_authoritative_knowledge_is_injected_into_ai_context(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    engine.knowledge.teach(KnowledgeType.FACT, "Yash is HoH.", author_id=1)

    context = format_learned_knowledge(engine.knowledge.active_items())

    assert "Yash is HoH." in context


def test_authoritative_knowledge_is_clearly_distinguished_from_conversation() -> None:
    from production.knowledge import KnowledgeItem
    from datetime import UTC, datetime

    item = KnowledgeItem(
        id=1,
        type=KnowledgeType.FACT,
        content="Yash is HoH.",
        author_id=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    context = format_learned_knowledge([item])

    assert "ADMINISTRATOR-TAUGHT" in context
    assert "not conversation history" in context.lower()
    assert "more reliable than your own inference" in context.lower()


# ==========================================================
# 12. A correction properly supersedes conflicting prior knowledge
# ==========================================================


def test_correction_workflow_removes_stale_fact_from_active_context(
    tmp_path: Path, monkeypatch
) -> None:
    """The intended admin workflow: forget the stale fact, then teach
    the correction -- only the correction should remain in what
    reaches the AI. (See production/knowledge.py's design note: this
    repo does not attempt automatic content-matching between a new
    correction and old facts -- that would be exactly the kind of
    fragile heuristic this project's "do not over-engineer" principle
    rules out. The admin's explicit /teach forget is the deterministic
    mechanism.)
    """

    engine = _engine(tmp_path, monkeypatch)

    stale = engine.knowledge.teach(KnowledgeType.FACT, "Barrett is HoH.", author_id=1)
    engine.knowledge.forget(stale.id)
    engine.knowledge.teach(
        KnowledgeType.CORRECTION, "Yash is HoH, not Barrett.", author_id=1
    )

    context = format_learned_knowledge(engine.knowledge.active_items())

    assert "Barrett is HoH." not in context
    assert "Yash is HoH, not Barrett." in context


def test_correction_block_is_explicitly_framed_as_overriding() -> None:
    """Even without the admin manually forgetting the old fact, the
    rendered context must not present a fact and a contradicting
    correction as equally-weighted, unresolved statements -- the
    correction section is explicitly labeled as authoritative over
    conflicting facts."""

    from production.knowledge import KnowledgeItem
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    fact = KnowledgeItem(
        id=1, type=KnowledgeType.FACT, content="Barrett is HoH.",
        author_id=1, created_at=now, updated_at=now,
    )
    correction = KnowledgeItem(
        id=2, type=KnowledgeType.CORRECTION, content="Yash is HoH, not Barrett.",
        author_id=1, created_at=now, updated_at=now,
    )

    context = format_learned_knowledge([fact, correction])

    assert "Barrett is HoH." in context
    assert "Yash is HoH, not Barrett." in context
    # The correction section must explicitly claim precedence, not
    # just sit silently next to the contradicting fact.
    corrections_index = context.index("ADMINISTRATOR CORRECTIONS")
    assert "override" in context[corrections_index:].lower()


def test_rules_are_framed_as_permanent_and_facts_as_maintainable() -> None:
    """The prompt text itself must draw the distinction, not just the
    section headings -- rules get 'no expiry'/'absolute' language,
    facts get 'can become outdated' language."""

    from datetime import UTC, datetime
    from production.knowledge import KnowledgeItem

    now = datetime.now(UTC)
    rule = KnowledgeItem(
        id=1, type=KnowledgeType.RULE,
        content="The house-status image is authoritative for Have-Nots.",
        author_id=1, created_at=now, updated_at=now,
    )
    fact = KnowledgeItem(
        id=2, type=KnowledgeType.FACT, content="Yash is HoH.",
        author_id=1, created_at=now, updated_at=now,
    )

    context = format_learned_knowledge([rule, fact])

    rules_index = context.index("PERMANENT RULES")
    facts_index = context.index("ADMINISTRATOR-MAINTAINED FACTS")
    rules_section = context[rules_index:facts_index]
    facts_section = context[facts_index:]

    assert "no expiry" in rules_section.lower() or "never expire" in rules_section.lower()
    assert "outdated" in facts_section.lower() or "stale" in facts_section.lower()


# ==========================================================
# Explicit supersession via the actual /teach commands
# ==========================================================


def test_teach_correction_with_supersedes_deactivates_target(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    stale = engine.knowledge.teach(
        KnowledgeType.FACT, "Barrett is HoH.", author_id=1
    )

    interaction = FakeInteraction()
    asyncio.run(
        group.get_command("correction").callback(
            interaction, "Yash is HoH, not Barrett.", stale.id
        )
    )

    active = engine.knowledge.active_items()
    assert len(active) == 1
    assert active[0].content == "Yash is HoH, not Barrett."
    assert active[0].supersedes == stale.id

    embed = interaction.sent[0]["kwargs"]["embed"]
    assert any(f.name == "Supersedes" and f.value == f"#{stale.id}" for f in embed.fields)


def test_teach_correction_with_invalid_supersedes_reports_cleanly_and_creates_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    interaction = FakeInteraction()
    asyncio.run(
        group.get_command("correction").callback(
            interaction, "Yash is HoH.", 999
        )
    )

    assert engine.knowledge.active_items() == []
    assert "999" in interaction.sent[0]["args"][0]


def test_teach_fact_also_supports_supersedes(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    stale = engine.knowledge.teach(
        KnowledgeType.FACT, "Barrett is HoH.", author_id=1
    )

    interaction = FakeInteraction()
    asyncio.run(
        group.get_command("fact").callback(interaction, "Yash is HoH.", stale.id)
    )

    active = engine.knowledge.active_items()
    assert len(active) == 1
    assert active[0].content == "Yash is HoH."
    assert active[0].supersedes == stale.id


def test_teach_correction_without_supersedes_still_works(
    tmp_path: Path, monkeypatch
) -> None:
    """The freeform path (no admin lookup of an ID required) must
    remain fully available -- supersedes is opt-in, never mandatory."""

    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    interaction = FakeInteraction()
    asyncio.run(
        group.get_command("correction").callback(
            interaction, "Yash is HoH, not Barrett.", None
        )
    )

    active = engine.knowledge.active_items()
    assert len(active) == 1
    assert active[0].supersedes is None


# ==========================================================
# /teach list: timestamps and the truncation boundary
# ==========================================================


def test_teach_list_shows_taught_date(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    engine.knowledge.teach(KnowledgeType.FACT, "Yash is HoH.", author_id=1)

    interaction = FakeInteraction()
    asyncio.run(group.get_command("list").callback(interaction))

    embed = interaction.sent[0]["kwargs"]["embed"]
    assert "Taught:" in embed.description


def test_teach_list_shows_supersedes_relationship(
    tmp_path: Path, monkeypatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    stale = engine.knowledge.teach(KnowledgeType.FACT, "Barrett is HoH.", author_id=1)
    engine.knowledge.teach(
        KnowledgeType.CORRECTION,
        "Yash is HoH, not Barrett.",
        author_id=1,
        supersedes=stale.id,
    )
    engine.knowledge.forget(stale.id)

    interaction = FakeInteraction()
    asyncio.run(group.get_command("list").callback(interaction))

    embed = interaction.sent[0]["kwargs"]["embed"]
    assert f"supersedes #{stale.id}" in embed.description


def test_teach_list_actually_truncates_past_the_character_limit(
    tmp_path: Path, monkeypatch
) -> None:
    """Exercises the real LIST_CHAR_LIMIT boundary (3900 chars) rather
    than just asserting the constant exists -- proves the truncation
    branch in commands/teach.py actually engages and the resulting
    embed description stays within Discord's real 4096-char limit."""

    engine = _engine(tmp_path, monkeypatch)
    group = _teach_group(engine)

    # Each item's rendered block is roughly 150+ chars (header + long
    # content + taught line); 40 of them comfortably exceeds the
    # 3900-char LIST_CHAR_LIMIT.
    long_content = "Yash is the current Head of Household. " * 5
    for _ in range(40):
        engine.knowledge.teach(KnowledgeType.FACT, long_content, author_id=1)

    interaction = FakeInteraction()
    asyncio.run(group.get_command("list").callback(interaction))

    embed = interaction.sent[0]["kwargs"]["embed"]
    assert len(embed.description) <= teach_module.LIST_CHAR_LIMIT + 200
    assert "total item(s)" in embed.description
    assert "40" in embed.description
    # Discord's actual hard limit -- the whole point of truncating.
    assert len(embed.description) <= 4096
