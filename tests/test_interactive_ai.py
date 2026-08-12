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


def test_thinking_budget_is_disabled_in_config():
    from google.genai import types

    config = types.GenerateContentConfig(
        max_output_tokens=600,
        temperature=0.8,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    assert config.thinking_config.thinking_budget == 0


def test_ai_service_configs_disable_thinking():
    """Guards against someone editing ai_service.py and dropping the
    thinking_config line without noticing."""

    import inspect

    import services.ai_service as ai_service

    source = inspect.getsource(ai_service)

    assert source.count("thinking_config=types.ThinkingConfig(thinking_budget=0)") == 2, (
        "Expected thinking to be disabled in both "
        "generate_julie_response and generate_recap"
    )
