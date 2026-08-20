"""Tests proving the ACTUAL composed system instruction generate_julie_
response()/generate_recap() build and hand to each provider, not just
that format_learned_knowledge() produces reasonable-looking text in
isolation.

Follows the exact fake-client pattern already established in
tests/test_ai_service_threading.py (self-contained fakes matching the
precise shapes _try_groq_chat()/_try_gemini_chat() read), extended here
to capture the full call kwargs -- messages for Groq, config for
Gemini -- rather than just which thread ran the call.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import services.ai_service as ai_service
from production.competition import CompetitionState, CompetitionType
from production.house_status import HouseStatus
from production.knowledge import KnowledgeItem, KnowledgeType


def _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=None):
    monkeypatch.setattr(
        ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat_history.db"
    )
    monkeypatch.setattr(ai_service, "groq_client", groq)
    monkeypatch.setattr(ai_service, "ai_client", gemini)
    return ai_service


def make_groq_client_capturing(recorder: dict, content: str = "reply"):
    """Captures the full `messages` list passed to Groq's chat.completions.create()."""

    def create(**kwargs):
        recorder["messages"] = kwargs["messages"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def make_gemini_client_capturing(recorder: dict, content: str = "reply"):
    """Captures the `config.system_instruction` passed to Gemini's
    models.generate_content() -- the real SDK shape _try_gemini_chat()
    builds via types.GenerateContentConfig(system_instruction=...)."""

    def generate_content(**kwargs):
        recorder["system_instruction"] = kwargs["config"].system_instruction
        candidate = SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text=content)]),
            finish_reason=SimpleNamespace(name="STOP"),
        )
        return SimpleNamespace(candidates=[candidate], text=content)

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


KNOWLEDGE_TEXT = "ADMINISTRATOR-TAUGHT KNOWLEDGE.\n\n- Yash is HoH."
GAME_STATE_TEXT = "Current known Big Brother house state.\n- Head of Household: Yash"


# ==========================================================
# generate_julie_response(): knowledge reaches the real system
# instruction, in the intended position, for both provider paths
# ==========================================================


def test_generate_julie_response_places_knowledge_before_game_state_groq_path(
    monkeypatch, tmp_path
) -> None:
    recorder: dict = {}
    groq = make_groq_client_capturing(recorder)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    asyncio.run(
        svc.generate_julie_response(
            1, "who is HoH?", game_state=GAME_STATE_TEXT, knowledge=KNOWLEDGE_TEXT
        )
    )

    system_message = recorder["messages"][0]
    assert system_message["role"] == "system"
    content = system_message["content"]

    assert KNOWLEDGE_TEXT in content
    assert GAME_STATE_TEXT in content
    assert content.index(KNOWLEDGE_TEXT) < content.index(GAME_STATE_TEXT)
    # The persona instruction must still be present and lead everything.
    assert content.index(ai_service.SYSTEM_INSTRUCTION) < content.index(KNOWLEDGE_TEXT)


def test_generate_julie_response_places_knowledge_before_game_state_gemini_path(
    monkeypatch, tmp_path
) -> None:
    recorder: dict = {}
    gemini = make_gemini_client_capturing(recorder)
    # Groq unconfigured -> falls straight through to Gemini.
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=gemini)

    asyncio.run(
        svc.generate_julie_response(
            2, "who is HoH?", game_state=GAME_STATE_TEXT, knowledge=KNOWLEDGE_TEXT
        )
    )

    content = recorder["system_instruction"]

    assert KNOWLEDGE_TEXT in content
    assert GAME_STATE_TEXT in content
    assert content.index(KNOWLEDGE_TEXT) < content.index(GAME_STATE_TEXT)


def test_generate_julie_response_omits_knowledge_block_when_none_active(
    monkeypatch, tmp_path
) -> None:
    """No active knowledge -> knowledge="" -> the system instruction
    must not gain an empty/awkward extra section."""

    recorder: dict = {}
    groq = make_groq_client_capturing(recorder)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    asyncio.run(svc.generate_julie_response(3, "hello", game_state="", knowledge=""))

    content = recorder["messages"][0]["content"]
    assert content == ai_service.SYSTEM_INSTRUCTION


# ==========================================================
# generate_recap(): same guarantee, via the system message rather
# than the sourced-content user prompt
# ==========================================================


def test_generate_recap_places_knowledge_in_system_instruction_groq_path(
    monkeypatch, tmp_path
) -> None:
    recorder: dict = {}
    groq = make_groq_client_capturing(recorder, content="Recap text")
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    reply = asyncio.run(
        svc.generate_recap(["update one"], knowledge=KNOWLEDGE_TEXT)
    )

    assert reply == "Recap text"
    system_message = recorder["messages"][0]
    assert system_message["role"] == "system"
    assert KNOWLEDGE_TEXT in system_message["content"]
    # Knowledge belongs in the system instruction, not mixed into the
    # sourced-content user prompt (game_state/entries/hamsterwatch).
    user_message = recorder["messages"][1]
    assert user_message["role"] == "user"
    assert KNOWLEDGE_TEXT not in user_message["content"]


