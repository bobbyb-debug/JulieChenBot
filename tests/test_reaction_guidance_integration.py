"""Integration/boundary tests for the situational reaction engine's
wiring into services/ai_service.py -- format_reaction_guidance(), the
SYSTEM_INSTRUCTION/RECAP_SYSTEM_INSTRUCTION epistemic-discipline
additions, generate_julie_response()'s SITUATIONAL REACTION block, and
generate_recap()'s opt-in intensity-gated note.

Mirrors tests/test_hosting_guidance_boundaries.py's structure and
philosophy: prove the DETERMINISTIC routing/rendering guarantees, not
nondeterministic AI-generated prose. See tests/test_reaction_engine.py
for production/reaction_engine.py's own unit tests.
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
from production.reaction_engine import ReactionContext, SituationalEvent
from services.discord import DiscordService
from services.logger import ProductionLogger


# ==========================================================
# format_reaction_guidance() -- structural rendering guarantees.
# ==========================================================


def test_format_reaction_guidance_never_contains_a_game_fact():
    """Same structural guarantee as
    test_hosting_guidance_content_is_fully_determined_by_fixed_inputs():
    ReactionContext is built from only an enum, an int, and three
    bools -- there is no parameter through which a game fact (a player
    name, a HOH value) could reach the rendered text."""

    context = ReactionContext(
        event=SituationalEvent.BLINDSIDE,
        intensity=4,
        opinion_requested=True,
        user_banter=True,
        user_challenges_julie=True,
    )
    text = ai_service.format_reaction_guidance(context)

    assert "SITUATIONAL REACTION" in text
    assert "blindside" in text.lower()
    # No player names, no HOH/nominee/veto values -- only fixed
    # guidance prose from _EVENT_GUIDANCE/_INTENSITY_GUIDANCE tables.
    for forbidden in ("dee", "drew", "taylor", "lala"):
        assert forbidden not in text.lower()


def test_format_reaction_guidance_is_deterministic():
    context = ReactionContext(event=SituationalEvent.COMP_WIN, intensity=2)
    assert ai_service.format_reaction_guidance(context) == ai_service.format_reaction_guidance(
        context
    )


def test_format_reaction_guidance_ordinary_context_has_no_optional_lines():
    context = ReactionContext()  # all defaults -- NONE/0/False/False/False
    text = ai_service.format_reaction_guidance(context)

    assert "nothing here calls for a heightened reaction" in text.lower()
    assert "give one" not in text.lower()  # opinion_requested line absent
    assert "match that energy" not in text.lower()  # user_banter line absent


def test_event_guidance_table_covers_every_situational_event():
    from production.reaction_engine import SituationalEvent as _SE

    assert set(ai_service._EVENT_GUIDANCE.keys()) == {e.value for e in _SE}


def test_intensity_guidance_table_covers_full_range():
    assert set(ai_service._INTENSITY_GUIDANCE.keys()) == {0, 1, 2, 3, 4}


# ==========================================================
# SYSTEM_INSTRUCTION / RECAP_SYSTEM_INSTRUCTION additions -- the
# epistemic-labeling discipline (FACT/OBSERVATION/INTERPRETATION/
# OPINION/SPECULATION/UNCERTAINTY) required by this feature.
# ==========================================================


def test_system_instruction_teaches_the_epistemic_labels():
    lowered = ai_service.SYSTEM_INSTRUCTION.lower()
    for label in ("fact", "observation", "interpretation", "opinion", "speculation"):
        assert label in lowered
    assert "uncertain" in lowered or "genuinely don't have" in lowered


def test_system_instruction_permits_opinions_teasing_and_changing_her_mind():
    lowered = ai_service.SYSTEM_INSTRUCTION.lower()
    assert "real opinions" in lowered
    assert "tease" in lowered
    assert "that changes my read" in lowered


def test_system_instruction_still_never_treats_speculation_as_confirmed():
    # R1/original boundary paragraph must still be intact -- this
    # feature is additive, not a rewrite of the existing trust rules.
    lowered = ai_service.SYSTEM_INSTRUCTION.lower()
    assert "official game facts" in lowered
    assert "never becomes an official fact merely because you said it" in lowered


def test_recap_system_instruction_permits_a_closing_read_after_the_facts():
    lowered = ai_service.RECAP_SYSTEM_INSTRUCTION.lower()
    assert "after covering what actually happened" in lowered
    assert "never in place of it" in lowered


def test_system_instruction_teaches_conversational_momentum():
    lowered = ai_service.SYSTEM_INSTRUCTION.lower()
    assert "conversational momentum" in lowered
    assert "conversation history" in lowered


# ==========================================================
# generate_julie_response() -- SITUATIONAL REACTION block presence and
# regression guards on everything already there (R1's DIRECT_FACT
# fix, HOSTING GUIDANCE).
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
            message = SimpleNamespace(content=self.outer._reply_text)
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


def test_prompt_contains_situational_reaction_block(tmp_path, monkeypatch):
    host, knowledge = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=600, text="That was such a blindside!!!")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "SITUATIONAL REACTION" in prompt
    assert "genuine blindside" in prompt.lower()


def test_ordinary_question_gets_the_ordinary_reaction_line(tmp_path, monkeypatch):
    host, knowledge = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=601, text="Who is the current HOH?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    start = prompt.index("SITUATIONAL REACTION")
    reaction_block = prompt[start:].lower()
    assert "nothing here calls for a heightened reaction" in reaction_block


def test_reaction_block_never_regresses_r1_direct_fact_guidance(tmp_path, monkeypatch):
    """The DIRECT_FACT sentence-ceiling regression (R1) must stay
    fixed -- this feature is additive alongside HOSTING GUIDANCE, not
    a replacement for it."""

    host, knowledge = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=602, text="Who is the current HoH?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "HOSTING GUIDANCE FOR THIS REPLY" in prompt
    start = prompt.index("HOSTING GUIDANCE FOR THIS REPLY")
    end = prompt.index("SITUATIONAL REACTION")
    hosting_block = prompt[start:end].lower()
    assert "1-3 sentences" not in hosting_block
    assert "lead with the actual answer" in hosting_block


def test_situational_reaction_is_the_final_block_in_the_prompt(tmp_path, monkeypatch):
    host, knowledge = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=603, text="Who is the current HOH?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    hosting_idx = prompt.index("HOSTING GUIDANCE FOR THIS REPLY")
    reaction_idx = prompt.index("SITUATIONAL REACTION")
    assert reaction_idx > hosting_idx


def test_reaction_context_is_logged_content_free(tmp_path, monkeypatch, caplog):
    """Dev/debug observability (no new logging system -- reuses the
    existing ProductionLogger, same as ContextBudgetReport.log_line())."""

    import logging as _logging

    host, knowledge = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    with caplog.at_level(_logging.INFO, logger="AIService"):
        _ask(host, channel_id=604, text="Can you believe that blindside?!")

    assert "Reaction: event=BLINDSIDE" in caplog.text
    assert "intensity=" in caplog.text
    # Never the raw message text in the log.
    assert "believe" not in caplog.text.lower()


def test_fixed_overhead_never_pushes_total_prompt_over_the_safe_ceiling(tmp_path, monkeypatch):
    """The reaction-engine feature added SYSTEM_INSTRUCTION text and a
    new always-included SITUATIONAL REACTION block -- both outside
    allocate_context_budget()'s own inclusion/exclusion. Without
    reserving their real cost against MAX_PROMPT_TOKENS before calling
    the allocator, a fully-taught knowledge base plus a full
    conversation history could combine with this fixed overhead to
    exceed Groq's real TPM limit again (see generate_julie_response()'s
    dynamic_budget calculation). This proves the fix holds even in a
    deliberately worst-case scenario."""

    from production.context_budget import GROQ_TPM_LIMIT, RESERVED_OUTPUT_TOKENS, estimate_tokens

    host, knowledge = _setup(tmp_path, monkeypatch)

    # Force every allocator-budgeted block to be enormous -- far beyond
    # what MAX_PROMPT_TOKENS alone would allow if fixed overhead were
    # not reserved for.
    for i in range(30):
        knowledge.teach(
            KnowledgeType.FACT, f"Some very long taught fact number {i} " * 40, author_id=1
        )

    # A long, excitement-heavy, blindside-shaped message (worst case for
    # both SITUATIONAL REACTION's optional lines AND fixed overhead).
    _ask(
        host, channel_id=699,
        text="OMG did you see that blindside??? What do you think, lol, that's wrong right?!",
    )

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    total_estimate = estimate_tokens(prompt)
    safe_ceiling = GROQ_TPM_LIMIT - RESERVED_OUTPUT_TOKENS

    assert total_estimate <= safe_ceiling, (
        f"prompt estimate {total_estimate} exceeds the safe ceiling {safe_ceiling} -- "
        "this would reproduce the original Groq 413 incident"
    )


def test_gemini_fallback_receives_the_same_situational_reaction_block(tmp_path, monkeypatch):
    """Groq-first/Gemini-fallback must see an identical, already-
    assembled system_instruction -- SITUATIONAL REACTION included --
    since both providers are called with the same variable. Forces
    Groq to fail so the Gemini path actually runs."""

    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    monkeypatch.setattr(ai_service, "groq_client", object())  # present but will raise/fail

    def _failing_groq(*args, **kwargs):
        return None

    monkeypatch.setattr(ai_service, "_try_groq_chat", _failing_groq)

    captured = {}

    def _fake_gemini(contents, system_instruction, max_tokens, temperature):
        captured["system_instruction"] = system_instruction
        return "Dee is the current HOH."

    monkeypatch.setattr(ai_service, "ai_client", object())  # present so Gemini path is tried
    monkeypatch.setattr(ai_service, "_try_gemini_chat", _fake_gemini)

    from database.storage import Storage
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    from production.knowledge import KnowledgeStore
    knowledge = KnowledgeStore(storage=storage)
    memory = MemoryStore(storage=storage)
    historical_events = HistoricalEventStore(db_path=tmp_path / "historical_events.db")
    engine = _FakeEngine(knowledge, memory, historical_events)
    host = _FakeDiscordServiceHost(engine)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=605, text="Can you believe that blindside?!")

    assert "SITUATIONAL REACTION" in captured["system_instruction"]
    assert "genuine blindside" in captured["system_instruction"].lower()


# ==========================================================
# generate_recap() -- opt-in intensity-gated note, and the
# "information first" guarantee (Section 11's core requirement).
# ==========================================================


def _setup_recap(monkeypatch, reply_text: str = "Recap text."):
    client = _CapturingGroqClient(reply_text)
    monkeypatch.setattr(ai_service, "groq_client", client)
    monkeypatch.setattr(ai_service, "ai_client", None)
    return client


def test_recap_ordinary_entries_get_no_situational_note(monkeypatch):
    client = _setup_recap(monkeypatch, "A quiet day in the house.")

    asyncio.run(ai_service.generate_recap(["Someone made a sandwich in the kitchen."]))

    prompt = client.calls[0]["messages"][-1]["content"]
    assert "reads like it includes a genuinely major moment" not in prompt


def test_recap_significant_entries_get_an_opt_in_note_after_the_facts(monkeypatch):
    client = _setup_recap(monkeypatch, "A wild day in the house.")

    asyncio.run(
        ai_service.generate_recap(["In a total blindside, the house evicted Drew tonight."])
    )

    prompt = client.calls[0]["messages"][-1]["content"]
    assert "reads like it includes a genuinely major moment" in prompt
    # The note must come AFTER the actual entries section, never before it.
    entries_idx = prompt.index("RECENT JOKER'S UPDATES")
    note_idx = prompt.index("reads like it includes a genuinely major moment")
    assert note_idx > entries_idx


def test_recap_system_instruction_used_for_recap_not_chat_system_instruction(monkeypatch):
    """Regression guard, mirroring
    tests/test_ai_service_knowledge_context.py's existing coverage:
    generate_recap() must keep using its own RECAP_SYSTEM_INSTRUCTION,
    not the chat-path SYSTEM_INSTRUCTION -- this feature must not have
    merged the two."""

    client = _setup_recap(monkeypatch)

    asyncio.run(ai_service.generate_recap(["update one"]))

    system_prompt = client.calls[0]["messages"][0]["content"]
    assert "writing a live-feed recap" in system_prompt.lower()
    assert "OFFICIAL GAME FACTS" not in system_prompt
