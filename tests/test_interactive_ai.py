"""Tests for the new interactive-AI features: real game-state context
for Gemini, the recap buffer, and per-user cooldown.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from production.competition import CompetitionState, CompetitionType
from production.engine import ProductionEngine
from production.events import EventSeverity, EventType, ProductionEvent
from production.house_status import HouseStatus
from services.ai_service import format_game_state


class FakeStorage:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        pass


# ==========================================================
# format_game_state
# ==========================================================


def test_empty_state_returns_empty_string():
    assert format_game_state(HouseStatus(), CompetitionState()) == ""


def test_includes_only_known_facts():
    status = HouseStatus(hoh="Morgan", nominees=("Ava", "Jax"))
    text = format_game_state(status, CompetitionState())

    assert "Morgan" in text
    assert "Ava" in text and "Jax" in text
    assert "Veto" not in text  # not set, must not appear
    assert "don't know yet rather than guessing" in text


def test_veto_used_state_is_reflected():
    status = HouseStatus(veto_holder="Sam", veto_used=True)
    text = format_game_state(status, CompetitionState())

    assert "Sam" in text
    assert "(used)" in text


def test_veto_not_used_state_is_reflected():
    status = HouseStatus(veto_holder="Sam", veto_used=False)
    text = format_game_state(status, CompetitionState())

    assert "(not yet used)" in text


def test_active_competition_is_reflected():
    comp = CompetitionState(
        competition=CompetitionType.POV, active=True
    )
    text = format_game_state(HouseStatus(), comp)

    assert "in progress" in text
    assert "Power of Veto" in text


def test_finished_competition_shows_winner():
    comp = CompetitionState(
        competition=CompetitionType.HOH,
        active=False,
        winner="Riley",
    )
    text = format_game_state(HouseStatus(), comp)

    assert "Riley" in text
    assert "Head of Household" in text


# ==========================================================
# Recap buffer
# ==========================================================


def make_rss_event(detail: str) -> ProductionEvent:
    return ProductionEvent(
        source="Joker's Updates",
        event_type=EventType.RSS_UPDATE,
        title="LIVE FEED UPDATE",
        detail=detail,
        severity=EventSeverity.INFO,
    )


def test_recap_buffer_starts_empty():
    engine = ProductionEngine(storage=FakeStorage())
    assert engine.recent_updates() == []


def test_recap_buffer_records_rss_updates():
    engine = ProductionEngine(storage=FakeStorage())

    engine._record_recap(make_rss_event("Kamu went to the DR."))
    engine._record_recap(make_rss_event("Lights out in HN room."))

    assert engine.recent_updates() == [
        "Kamu went to the DR.",
        "Lights out in HN room.",
    ]


def test_recap_buffer_ignores_non_rss_events():
    engine = ProductionEngine(storage=FakeStorage())

    non_rss = ProductionEvent(
        source="HouseImage",
        event_type=EventType.IMAGE_CHANGED,
        title="HOUSE STATUS UPDATED",
        detail="image changed",
        severity=EventSeverity.NOTICE,
    )

    engine._record_recap(non_rss)

    assert engine.recent_updates() == []


def test_recap_buffer_is_capped():
    engine = ProductionEngine(storage=FakeStorage())
    engine.RECAP_LIMIT = 5

    for i in range(10):
        engine._record_recap(make_rss_event(f"update {i}"))

    buffer = engine.recent_updates(limit=100)
    assert len(buffer) == 5
    # oldest entries dropped, most recent kept
    assert buffer[0] == "update 5"
    assert buffer[-1] == "update 9"


def test_recent_updates_respects_limit_param():
    engine = ProductionEngine(storage=FakeStorage())

    for i in range(10):
        engine._record_recap(make_rss_event(f"update {i}"))

    assert engine.recent_updates(limit=3) == [
        "update 7", "update 8", "update 9",
    ]


# ==========================================================
# AI cooldown (tested against the real DiscordService logic)
# ==========================================================


class _CooldownOnly:
    """Isolates DiscordService's cooldown math without constructing
    a real Discord client/bot in tests."""

    def __init__(self):
        self._ai_cooldowns: dict[int, float] = {}

    from services.discord import DiscordService
    _ai_cooldown_remaining = DiscordService._ai_cooldown_remaining


def test_first_use_has_no_cooldown():
    c = _CooldownOnly()
    assert c._ai_cooldown_remaining(user_id=1) == 0.0


def test_immediate_reuse_is_rate_limited():
    c = _CooldownOnly()
    c._ai_cooldowns[1] = time.monotonic()

    assert c._ai_cooldown_remaining(user_id=1) > 0.0


def test_different_users_do_not_share_a_cooldown():
    c = _CooldownOnly()
    c._ai_cooldowns[1] = time.monotonic()

    assert c._ai_cooldown_remaining(user_id=2) == 0.0


def test_cooldown_expires_after_the_window():
    c = _CooldownOnly()
    # simulate a use far enough in the past that the cooldown is over
    c._ai_cooldowns[1] = time.monotonic() - 999

    assert c._ai_cooldown_remaining(user_id=1) == 0.0


# ==========================================================
# _extract_text diagnostic extraction
# ==========================================================


class FakePart:
    def __init__(self, text):
        self.text = text


class FakeContent:
    def __init__(self, parts):
        self.parts = parts


class FakeFinishReason:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


class FakeCandidate:
    def __init__(self, parts, finish_reason="STOP"):
        self.content = FakeContent(parts)
        self.finish_reason = FakeFinishReason(finish_reason)


class FakeResponse:
    def __init__(self, parts, finish_reason="STOP", text_fallback=""):
        self.candidates = [FakeCandidate(parts, finish_reason)]
        self.text = text_fallback


def test_extract_text_joins_all_parts():
    from services.ai_service import _extract_text

    response = FakeResponse(
        [FakePart("Hello, "), FakePart("Houseguest!")]
    )

    assert _extract_text(response) == "Hello, Houseguest!"


def test_extract_text_logs_non_stop_reason(capsys):
    from services.ai_service import _extract_text

    response = FakeResponse(
        [FakePart("cut off mid")], finish_reason="MAX_TOKENS"
    )

    _extract_text(response)

    captured = capsys.readouterr()
    assert "MAX_TOKENS" in captured.out


def test_extract_text_does_not_log_normal_stop(capsys):
    from services.ai_service import _extract_text

    response = FakeResponse([FakePart("complete.")], finish_reason="STOP")

    _extract_text(response)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_extract_text_falls_back_to_response_text_on_malformed_candidate():
    from services.ai_service import _extract_text

    class BrokenResponse:
        candidates = None  # will raise when indexed
        text = "fallback text"

    assert _extract_text(BrokenResponse()) == "fallback text"


def test_extract_text_falls_back_when_parts_are_empty():
    from services.ai_service import _extract_text

    response = FakeResponse([], finish_reason="SAFETY", text_fallback="fallback")

    assert _extract_text(response) == "fallback"


# ==========================================================
# Thinking-token regression guard
# ==========================================================
#
# The truncation bug (responses cutting off mid-sentence) was traced
# to Gemini's hidden "thinking" tokens consuming the entire
# max_output_tokens budget before any visible reply was generated
# (confirmed via _extract_text's finish_reason logging: MAX_TOKENS
# with only 90 characters produced against a 700-token budget).
# thinking_config=ThinkingConfig(thinking_budget=0) disables that.
# This test exists so a future SDK upgrade that silently drops or
# renames this parameter fails loudly here, rather than quietly
# reintroducing cut-off replies in production.


# ==========================================================
# Truncation fix history
# ==========================================================
#
# Attempt 1: thinking_config=ThinkingConfig(thinking_budget=0) to
# disable Gemini's hidden "thinking" tokens, which were consuming
# the entire max_output_tokens budget before any visible reply was
# generated (confirmed via _extract_text's finish_reason logging:
# MAX_TOKENS with only 90 characters produced against a 700-token
# budget).
#
# That construct validates fine client-side, but the live API
# rejected it outright for this model:
#   AI Service Error: 400 INVALID_ARGUMENT. Request contains an
#   invalid argument.
# Constructing the config object without error was never proof the
# API would accept it — that gap is exactly why this broke every
# single AI call instead of just truncating some of them. Reverted.
#
# Attempt 2 (current): raise max_output_tokens enough (2000) that
# even with mandatory hidden thinking tokens, there should be
# budget left for a real visible reply. This doesn't touch
# thinking_config at all, so it can't reproduce the 400 error.
# Unverified against the live API by the same token as attempt 1 —
# flagging that honestly rather than repeating the same mistake.


def test_ai_service_does_not_set_thinking_config():
    """Guards against reintroducing the config that caused live
    400 INVALID_ARGUMENT errors on every AI call."""

    import inspect

    import services.ai_service as ai_service

    source = inspect.getsource(ai_service)

    assert "thinking_config" not in source, (
        "thinking_config caused a live 400 INVALID_ARGUMENT error "
        "for this model — see the comment above before re-adding it, "
        "and verify against the real API before shipping, not just "
        "that the object constructs locally."
    )


def test_ai_service_uses_a_large_token_budget():
    """The current mitigation: enough headroom that hidden thinking
    tokens shouldn't starve the visible reply."""

    import inspect

    import services.ai_service as ai_service

    source = inspect.getsource(ai_service)

    assert source.count("max_output_tokens=2000") == 2, (
        "Expected both generate_julie_response and generate_recap "
        "to use the raised token budget"
    )