def test_generate_recap_places_knowledge_in_system_instruction_gemini_path(
    monkeypatch, tmp_path
) -> None:
    recorder: dict = {}
    gemini = make_gemini_client_capturing(recorder, content="Recap text")
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=gemini)

    reply = asyncio.run(
        svc.generate_recap(["update one"], knowledge=KNOWLEDGE_TEXT)
    )

    assert reply == "Recap text"
    assert KNOWLEDGE_TEXT in recorder["system_instruction"]


def test_generate_recap_without_knowledge_uses_bare_recap_system_instruction(
    monkeypatch, tmp_path
) -> None:
    """generate_recap() uses its own RECAP_SYSTEM_INSTRUCTION, not the
    shared SYSTEM_INSTRUCTION /chat uses -- toning down recap's
    catchphrase habit must never change conversational Julie
    elsewhere (see services/ai_service.py's comment on
    RECAP_SYSTEM_INSTRUCTION)."""

    recorder: dict = {}
    groq = make_groq_client_capturing(recorder, content="Recap text")
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    asyncio.run(svc.generate_recap(["update one"]))

    assert recorder["messages"][0]["content"] == ai_service.RECAP_SYSTEM_INSTRUCTION
    assert ai_service.RECAP_SYSTEM_INSTRUCTION != ai_service.SYSTEM_INSTRUCTION


def test_recap_system_instruction_does_not_mandate_catchphrases() -> None:
    """Content check, not exact-wording match: the instruction that
    made every recap open/close identically told the model to use
    'Expect the unexpected'/'Good evening, Houseguests' "naturally
    when starting conversations" (see SYSTEM_INSTRUCTION). The
    recap-specific instruction must explicitly say those lines are
    optional, not required, and must not tell the model to reach for
    them at the start of every recap the way SYSTEM_INSTRUCTION does."""

    instruction = ai_service.RECAP_SYSTEM_INSTRUCTION.lower()

    assert "not required" in instruction
    assert "naturally when starting conversations" not in instruction


def test_recap_prompt_still_instructs_source_grounding(monkeypatch, tmp_path) -> None:
    """The style refinement must not loosen factual grounding -- the
    user-role prompt (not just the system instruction) must still
    tell the model not to invent details."""

    recorder: dict = {}
    groq = make_groq_client_capturing(recorder, content="Recap text")
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    asyncio.run(svc.generate_recap(["update one"]))

    user_prompt = recorder["messages"][1]["content"].lower()
    assert "do not invent" in user_prompt
    assert "ground" in user_prompt


def test_recap_prompt_does_not_require_a_fixed_opening_or_closing(
    monkeypatch, tmp_path
) -> None:
    recorder: dict = {}
    groq = make_groq_client_capturing(recorder, content="Recap text")
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    asyncio.run(svc.generate_recap(["update one"]))

    user_prompt = recorder["messages"][1]["content"].lower()
    assert "no required greeting" in user_prompt or "without a catchphrase" in user_prompt


# ==========================================================
# Stale learned fact vs. fresher automated game state: the prompt
# must distinguish permanent rules, maintained facts, and current
# automated state -- not resolve the contradiction itself.
# ==========================================================


