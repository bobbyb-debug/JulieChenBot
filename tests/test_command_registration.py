"""Tests for the duplicate-slash-command fix (services/discord.py).

Root cause: DiscordService's on_ready() handler synced the same
command set to BOTH a specific guild (via copy_global_to() +
sync(guild=guild)) AND globally (a separate sync() call with no
guild). Discord shows a guild-scoped and a global-scoped registration
of the same command name side by side in any guild where a bot has
both -- confirmed directly against discord.py's own behavior (see
below) rather than assumed -- so every command appeared twice.

The fix (DiscordService.sync_commands()) syncs guild-scoped only, and
actively clears/re-syncs an empty global command set to remove
whatever Discord still has cached globally from every earlier deploy
that ran the old dual-sync code. This is not a workaround layered on
top of the duplication -- it changes which Discord-side state exists
at all, so there is nothing left to show twice.

Separately: on_ready() is not guaranteed by Discord to fire only once
per process (a full reconnect can trigger it again, not just
on_resumed()). load_commands() now guards against being re-run, since
a second pass would hit discord.py's own CommandAlreadyRegistered for
every command -- verified directly (see the empirical check this fix
was based on): re-registering an existing command name or Group raises
rather than silently duplicating, so this was never a source of
visible Discord-side duplication, but it did produce noisy, misleading
per-command failure logs on every reconnect.

No real Discord connection is made anywhere in this file.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

from services.discord import DiscordService


class FakeEngine:
    class watcher:
        house_status = SimpleNamespace(current=None)
        competition = SimpleNamespace(current=None)

    class knowledge:
        @staticmethod
        def active_items():
            return []


def _bare_discord_service() -> DiscordService:
    """A DiscordService with __init__ skipped (no real Discord
    connection), matching the existing pattern in
    tests/test_interactive_ai.py's admin-permission test."""

    ds = DiscordService.__new__(DiscordService)
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)
    ds.scheduler = SimpleNamespace(engine=FakeEngine())
    ds._commands_loaded = False

    from services.logger import ProductionLogger
    ds.logger = ProductionLogger.get("Discord")

    return ds


# ==========================================================
# 1-3, 6-7. Every real command -- including /nominees, /noms, /teach
# and its children -- is registered exactly once on the command tree
# ==========================================================


def test_every_command_is_registered_exactly_once() -> None:
    ds = _bare_discord_service()
    ds.load_commands()

    names = [c.name for c in ds.bot.tree.get_commands()]
    duplicates = {name for name in names if names.count(name) > 1}

    assert duplicates == set()
    assert "nominees" in names
    assert "noms" in names
    assert "teach" in names


def test_nominees_and_noms_both_present_exactly_once() -> None:
    ds = _bare_discord_service()
    ds.load_commands()

    names = [c.name for c in ds.bot.tree.get_commands()]
    assert names.count("nominees") == 1
    assert names.count("noms") == 1


def test_teach_group_registered_exactly_once() -> None:
    ds = _bare_discord_service()
    ds.load_commands()

    teach_commands = [c for c in ds.bot.tree.get_commands() if c.name == "teach"]
    assert len(teach_commands) == 1


def test_every_teach_child_registered_exactly_once() -> None:
    ds = _bare_discord_service()
    ds.load_commands()

    teach = ds.bot.tree.get_command("teach")
    child_names = [c.name for c in teach.commands]

    for expected in ("fact", "rule", "correction", "list", "forget"):
        assert child_names.count(expected) == 1


def test_load_commands_called_twice_does_not_duplicate_or_raise() -> None:
    """on_ready() is not guaranteed to fire only once -- a second
    load_commands() call must be a safe no-op, not raise
    CommandAlreadyRegistered or duplicate anything."""

    ds = _bare_discord_service()
    ds.load_commands()
    first_names = sorted(c.name for c in ds.bot.tree.get_commands())

    ds.load_commands()  # must not raise
    second_names = sorted(c.name for c in ds.bot.tree.get_commands())

    assert second_names == first_names


