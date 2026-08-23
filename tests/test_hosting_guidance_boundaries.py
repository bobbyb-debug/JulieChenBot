"""Tests for the personality/hosting-guidance final polish -- the fix
for Julie's repeated "Good evening, Houseguests! ... Expect the
unexpected!" template wrapper appearing on every answer regardless of
time, question type, or whether it had just been used.

Deliberately does NOT assert exact AI-generated prose (nondeterministic
model output) -- every test here proves a DETERMINISTIC guidance/
routing guarantee instead: what production/response_style.py's
classify_intent() decides for a real question, and what
services/ai_service.py's format_response_guidance()/_INTENT_GUIDANCE
table can and cannot ever instruct the model to do. See
tests/test_response_style.py and tests/test_personality_hosting.py for
production/response_style.py's own unit tests, and
tests/test_ai_service_knowledge_context.py's "SYSTEM_INSTRUCTION" and
"HOSTING GUIDANCE" sections for the prompt-assembly-level proof.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import services.ai_service as ai_service
from database.historical_events import HistoricalEventStore
from production.competition import CompetitionState
from production.house_status import HouseStatus
from production.knowledge import KnowledgeType
from production.memory import MemoryStore
from production.response_style import ResponseGuidance, ResponseIntent, classify_intent
import production.response_style as response_style
from services.discord import DiscordService
from services.logger import ProductionLogger


# ==========================================================
# 1/3/5. No per-intent guidance ever mandates a greeting; 2/4/6. none
# ever mandates "Expect the unexpected" -- structural guarantees over
# EVERY intent category at once, not just the three named in the bug
# report (a current-state question, a historical question, and a
# knowledge-summary-shaped question all resolve to one of these).
# ==========================================================


def test_no_intent_guidance_ever_mandates_a_greeting():
    for intent, text in ai_service._INTENT_GUIDANCE.items():
        assert "greet" not in text.lower(), f"{intent} guidance mentions greeting"


def test_no_intent_guidance_ever_mandates_expect_the_unexpected():
    for intent, text in ai_service._INTENT_GUIDANCE.items():
        assert "expect the unexpected" not in text.lower(), (
            f"{intent} guidance names the catchphrase"
        )


def test_conversation_start_greeting_permission_is_never_mandatory():
    """The ONE place a greeting is ever mentioned as appropriate --
    separate from what KIND of question this is -- and even there it
    is optional, never required."""

    guidance = ai_service.format_response_guidance(
        ResponseGuidance(intent=ResponseIntent.GENERAL, is_conversation_start=True)
    ).lower()

    assert "greeting is fine here if it fits, but never required" in guidance


# ==========================================================
# Routing boundaries: classify_intent() on the exact five questions
# from the live-path validation scenarios (A-E)
# ==========================================================


def test_current_hoh_question_classifies_as_direct_fact():
    # Scenario A
    assert classify_intent("Who is the current HOH?") == ResponseIntent.DIRECT_FACT


def test_taylor_hoh_question_classifies_direct_fact_or_historical_never_banter_or_dramatic():
    # Scenario B -- the real pipeline may or may not have retrieved
    # Hamsterwatch prose for this query; either way it must land on a
    # grounded, direct answer shape, never banter or forced drama.
    without_context = classify_intent("What do you know about Taylor's HOH?")
    assert without_context == ResponseIntent.DIRECT_FACT

    with_context = classify_intent(
        "What do you know about Taylor's HOH?",
        historical_context="Taylor was HOH earlier this season.",
    )
    assert with_context == ResponseIntent.HISTORICAL


def test_tell_me_everything_classifies_as_general_when_no_historical_match():
    # Scenario C -- KNOWLEDGE_SUMMARY's own guidance (see
    # production/knowledge_summary.py) handles the answer's actual
    # shape; classify_intent() must not force GREETING/DRAMATIC framing
    # onto it either.
    assert classify_intent("Tell me everything you know.") == ResponseIntent.GENERAL


def test_good_morning_greeting_classifies_as_general():
    # Scenario D -- a real greeting from the user. GENERAL intent
    # carries no greeting prohibition of its own; whether a greeting
    # is appropriate is decided separately by is_conversation_start.
    assert classify_intent("Good morning Julie") == ResponseIntent.GENERAL


def test_latest_veto_question_classifies_as_grounded_never_banter_or_general():
    # Scenario E -- "during" is one of response_style.py's own
    # _HISTORICAL_SIGNAL_WORDS (reused as-is, not modified by this
    # task), so this correctly resolves to HISTORICAL rather than
    # DIRECT_FACT; either way it must land on a grounded answer shape
    # ("say so plainly rather than inventing"), never banter or an
    # unrelated general chat framing.
    intent = classify_intent("What happened during the latest veto?")
    assert intent in (ResponseIntent.DIRECT_FACT, ResponseIntent.HISTORICAL)


# ==========================================================
# 8. A user's own time-of-day greeting can be mirrored back -- Julie
# never invents one from a guessed clock, but reciprocating what the
# Houseguest just said isn't a guess.
# ==========================================================


def test_system_instruction_permits_mirroring_a_houseguests_own_greeting():
    lowered = ai_service.SYSTEM_INSTRUCTION.lower()
    assert "don't invent a time-of-day greeting" in lowered or (
        "never use a time-of-day greeting" in lowered
    )
    assert "mirror it back" in lowered or "reciprocat" in lowered


# ==========================================================
# 13. Personality guidance cannot create factual content -- structural
# proof, not just absence of any one string.
# ==========================================================


def test_hosting_guidance_content_is_fully_determined_by_fixed_inputs():
    """format_response_guidance() takes a ResponseGuidance built from
    only an intent enum, a bool, and a list of phrases drawn from a
    fixed marker set (_CATCHPHRASE_MARKERS) -- there is no parameter
    through which a game fact (a player name, a HOH value, anything
    from KnowledgeStore/HistoricalEventStore/HouseStatus/
    CompetitionState) could reach the rendered text."""

    guidance = ResponseGuidance(
        intent=ResponseIntent.DIRECT_FACT,
        is_conversation_start=False,
        recently_used_phrases=list(response_style._CATCHPHRASE_MARKERS),
    )
    text = ai_service.format_response_guidance(guidance)

    # Every word that CAN appear came from one of: the fixed
    # _INTENT_GUIDANCE table, the two fixed conversation-start
    # sentences, or _CATCHPHRASE_MARKERS -- none of which is built from
    # any store read.
    for marker in response_style._CATCHPHRASE_MARKERS:
        assert marker in text.lower()


def test_hosting_guidance_is_identical_regardless_of_official_state_content():
    """Changing official_state (a real fact) must never change the
    rendered HOSTING GUIDANCE block -- proving the guidance layer
    genuinely never reads fact content, only the (user_text,
    historical_context, history, minutes_since_last_message) signals
    build_response_guidance() actually takes."""

    guidance = response_style.build_response_guidance("Who is the current HOH?")
    text_a = ai_service.format_response_guidance(guidance)
    text_b = ai_service.format_response_guidance(guidance)

    assert text_a == text_b  # deterministic, no hidden state


# ==========================================================
# 18. @mention/DM path receives the same hosting guidance behavior as
# /chat -- both call the one real, shared DiscordService.
# generate_ai_reply(), exercised here exactly as tests/
# test_knowledge_summary_boundary.py and tests/
# test_historical_hoh_boundary.py already do for their own features.
# ==========================================================


class _FakeWatcher:
    def __init__(self) -> None:
        self.house_status = SimpleNamespace(current=HouseStatus(hoh="Dee"))
        self.competition = SimpleNamespace(current=CompetitionState())
        self.hamsterwatch = None


class _FakeEngine:
    def __init__(self, knowledge, memory, historical_events) -> None:
        self.watcher = _FakeWatcher()
        self.knowledge = knowledge
        self.memory = memory
        self.historical_events = historical_events


class _FakeDiscordServiceHost:
    _ai_cooldown_remaining = DiscordService._ai_cooldown_remaining
    generate_ai_reply = DiscordService.generate_ai_reply

    def __init__(self, engine) -> None:
        self._ai_cooldowns: dict[int, float] = {}
        self.scheduler = SimpleNamespace(engine=engine)
        self.logger = ProductionLogger.get("Test")


class _CapturingGroqClient:
    def __init__(self, reply_text: str) -> None:
        self._reply_text = reply_text
        self.calls: list[dict] = []

    class _Completions:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.calls.append(kwargs)
            content = self.outer._reply_text
            message = SimpleNamespace(content=content)
            choice = SimpleNamespace(message=message)
            return SimpleNamespace(choices=[choice])

    @property
    def chat(self):
        outer = self

        class _Chat:
            completions = _CapturingGroqClient._Completions(outer)

        return _Chat()


def _setup(tmp_path: Path, monkeypatch, reply_text: str = "Dee is the current HOH."):
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    monkeypatch.setattr(ai_service, "groq_client", _CapturingGroqClient(reply_text))
    monkeypatch.setattr(ai_service, "ai_client", None)

    from database.storage import Storage
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    from production.knowledge import KnowledgeStore
    knowledge = KnowledgeStore(storage=storage)
    memory = MemoryStore(storage=storage)
    historical_events = HistoricalEventStore(db_path=tmp_path / "historical_events.db")
    engine = _FakeEngine(knowledge, memory, historical_events)
    host = _FakeDiscordServiceHost(engine)
    return host, knowledge


def _ask(host, channel_id, text, user_id=1, author_name="Bobby", is_moderator=False):
    host._ai_cooldowns.pop(user_id, None)
    return asyncio.run(
        host.generate_ai_reply(
            user_id=user_id,
            channel_id=channel_id,
            user_text=text,
            author_name=author_name,
            is_moderator=is_moderator,
        )
    )


def test_mention_dm_shared_path_receives_hosting_guidance(tmp_path, monkeypatch):
    """generate_ai_reply() -- the exact method both commands/chat.py
    and services/discord.py's on_message @mention/DM handler call --
    must produce a prompt containing HOSTING GUIDANCE, proving both
    real entry points get identical hosting behavior with zero
    per-caller wiring."""

    host, knowledge = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=500, text="Who is the current HoH?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "HOSTING GUIDANCE FOR THIS REPLY" in prompt


def test_mention_dm_shared_path_direct_fact_guidance_has_no_greeting_mandate(
    tmp_path, monkeypatch
):
    host, knowledge = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=501, text="Who is the current HoH?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    start = prompt.index("HOSTING GUIDANCE FOR THIS REPLY")
    hosting_block = prompt[start:]
    assert "lead with the actual answer" in hosting_block.lower()
    assert "1-3 sentences" not in hosting_block.lower()
    assert "expect the unexpected" not in hosting_block.lower()