def test_stale_taught_fact_and_fresh_live_feed_are_both_present_and_distinctly_labeled(
    monkeypatch, tmp_path
) -> None:
    """Simulates the exact scenario the production bug was built on:
    an administrator taught 'Barrett is HoH' three weeks ago; the
    automated, RSS-driven live feed has since reported a different
    (unverified) name. Both pieces of information reach the model in
    clearly distinguished, differently-framed sections -- but unlike
    the old behavior, the taught fact is never told to defer to the
    automated live feed. It does NOT assert that any code computed
    which one is "right" -- no timestamp comparison, no ranking, no
    resolution logic exists or is expected here.
    """

    stale_fact = KnowledgeItem(
        id=1,
        type=KnowledgeType.FACT,
        content="Barrett is HoH.",
        author_id=1,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),  # three weeks "stale"
        updated_at=datetime(2026, 7, 24, tzinfo=UTC),
    )
    permanent_rule = KnowledgeItem(
        id=2,
        type=KnowledgeType.RULE,
        content="The house-status image is authoritative for Have-Nots.",
        author_id=1,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    knowledge = ai_service.format_learned_knowledge([permanent_rule, stale_fact])

    # An unverified automated live-feed observation, deliberately
    # disagreeing with the taught fact above.
    fresh_house_status = HouseStatus(hoh="Yash")
    fresh_competition = CompetitionState(
        competition=CompetitionType.HOH, winner="Yash"
    )
    game_state = ai_service.format_game_state(fresh_house_status, fresh_competition)

    recorder: dict = {}
    groq = make_groq_client_capturing(recorder)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    asyncio.run(
        svc.generate_julie_response(
            4, "who is HoH?", game_state=game_state, knowledge=knowledge
        )
    )

    content = recorder["messages"][0]["content"]

    # Both pieces of information actually reached the prompt.
    assert "Barrett is HoH." in content
    assert "Head of Household: Yash" in content

    # They are in distinctly labeled sections, not merged into one
    # undifferentiated block. The system-prompt guardrail mentions
    # "LIVE FEED OBSERVATION" by name earlier on (see SYSTEM_INSTRUCTION),
    # ahead of the knowledge section -- rindex() finds the actual
    # formatted live-feed block, not that earlier mention.
    rules_index = content.index("PERMANENT RULES")
    facts_index = content.index("ADMINISTRATOR-MAINTAINED FACTS")
    game_state_index = content.rindex("LIVE FEED OBSERVATION")
    assert rules_index < facts_index < game_state_index

    # The rule is framed as permanent; the fact section (where the
    # stale "Barrett is HoH" lives) is framed as capable of going
    # stale -- but, unlike the old behavior, it must NEVER instruct
    # the model to defer to the automated live feed. This is the
    # literal fix for the reported Taylor/Yash production bug's root
    # cause: the prompt itself used to tell the model to trust
    # automation over a taught fact.
    facts_section = content[facts_index:game_state_index]
    assert "prefer it" not in facts_section.lower()
    assert "never the unverified automated live feed" in facts_section.lower()
    live_feed_section = content[game_state_index:]
    assert "unverified" in live_feed_section.lower()


# ==========================================================
# format_official_state()
# ==========================================================


def test_format_official_state_empty_when_nothing_taught() -> None:
    class _EmptyKnowledge:
        def active_items(self):
            return []

    assert ai_service.format_official_state(_EmptyKnowledge()) == ""


def test_format_official_state_lists_every_active_state_topic() -> None:
    hoh = KnowledgeItem(
        id=1, type=KnowledgeType.STATE, content="Yash", author_id=1,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC), topic="HOH",
    )
    evicted = KnowledgeItem(
        id=2, type=KnowledgeType.STATE, content="Angela", author_id=1,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC), topic="EVICTED",
    )
    # A non-STATE item must never leak into the official-facts block.
    unrelated_fact = KnowledgeItem(
        id=3, type=KnowledgeType.FACT, content="Yash is funny.", author_id=1,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
    )

    class _Knowledge:
        def active_items(self):
            return [hoh, evicted, unrelated_fact]

    text = ai_service.format_official_state(_Knowledge())

    assert "OFFICIAL GAME FACTS" in text
    assert "Yash" in text
    assert "Angela" in text
    assert "Yash is funny." not in text  # FACT items are not official state


def test_official_state_outranks_taught_facts_and_live_feed_in_prompt_order(
    monkeypatch, tmp_path
) -> None:
    """Regression test for the reported Taylor/Yash bug at the prompt-
    assembly level: official_state must appear ahead of both taught
    knowledge and the live-feed observation in the composed system
    instruction."""

    official = ai_service.format_official_state(
        SimpleNamespace(active_items=lambda: [
            KnowledgeItem(
                id=1, type=KnowledgeType.STATE, content="Yash", author_id=1,
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
                updated_at=datetime(2026, 8, 1, tzinfo=UTC), topic="HOH",
            )
        ])
    )
    game_state = ai_service.format_game_state(HouseStatus(hoh="Taylor"), CompetitionState())

    recorder: dict = {}
    groq = make_groq_client_capturing(recorder)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    asyncio.run(
        svc.generate_julie_response(
            9, "who is HoH?", official_state=official, game_state=game_state
        )
    )

    content = recorder["messages"][0]["content"]
    assert content.index("OFFICIAL GAME FACTS") < content.rindex("LIVE FEED OBSERVATION")


# ==========================================================
# format_long_term_memory()
# ==========================================================


def test_format_long_term_memory_empty_when_nothing_remembered() -> None:
    assert ai_service.format_long_term_memory([]) == ""


def test_format_long_term_memory_is_labeled_and_not_official(monkeypatch, tmp_path) -> None:
    from production.memory import MemoryItem

    item = MemoryItem(
        id=1, channel_id=1, author_id=1, author_name="Bobby",
        content="we call the Have-Not room the Slop Dungeon",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )

    text = ai_service.format_long_term_memory([item])

    assert "REMEMBERED CONTEXT" in text
    assert "Slop Dungeon" in text
    assert "NOT an official game fact" in text
