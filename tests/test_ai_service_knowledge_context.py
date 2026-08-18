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


def test_stale_taught_fact_and_fresh_game_state_are_both_present_and_distinctly_labeled(
    monkeypatch, tmp_path
) -> None:
    """Simulates the exact scenario: an administrator taught 'Barrett
    is HoH' three weeks ago; the automated monitors have since
    correctly detected a new HoH. Both pieces of information reach the
    model -- this test proves the composed prompt keeps them in
    clearly distinguished sections with different framing, rather than
    silently dropping one or presenting them as equally-weighted,
    unresolved statements. It does NOT assert that any code computed
    which one is "right" -- no timestamp comparison, no ranking, no
    resolution logic exists or is expected here; that judgment is
    deliberately left to the model, guided by the wording alone.
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

    fresh_house_status = HouseStatus(hoh="Yash")  # automated monitor's current answer
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
    # undifferentiated block.
    rules_index = content.index("PERMANENT RULES")
    facts_index = content.index("ADMINISTRATOR-MAINTAINED FACTS")
    game_state_index = content.index("Current known Big Brother house state")
    assert rules_index < facts_index < game_state_index

    # The rule is framed as permanent; the fact section (which is
    # where the stale "Barrett is HoH" lives) is explicitly framed as
    # capable of going stale and defers to newer automated state --
    # this is the actual distinction being tested, expressed as
    # prompt wording, not as any staleness-computing code.
    facts_section = content[facts_index:game_state_index]
    assert "outdated" in facts_section.lower()
    assert "newer" in facts_section.lower()
