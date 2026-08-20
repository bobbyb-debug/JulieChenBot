"""Tests for production/memory.py MemoryStore -- explicit long-term
conversational memory via /remember, deliberately separate from
KnowledgeStore/official game facts. Covers persistence across a
simulated restart, per-channel scoping (the mechanism that keeps a
DM's memories private from a guild channel and vice versa), keyword
recall, and the /remember command itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

from database.storage import Storage
from production.memory import MemoryStore


def _storage(tmp_path: Path, monkeypatch) -> Storage:
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    return Storage()


# ==========================================================
# Persistence across a simulated restart
# ==========================================================


def test_remember_persists_across_a_fresh_store_instance(
    tmp_path: Path, monkeypatch
) -> None:
    storage = _storage(tmp_path, monkeypatch)
    store_a = MemoryStore(storage=storage)
    store_a.remember(42, 1, "Bobby", "we call the Have-Not room the Slop Dungeon")

    # Simulates a process restart: a brand-new MemoryStore built from
    # the same underlying Storage/file must see what was remembered.
    store_b = MemoryStore(storage=Storage())

    items = store_b.all_items()
    assert len(items) == 1
    assert items[0].content == "we call the Have-Not room the Slop Dungeon"
    assert items[0].author_name == "Bobby"


def test_remember_does_not_touch_knowledge_storage_key(
    tmp_path: Path, monkeypatch
) -> None:
    """Long-term memory is a separate Storage key from KnowledgeStore
    -- must never be conflated with official game facts."""

    storage = _storage(tmp_path, monkeypatch)
    store = MemoryStore(storage=storage)
    store.remember(1, 1, "Bobby", "something memorable")

    assert storage.get("knowledge", []) == []
    assert storage.get(MemoryStore.STORAGE_KEY) is not None


# ==========================================================
# Recall
# ==========================================================


def test_recall_finds_keyword_overlap() -> None:
    store = MemoryStore(storage=_FakeStorage())
    store.remember(1, 1, "Bobby", "we call the Have-Not room the Slop Dungeon")
    store.remember(1, 1, "Bobby", "Julie's favorite catchphrase is expect the unexpected")

    results = store.recall(1, "what do we call the Have-Not room?")

    assert len(results) == 1
    assert "Slop Dungeon" in results[0].content


def test_recall_is_scoped_to_channel() -> None:
    store = MemoryStore(storage=_FakeStorage())
    store.remember(1, 1, "Bobby", "the Slop Dungeon nickname")
    store.remember(2, 1, "Bobby", "a completely different channel's memory")

    results = store.recall(1, "Slop Dungeon")

    assert len(results) == 1
    assert results[0].channel_id == 1


def test_recall_scoping_keeps_a_dm_private_from_a_guild_channel() -> None:
    """A DM's channel_id is unique to that DM (same mechanism
    services/ai_service.py's chat_messages already relies on) -- a
    memory made in a DM must never surface in a guild channel's
    recall, and vice versa."""

    store = MemoryStore(storage=_FakeStorage())
    dm_channel_id = 999888777
    guild_channel_id = 111222333

    store.remember(dm_channel_id, 1, "Bobby", "a private nickname just between us")
    store.remember(guild_channel_id, 1, "Bobby", "a public house nickname")

    dm_results = store.recall(dm_channel_id, "nickname")
    guild_results = store.recall(guild_channel_id, "nickname")

    assert [item.content for item in dm_results] == ["a private nickname just between us"]
    assert [item.content for item in guild_results] == ["a public house nickname"]


def test_recall_falls_back_to_recent_when_no_keyword_overlap() -> None:
    store = MemoryStore(storage=_FakeStorage())
    store.remember(1, 1, "Bobby", "first thing")
    store.remember(1, 1, "Bobby", "second thing")

    results = store.recall(1, "completely unrelated query with no overlap")

    assert len(results) == 2  # falls back to recent items, not empty


def test_recall_returns_empty_for_a_channel_with_no_memories() -> None:
    store = MemoryStore(storage=_FakeStorage())
    assert store.recall(1, "anything") == []


def test_recall_respects_limit() -> None:
    store = MemoryStore(storage=_FakeStorage())
    for i in range(10):
        store.remember(1, 1, "Bobby", f"memory number {i}")

    assert len(store.recall(1, "memory", limit=3)) == 3


# ==========================================================
# Multiple memories coexist -- no supersede logic
# ==========================================================


def test_multiple_memories_coexist_without_superseding() -> None:
    store = MemoryStore(storage=_FakeStorage())
    store.remember(1, 1, "Bobby", "first memory")
    store.remember(1, 2, "Alex", "second memory")

    assert len(store.all_items()) == 2


# ==========================================================
# Malformed persisted records are skipped, never fatal
# ==========================================================


def test_malformed_record_is_discarded_not_fatal() -> None:
    storage = _FakeStorage()
    storage.set(MemoryStore.STORAGE_KEY, [{"garbage": True}])

    store = MemoryStore(storage=storage)  # must not raise

    assert store.all_items() == []


# ==========================================================
# /remember command
# ==========================================================


class _FakeStorage:
    def __init__(self):
        self.data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        pass


class FakeInteraction:
    def __init__(self, user_id: int = 1, channel_id: int = 555) -> None:
        self.user = SimpleNamespace(
            id=user_id, display_name="Bobby", __str__=lambda self: "Bobby#0001"
        )
        self.channel_id = channel_id
        self.sent: list[dict] = []

        async def send_message(*args, **kwargs):
            self.sent.append({"args": args, "kwargs": kwargs})

        self.response = SimpleNamespace(send_message=send_message)


def _discord_service(memory_store: MemoryStore) -> SimpleNamespace:
    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)
    engine = SimpleNamespace(memory=memory_store)
    ds.scheduler = SimpleNamespace(engine=engine)
    return ds


def test_remember_command_registered() -> None:
    import commands.remember as remember_module

    ds = _discord_service(MemoryStore(storage=_FakeStorage()))
    remember_module.register(ds)

    assert ds.bot.tree.get_command("remember") is not None


def test_remember_command_stores_and_confirms() -> None:
    import commands.remember as remember_module

    store = MemoryStore(storage=_FakeStorage())
    ds = _discord_service(store)
    remember_module.register(ds)
    cmd = ds.bot.tree.get_command("remember")

    interaction = FakeInteraction(user_id=42, channel_id=777)
    asyncio.run(cmd.callback(interaction, "we call the Have-Not room the Slop Dungeon"))

    items = store.recall(777, "Have-Not room")
    assert len(items) == 1
    assert items[0].author_id == 42
    assert items[0].author_name == "Bobby"
    assert "Got it" in interaction.sent[0]["args"][0]


def test_remember_command_rejects_empty_text() -> None:
    import commands.remember as remember_module

    store = MemoryStore(storage=_FakeStorage())
    ds = _discord_service(store)
    remember_module.register(ds)
    cmd = ds.bot.tree.get_command("remember")

    interaction = FakeInteraction()
    asyncio.run(cmd.callback(interaction, "   "))

    assert store.all_items() == []


def test_remember_command_open_to_everyone() -> None:
    """Unlike /teach, /remember is personal conversational memory --
    it must carry no administrator restriction."""

    import commands.remember as remember_module

    ds = _discord_service(MemoryStore(storage=_FakeStorage()))
    remember_module.register(ds)

    assert ds.bot.tree.get_command("remember").default_permissions is None
