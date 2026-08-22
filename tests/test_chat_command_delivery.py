"""Tests for commands/chat.py's actual message delivery -- proving the
real /chat command callback uses services/message_chunking.py's
send_long_message() (see tests/test_message_chunking.py for the
splitting logic itself), the exact fix for the production incident
where a long KNOWLEDGE_SUMMARY-style reply raised discord.errors.
HTTPException 400 (error code 50035, "Must be 2000 or fewer in
length") from a single interaction.followup.send(reply) call.

Follows the same "register the real command module against a real
discord.ext.commands.Bot, then invoke its .callback() directly" pattern
already established in tests/test_teach_command.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import discord.ext.commands as dc

import commands.chat as chat_module
from services.message_chunking import DISCORD_MESSAGE_LIMIT


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str) -> None:
        self.sent.append(content)


class FakeInteraction:
    """Minimal discord.Interaction stand-in for /chat specifically --
    response.defer() + followup.send(), a user with no guild
    (guild_permissions absent, matching a DM or a plain member with no
    cached permissions), and channel_id."""

    def __init__(self, user_id: int = 111, channel_id: int = 555) -> None:
        self.user = SimpleNamespace(
            id=user_id, display_name="Alex", __str__=lambda self: "Alex#0001"
        )
        self.channel_id = channel_id
        self.followup = FakeFollowup()
        self.response = SimpleNamespace(defer=self._defer)
        self.deferred = False

    async def _defer(self) -> None:
        self.deferred = True


def _chat_command(fake_reply: str):
    """Registers the real commands/chat.py module against a real
    command tree, with discord_service.generate_ai_reply() stubbed to
    return `fake_reply` -- isolates this test to chat.py's own
    delivery wiring, not generate_ai_reply()'s internals (already
    covered in tests/test_knowledge_summary_boundary.py and friends).
    """

    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)

    async def fake_generate_ai_reply(*args, **kwargs):
        return fake_reply

    ds.generate_ai_reply = fake_generate_ai_reply

    chat_module.register(ds)
    return ds.bot.tree.get_command("chat")


# ==========================================================
# Normal short replies are completely unaffected (9. /chat continues
# working; 11. existing short responses unchanged)
# ==========================================================


def test_short_reply_is_sent_exactly_once():
    command = _chat_command("Yash is the current HOH.")
    interaction = FakeInteraction()

    asyncio.run(command.callback(interaction, "Who is the current HoH?"))

    assert interaction.deferred is True
    assert interaction.followup.sent == ["Yash is the current HOH."]


# ==========================================================
# The actual production bug: a long reply must be delivered in
# multiple chunks, not raise/silently fail
# ==========================================================


def test_long_reply_is_delivered_in_multiple_chunks_under_the_discord_limit():
    long_reply = (
        "- Current official game state: you have a live, admin-verified value...\n"
        "- Administrator-taught knowledge: several rules and facts...\n"
    ) * 40  # comfortably over 2,000 characters, matching a real
    # KNOWLEDGE_SUMMARY-style moderator briefing's actual length

    assert len(long_reply) > DISCORD_MESSAGE_LIMIT

    command = _chat_command(long_reply)
    interaction = FakeInteraction()

    asyncio.run(command.callback(interaction, "Tell me everything you know."))

    assert len(interaction.followup.sent) > 1
    assert all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in interaction.followup.sent)
    # No content lost across the chunk boundaries.
    rejoined_words = " ".join(interaction.followup.sent).split()
    assert rejoined_words == long_reply.split()


def test_generate_ai_reply_is_called_exactly_once_regardless_of_split_count():
    """The chunking fix must never trigger an extra AI generation call
    -- splitting happens strictly after generate_ai_reply() already
    returned its one final string."""

    call_count = 0
    long_reply = "word " * 1000

    async def counting_generate_ai_reply(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return long_reply

    ds = SimpleNamespace()
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)
    ds.generate_ai_reply = counting_generate_ai_reply
    chat_module.register(ds)
    command = ds.bot.tree.get_command("chat")

    interaction = FakeInteraction()
    asyncio.run(command.callback(interaction, "Tell me everything you know."))

    assert call_count == 1
    assert len(interaction.followup.sent) > 1
