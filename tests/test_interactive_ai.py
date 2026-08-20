"""Tests for the new interactive-AI features: real game-state context
for Gemini, the recap buffer, and per-user cooldown.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
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
    assert "UNVERIFIED" in text  # must never read as confirmed fact


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


def make_rss_event(detail: str, created_at: datetime | None = None) -> ProductionEvent:
    kwargs = {}
    if created_at is not None:
        kwargs["created_at"] = created_at
    return ProductionEvent(
        source="Joker's Updates",
        event_type=EventType.RSS_UPDATE,
        title="LIVE FEED UPDATE",
        detail=detail,
        severity=EventSeverity.INFO,
        **kwargs,
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

    buffer = engine.recent_updates()
    assert len(buffer) == 5
    # oldest entries dropped, most recent kept
    assert buffer[0] == "update 5"
    assert buffer[-1] == "update 9"


def test_recent_updates_excludes_events_older_than_the_window():
    engine = ProductionEngine(storage=FakeStorage())
    now = datetime.now(UTC)

    engine._record_recap(make_rss_event("too old", created_at=now - timedelta(hours=30)))
    engine._record_recap(make_rss_event("within window", created_at=now - timedelta(hours=2)))

    assert engine.recent_updates(hours=24) == ["within window"]


def test_recent_updates_respects_custom_hours_param():
    engine = ProductionEngine(storage=FakeStorage())
    now = datetime.now(UTC)

    engine._record_recap(make_rss_event("two hours ago", created_at=now - timedelta(hours=2)))
    engine._record_recap(make_rss_event("thirty minutes ago", created_at=now - timedelta(minutes=30)))

    assert engine.recent_updates(hours=1) == ["thirty minutes ago"]


def test_recent_updates_preserves_chronological_order():
    engine = ProductionEngine(storage=FakeStorage())
    now = datetime.now(UTC)

    # Recorded out of order; must come back oldest-first (buffer
    # append order, which _record_recap always preserves).
    engine._record_recap(make_rss_event("first", created_at=now - timedelta(hours=3)))
    engine._record_recap(make_rss_event("second", created_at=now - timedelta(hours=2)))
    engine._record_recap(make_rss_event("third", created_at=now - timedelta(hours=1)))

    assert engine.recent_updates(hours=24) == ["first", "second", "third"]


def test_recent_updates_deduplicates_identical_detail_text():
    engine = ProductionEngine(storage=FakeStorage())
    now = datetime.now(UTC)

    engine._record_recap(make_rss_event("same text", created_at=now - timedelta(hours=2)))
    engine._record_recap(make_rss_event("same text", created_at=now - timedelta(hours=1)))
    engine._record_recap(make_rss_event("different text", created_at=now))

    assert engine.recent_updates(hours=24) == ["same text", "different text"]


def test_recent_updates_excludes_future_timestamped_entries():
    """Defensive: a corrupted/clock-skewed entry timestamped in the
    future must never be treated as recent."""

    engine = ProductionEngine(storage=FakeStorage())
    now = datetime.now(UTC)

    engine._record_recap(make_rss_event("from the future", created_at=now + timedelta(hours=5)))
    engine._record_recap(make_rss_event("normal", created_at=now))

    assert engine.recent_updates(hours=24) == ["normal"]


def test_recent_updates_ignores_legacy_bare_string_entries_without_crashing():
    """Backward compatibility: entries persisted before per-entry
    timestamps existed are bare strings, not {"created_at", "detail"}
    dicts. They must never crash recent_updates() -- they simply
    can't be time-windowed, so they're excluded rather than guessed
    at."""

    engine = ProductionEngine(storage=FakeStorage())
    now = datetime.now(UTC)

    legacy_buffer = ["a legacy update with no timestamp"]
    engine.storage.set(engine.RECAP_KEY, legacy_buffer)
    engine._record_recap(make_rss_event("current update", created_at=now))

    assert engine.recent_updates(hours=24) == ["current update"]


def test_recap_buffer_defaults_to_recap_window_hours_constant():
    engine = ProductionEngine(storage=FakeStorage())
    now = datetime.now(UTC)

    engine._record_recap(
        make_rss_event("just outside default window", created_at=now - timedelta(hours=25))
    )
    engine._record_recap(make_rss_event("inside", created_at=now - timedelta(hours=1)))

    assert engine.RECAP_WINDOW_HOURS == 24
    assert engine.recent_updates() == ["inside"]


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
    from services.ai_service import _extract_gemini_text as _extract_text

    response = FakeResponse(
        [FakePart("Hello, "), FakePart("Houseguest!")]
    )

    assert _extract_text(response) == "Hello, Houseguest!"


def test_extract_text_logs_non_stop_reason(capsys):
    from services.ai_service import _extract_gemini_text as _extract_text

    response = FakeResponse(
        [FakePart("cut off mid")], finish_reason="MAX_TOKENS"
    )

    _extract_text(response)

    captured = capsys.readouterr()
    assert "MAX_TOKENS" in captured.out


def test_extract_text_does_not_log_normal_stop(capsys):
    from services.ai_service import _extract_gemini_text as _extract_text

    response = FakeResponse([FakePart("complete.")], finish_reason="STOP")

    _extract_text(response)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_extract_text_falls_back_to_response_text_on_malformed_candidate():
    from services.ai_service import _extract_gemini_text as _extract_text

    class BrokenResponse:
        candidates = None  # will raise when indexed
        text = "fallback text"

    assert _extract_text(BrokenResponse()) == "fallback text"


def test_extract_text_falls_back_when_parts_are_empty():
    from services.ai_service import _extract_gemini_text as _extract_text

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
    tokens shouldn't starve the visible reply.

    max_output_tokens=2000 became max_tokens=2000 passed through to
    both providers when Groq-fallback was added, so this checks the
    call sites in generate_julie_response/generate_recap rather than
    a single Gemini-specific literal."""

    import inspect

    import services.ai_service as ai_service

    source = inspect.getsource(ai_service)

    assert source.count("max_tokens=2000") == 4, (
        "Expected both the Groq and Gemini call in each of "
        "generate_julie_response/generate_recap to request the "
        "raised token budget (2 functions x 2 providers = 4)"
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


# ==========================================================
# Groq-first, Gemini-fallback
# ==========================================================
#
# Added after Gemini's free tier was cut to 20 requests/day and
# real usage exhausted it mid-session. Groq (free, open-weight
# models) is tried first; Gemini is the fallback if Groq isn't
# configured or fails for any reason, so neither provider's own
# limits or outages take Julie's chat down alone.


class FakeGroqMessage:
    def __init__(self, content):
        self.content = content


class FakeGroqChoice:
    def __init__(self, content):
        self.message = FakeGroqMessage(content)


class FakeGroqResponse:
    def __init__(self, content):
        self.choices = [FakeGroqChoice(content)]


class FakeGroqClientSuccess:
    def __init__(self, content="Groq reply"):
        self._content = content
        self.calls = []

    class _Completions:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.calls.append(kwargs)
            return FakeGroqResponse(self.outer._content)

    @property
    def chat(self):
        outer = self

        class _Chat:
            completions = FakeGroqClientSuccess._Completions(outer)

        return _Chat()


class FakeGroqClientFailure:
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("simulated Groq failure")


class FakeGeminiPart:
    def __init__(self, text):
        self.text = text


class FakeGeminiContent:
    def __init__(self, text):
        self.parts = [FakeGeminiPart(text)]


class FakeGeminiFinishReason:
    name = "STOP"


class FakeGeminiCandidate:
    def __init__(self, text):
        self.content = FakeGeminiContent(text)
        self.finish_reason = FakeGeminiFinishReason()


class FakeGeminiResponse:
    def __init__(self, text):
        self.candidates = [FakeGeminiCandidate(text)]
        self.text = text


class FakeGeminiClientSuccess:
    def __init__(self, text="Gemini reply"):
        self.models = type(
            "M", (),
            {"generate_content": staticmethod(
                lambda **kwargs: FakeGeminiResponse(text)
            )},
        )()


class FakeGeminiClientFailure:
    class models:
        @staticmethod
        def generate_content(**kwargs):
            raise RuntimeError("simulated Gemini failure")


def _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=None):
    import services.ai_service as ai_service

    monkeypatch.setattr(
        ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat_history.db"
    )
    monkeypatch.setattr(ai_service, "groq_client", groq)
    monkeypatch.setattr(ai_service, "ai_client", gemini)
    return ai_service