# ==========================================================
# Admin-restricted commands
# ==========================================================
#
# /forget, /posttest, and /status are Administrator-only. Discord
# itself enforces default_permissions, so these tests confirm the
# permission bit is actually set (not just that our own code checks
# something) and that /help's dynamic content matches what a given
# user can actually run.


def test_admin_only_commands_have_administrator_permission_set():
    import discord

    from services.discord import DiscordService

    ds = DiscordService.__new__(DiscordService)  # skip full __init__
    import discord.ext.commands as dc
    ds.bot = dc.Bot(command_prefix="!", intents=discord.Intents.default())

    import commands.forget as forget_module
    import commands.posttest as posttest_module
    import commands.status as status_module
    import commands.ping as ping_module

    class FakeEngine:
        class watcher:
            house_status = type("H", (), {"current": None})()

    ds.scheduler = type("S", (), {"engine": FakeEngine()})()
    ds.command = lambda *a, **kw: ds.bot.tree.command(*a, **kw)

    for module in (forget_module, posttest_module, status_module, ping_module):
        module.register(ds)

    for name in ("forget", "posttest", "status"):
        cmd = ds.bot.tree.get_command(name)
        assert cmd.default_permissions is not None, f"{name} should be restricted"
        assert cmd.default_permissions.administrator is True

    ping_cmd = ds.bot.tree.get_command("ping")
    assert ping_cmd.default_permissions is None, "/ping must stay open to everyone"


def test_help_is_admin_safe():
    """_is_admin must never raise, including for a plain discord.User
    (the DM case) which has no guild_permissions attribute at all."""

    import discord

    from commands.help import _is_admin

    class FakeMemberInteraction:
        class user:
            guild_permissions = discord.Permissions(administrator=True)

    class FakeNonAdminInteraction:
        class user:
            guild_permissions = discord.Permissions(administrator=False)

    class FakeDMInteraction:
        class user:
            pass  # no guild_permissions attribute at all

    assert _is_admin(FakeMemberInteraction()) is True
    assert _is_admin(FakeNonAdminInteraction()) is False
    assert _is_admin(FakeDMInteraction()) is False