def test_discord_py_raises_rather_than_silently_duplicating_on_re_registration() -> None:
    """The empirical fact the fix above is based on, pinned as a
    regression test against discord.py's own behavior: re-registering
    an existing command name raises CommandAlreadyRegistered. If this
    ever stopped being true, load_commands()'s guard would still be
    correct, but the reasoning behind it (documented above) would no
    longer hold, so this is worth pinning directly."""

    bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())

    @bot.tree.command(name="dup", description="first")
    async def first(interaction: discord.Interaction) -> None:
        pass

    raised = False
    try:
        @bot.tree.command(name="dup", description="second")
        async def second(interaction: discord.Interaction) -> None:
            pass
    except discord.app_commands.CommandAlreadyRegistered:
        raised = True

    assert raised is True
    assert len(bot.tree.get_commands()) == 1


# ==========================================================
# 8. sync_commands() syncs guild-scoped only, and actively clears
# (never repopulates) the global scope -- no path syncs the real
# command set to both scopes at once.
# ==========================================================


def test_sync_commands_syncs_guild_and_clears_global(monkeypatch) -> None:
    ds = _bare_discord_service()
    ds.load_commands()

    fake_guild = SimpleNamespace(id=999)
    fake_channel = SimpleNamespace(guild=fake_guild)
    ds.bot.get_channel = lambda channel_id: fake_channel

    calls: list[tuple] = []

    def fake_copy_global_to(*, guild):
        calls.append(("copy_global_to", guild))

    async def fake_sync(*, guild=None):
        calls.append(("sync", guild))
        return []

    def fake_clear_commands(*, guild):
        calls.append(("clear_commands", guild))

    monkeypatch.setattr(ds.bot.tree, "copy_global_to", fake_copy_global_to)
    monkeypatch.setattr(ds.bot.tree, "sync", fake_sync)
    monkeypatch.setattr(ds.bot.tree, "clear_commands", fake_clear_commands)

    asyncio.run(ds.sync_commands())

    assert ("copy_global_to", fake_guild) in calls
    assert ("sync", fake_guild) in calls
    assert ("clear_commands", None) in calls
    assert ("sync", None) in calls

    # Exactly one guild sync, exactly one global sync -- and the
    # global scope is always cleared BEFORE the global sync call, so
    # that sync() call can only ever push an empty command set.
    sync_calls = [c for c in calls if c[0] == "sync"]
    assert len(sync_calls) == 2
    clear_index = calls.index(("clear_commands", None))
    global_sync_index = calls.index(("sync", None))
    assert clear_index < global_sync_index


def test_sync_commands_still_clears_global_when_guild_unresolvable(monkeypatch) -> None:
    """If LIVE_UPDATES_CHANNEL can't be resolved to a guild, the
    global-clear step must still run independently -- it must not be
    skipped just because the guild sync failed."""

    ds = _bare_discord_service()
    ds.load_commands()

    ds.bot.get_channel = lambda channel_id: None

    calls: list[tuple] = []

    async def fake_sync(*, guild=None):
        calls.append(("sync", guild))
        return []

    def fake_clear_commands(*, guild):
        calls.append(("clear_commands", guild))

    monkeypatch.setattr(ds.bot.tree, "sync", fake_sync)
    monkeypatch.setattr(ds.bot.tree, "clear_commands", fake_clear_commands)

    asyncio.run(ds.sync_commands())

    assert ("clear_commands", None) in calls
    assert ("sync", None) in calls
    assert not any(guild is not None for _, guild in calls)


def test_sync_commands_survives_a_failed_guild_sync_and_still_clears_global(
    monkeypatch,
) -> None:
    """A raised exception during the guild sync must not prevent the
    (separately try/excepted) global clear from running."""

    ds = _bare_discord_service()
    ds.load_commands()

    fake_guild = SimpleNamespace(id=999)
    fake_channel = SimpleNamespace(guild=fake_guild)
    ds.bot.get_channel = lambda channel_id: fake_channel

    def raising_copy_global_to(*, guild):
        raise RuntimeError("simulated guild sync failure")

    calls: list[tuple] = []

    async def fake_sync(*, guild=None):
        calls.append(("sync", guild))
        return []

    def fake_clear_commands(*, guild):
        calls.append(("clear_commands", guild))

    monkeypatch.setattr(ds.bot.tree, "copy_global_to", raising_copy_global_to)
    monkeypatch.setattr(ds.bot.tree, "sync", fake_sync)
    monkeypatch.setattr(ds.bot.tree, "clear_commands", fake_clear_commands)

    asyncio.run(ds.sync_commands())  # must not raise

    assert ("clear_commands", None) in calls
    assert ("sync", None) in calls