def test_groq_messages_map_model_role_to_assistant():
    from services.ai_service import _to_groq_messages

    history = [("user", "hi", None), ("model", "hello", None)]
    messages = _to_groq_messages(history, "system prompt")

    assert messages[0] == {"role": "system", "content": "system prompt"}
    assert messages[1] == {"role": "user", "content": "hi"}
    assert messages[2] == {"role": "assistant", "content": "hello"}


def test_groq_messages_prefix_user_turns_with_author_name():
    from services.ai_service import _to_groq_messages

    history = [("user", "hi", "Bobby"), ("model", "hello", None)]
    messages = _to_groq_messages(history, "system prompt")

    assert messages[1] == {"role": "user", "content": "Bobby: hi"}
    # Julie's own prior replies are never prefixed.
    assert messages[2] == {"role": "assistant", "content": "hello"}


def test_gemini_contents_preserve_roles():
    from services.ai_service import _to_gemini_contents

    history = [("user", "hi", None), ("model", "hello", None)]
    contents = _to_gemini_contents(history)

    assert contents[0].role == "user"
    assert contents[1].role == "model"


def test_gemini_contents_prefix_user_turns_with_author_name():
    from services.ai_service import _to_gemini_contents

    history = [("user", "hi", "Bobby")]
    contents = _to_gemini_contents(history)

    assert contents[0].parts[0].text == "Bobby: hi"


def test_groq_tried_first_gemini_untouched(monkeypatch, tmp_path):
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=FakeGroqClientSuccess("Groq says hi"),
        gemini=FakeGeminiClientFailure(),  # would raise if ever called
    )

    reply = asyncio.run(
        ai_service.generate_julie_response(1, "hello")
    )

    assert reply == "Groq says hi"


def test_falls_back_to_gemini_when_groq_fails(monkeypatch, tmp_path):
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=FakeGroqClientFailure(),
        gemini=FakeGeminiClientSuccess("Gemini says hi"),
    )

    reply = asyncio.run(
        ai_service.generate_julie_response(2, "hello")
    )

    assert reply == "Gemini says hi"


def test_falls_back_when_groq_not_configured(monkeypatch, tmp_path):
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=None,
        gemini=FakeGeminiClientSuccess("Gemini says hi"),
    )

    reply = asyncio.run(
        ai_service.generate_julie_response(3, "hello")
    )

    assert reply == "Gemini says hi"


def test_friendly_error_when_both_providers_down(monkeypatch, tmp_path):
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=FakeGroqClientFailure(),
        gemini=FakeGeminiClientFailure(),
    )

    reply = asyncio.run(
        ai_service.generate_julie_response(4, "hello")
    )

    assert "Groq and Gemini" in reply


def test_friendly_error_when_neither_provider_configured(monkeypatch, tmp_path):
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path, groq=None, gemini=None,
    )

    reply = asyncio.run(
        ai_service.generate_julie_response(5, "hello")
    )

    assert "Groq and Gemini" in reply


def test_history_round_trips_regardless_of_which_provider_answered(
    monkeypatch, tmp_path,
):
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=FakeGroqClientSuccess("Groq reply"),
        gemini=FakeGeminiClientFailure(),
    )

    asyncio.run(ai_service.generate_julie_response(6, "hi Julie"))

    history = ai_service._recent_history(6)

    assert history[-2] == ("user", "hi Julie", None)
    assert history[-1] == ("model", "Groq reply", None)


def test_history_round_trips_with_author_identity(monkeypatch, tmp_path):
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=FakeGroqClientSuccess("Groq reply"),
        gemini=FakeGeminiClientFailure(),
    )

    asyncio.run(
        ai_service.generate_julie_response(
            8, "hi Julie", author_id=42, author_name="Bobby"
        )
    )

    history = ai_service._recent_history(8)

    assert history[-2] == ("user", "hi Julie", "Bobby")
    assert history[-1] == ("model", "Groq reply", None)


def test_recap_also_tries_groq_first(monkeypatch, tmp_path):
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=FakeGroqClientSuccess("Groq recap"),
        gemini=FakeGeminiClientFailure(),
    )

    reply = asyncio.run(ai_service.generate_recap(["update one"]))

    assert reply == "Groq recap"


def test_recap_falls_back_to_gemini(monkeypatch, tmp_path):
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=FakeGroqClientFailure(),
        gemini=FakeGeminiClientSuccess("Gemini recap"),
    )

    reply = asyncio.run(ai_service.generate_recap(["update one"]))

    assert reply == "Gemini recap"


def test_empty_groq_content_falls_back_to_gemini(monkeypatch, tmp_path):
    """An empty/None content from Groq must count as failure, not
    a successful empty reply."""

    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=FakeGroqClientSuccess(content=None),
        gemini=FakeGeminiClientSuccess("Gemini says hi"),
    )

    reply = asyncio.run(
        ai_service.generate_julie_response(7, "hello")
    )

    assert reply == "Gemini says hi"


# ==========================================================
# generate_recap: game state + Hamsterwatch source provenance
# ==========================================================
#
# /recap now combines three sources — tracked game state, recent
# Joker's Updates, and a small retrieved slice of Hamsterwatch
# history — and the model must never blur which is which. These
# tests check the actual prompt text sent to the provider, since
# that's the only thing enforcing that distinction at generation time.


def test_recap_still_works_with_only_entries_backward_compatible(monkeypatch, tmp_path):
    """The pre-existing call shape (positional entries, no kwargs)
    must keep working unchanged."""

    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path,
        groq=FakeGroqClientSuccess("Groq recap"),
        gemini=FakeGeminiClientFailure(),
    )

    reply = asyncio.run(ai_service.generate_recap(["update one"]))

    assert reply == "Groq recap"


def test_recap_returns_placeholder_when_nothing_to_summarize(monkeypatch, tmp_path):
    ai_service = _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=None)

    reply = asyncio.run(ai_service.generate_recap([]))

    assert reply == "Nothing new to recap yet, Houseguest."


def test_recap_prompt_labels_each_source_for_provenance(monkeypatch, tmp_path):
    groq = FakeGroqClientSuccess("recap reply")
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path, groq=groq, gemini=FakeGeminiClientFailure(),
    )

    asyncio.run(
        ai_service.generate_recap(
            ["Kamu went to the DR."],
            game_state="Head of Household: LaLa",
            hamsterwatch_entries=[
                "[Day 37 - 2026-08-12] Day 37 recap: LaLa and Devens talked strategy."
            ],
        )
    )

    prompt = groq.calls[0]["messages"][1]["content"]

    assert "CURRENT GAME STATE" in prompt
    assert "RECENT JOKER'S UPDATES" in prompt
    assert "RELEVANT HAMSTERWATCH HISTORY" in prompt
    assert "Head of Household: LaLa" in prompt
    assert "Kamu went to the DR." in prompt
    assert "Day 37" in prompt
    assert "LaLa and Devens talked strategy" in prompt
    # The instruction telling the model not to conflate the two sources.
    assert "Hamsterwatch" in prompt and "Joker's" in prompt


def test_recap_omits_sections_that_are_not_provided(monkeypatch, tmp_path):
    """A recap with no Hamsterwatch hits and no tracked game state
    must not print an empty labeled section for either."""

    groq = FakeGroqClientSuccess("recap reply")
    ai_service = _reset_ai_service_clients(
        monkeypatch, tmp_path, groq=groq, gemini=FakeGeminiClientFailure(),
    )

    asyncio.run(ai_service.generate_recap(["Kamu went to the DR."]))

    prompt = groq.calls[0]["messages"][1]["content"]

    assert "CURRENT GAME STATE" not in prompt
    assert "RELEVANT HAMSTERWATCH HISTORY" not in prompt
    assert "RECENT JOKER'S UPDATES" in prompt
