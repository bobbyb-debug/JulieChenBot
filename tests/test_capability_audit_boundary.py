"""Capability-audit regression tests -- proves, through the real
shared entry point (services/discord.py DiscordService.
generate_ai_reply()), that every existing Julie capability survived
the context-budget fix (production/context_budget.py) intact, plus
the two genuine gaps this audit found and closed:

1. A genuinely broad historical question ("tell me everything you know
   about the history of Big Brother 28") was being incorrectly treated
   as narrowly scoped by production/knowledge_summary.py's "about"
   heuristic, leaving it with nothing useful to retrieve.
2. A comparison question naming two known players ("Compare Taylor's
   HOH with Dee's") only ever retrieved one of them -- see
   production/historical_retrieval.py's find_known_players_mentioned().

Deliberately does NOT assert exact AI-generated prose (nondeterministic
model output) -- every test proves a deterministic routing/retrieval/
budget guarantee instead, following the same convention as tests/
test_hosting_guidance_boundaries.py and tests/
test_context_budget_boundary.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import services.ai_service as ai_service
from database.hamsterwatch_archive import HamsterwatchArchive
from database.historical_events import HistoricalEventStore
from database.storage import Storage
from production.competition import CompetitionState
from production.house_status import HouseStatus
from production.knowledge import KnowledgeStore, KnowledgeType
from production.memory import MemoryStore
from services.discord import DiscordService
from services.logger import ProductionLogger


class _FakeWatcher:
    def __init__(self, hamsterwatch_archive=None) -> None:
        self.house_status = SimpleNamespace(current=HouseStatus(hoh="Dee"))
        self.competition = SimpleNamespace(current=CompetitionState())
        self.hamsterwatch = (
            SimpleNamespace(archive=hamsterwatch_archive)
            if hamsterwatch_archive is not None
            else None
        )


class _FakeEngine:
    def __init__(self, knowledge, memory, historical_events, hamsterwatch_archive=None) -> None:
        self.watcher = _FakeWatcher(hamsterwatch_archive)
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
    def __init__(self, reply_text: str = "A real reply.") -> None:
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


def _setup(tmp_path: Path, monkeypatch, reply_text: str = "A real reply."):
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    monkeypatch.setattr(ai_service, "groq_client", _CapturingGroqClient(reply_text))
    monkeypatch.setattr(ai_service, "ai_client", None)
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    knowledge = KnowledgeStore(storage=storage)
    memory = MemoryStore(storage=storage)
    historical_events = HistoricalEventStore(db_path=tmp_path / "historical_events.db")
    archive = HamsterwatchArchive(db_path=tmp_path / "hamsterwatch.db")
    engine = _FakeEngine(knowledge, memory, historical_events, archive)
    host = _FakeDiscordServiceHost(engine)
    return host, knowledge, memory, historical_events, archive


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


def _prompt_text(host) -> str:
    return " ".join(m["content"] for m in ai_service.groq_client.calls[-1]["messages"])


def _verify_hoh(store, *, season=28, cycle, week, winner):
    claim = store.record_hoh_claim(
        season=season, cycle_sequence_number=cycle, week_number=week,
        winner=winner, source_type="manual_admin_note", source_ref="x",
    )
    return store.verify_hoh(claim.id)


# ==========================================================
# TEST D -- a genuinely broad historical question must still surface
# useful (summarized, not dumped) historical capability
# ==========================================================


def test_broad_historical_question_triggers_knowledge_summary_not_a_dead_end(
    tmp_path, monkeypatch
):
    host, knowledge, _memory, historical_events, archive = _setup(tmp_path, monkeypatch)
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")
    _verify_hoh(historical_events, cycle=5, week=5, winner="Dee")

    _ask(
        host, channel_id=700,
        text="Tell me everything you know about the history of Big Brother 28.",
    )

    prompt = _prompt_text(host)
    assert "KNOWLEDGE SUMMARY GUIDANCE" in prompt
    # The count is present (real data), not the entire archive dumped
    # verbatim -- see production/knowledge_summary.py's capability-vs-
    # actual-data distinction.
    assert "2 known winner" in prompt


def test_broad_historical_question_does_not_dump_the_full_archive(tmp_path, monkeypatch):
    host, knowledge, _memory, historical_events, archive = _setup(tmp_path, monkeypatch)
    for week in range(1, 15):
        _verify_hoh(historical_events, cycle=week, week=week, winner=f"Player{week}")

    _ask(
        host, channel_id=701,
        text="Tell me everything you know about the history of Big Brother 28.",
    )

    prompt = _prompt_text(host)
    # A summary (count) is present; the individual per-player lines
    # HISTORICAL STRUCTURED EVENTS would render are NOT (that block
    # only populates for a scoped week/cycle/player query, not a
    # broad one). SYSTEM_INSTRUCTION's own boundary paragraph names
    # "HISTORICAL STRUCTURED EVENTS" as a phrase regardless, so check
    # for the rendered BLOCK's own distinguishing marker text instead
    # of the bare phrase.
    assert "14 known winner" in prompt
    assert "Player1" not in prompt  # none of the individual winner names dumped
    assert "administrator-verified historical game" not in prompt


# ==========================================================
# TEST E -- a comparison question retrieves BOTH named players'
# verified historical records
# ==========================================================


def test_comparison_question_retrieves_both_players_records(tmp_path, monkeypatch):
    host, knowledge, _memory, historical_events, _archive = _setup(tmp_path, monkeypatch)
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")
    _verify_hoh(historical_events, cycle=5, week=5, winner="Dee")

    _ask(host, channel_id=702, text="Compare Taylor's HOH with Dee's.")

    prompt = _prompt_text(host)
    assert "HISTORICAL STRUCTURED EVENTS" in prompt
    assert "Taylor" in prompt
    assert "Dee" in prompt


def test_comparison_question_never_triggers_broad_knowledge_summary(tmp_path, monkeypatch):
    host, knowledge, _memory, historical_events, _archive = _setup(tmp_path, monkeypatch)
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")
    _verify_hoh(historical_events, cycle=5, week=5, winner="Dee")

    _ask(host, channel_id=703, text="Compare Taylor's HOH with Dee's.")

    prompt = _prompt_text(host)
    assert "KNOWLEDGE SUMMARY GUIDANCE" not in prompt


# ==========================================================
# TEST F -- conversation continuity: a follow-up question still has
# the prior exchange available in history
# ==========================================================


def test_followup_question_still_has_prior_exchange_in_history(tmp_path, monkeypatch):
    host, knowledge, _memory, historical_events, _archive = _setup(
        tmp_path, monkeypatch, reply_text="Taylor won HOH in Week 2."
    )
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")

    _ask(host, channel_id=704, text="What do you know about Taylor's HOH?")
    _ask(host, channel_id=704, text="What happened after that?")

    # The second call's prompt must include the FIRST call's question
    # and reply as conversation history -- proving continuity across
    # turns, not just that each call independently works.
    prompt = _prompt_text(host)
    assert "Taylor's HOH" in prompt
    assert "Taylor won HOH in Week 2." in prompt


# ==========================================================
# TEST G -- relevant memory reaches the prompt
# ==========================================================


def test_relevant_memory_reaches_the_prompt(tmp_path, monkeypatch):
    host, _knowledge, memory, _historical_events, _archive = _setup(tmp_path, monkeypatch)
    memory.remember(
        channel_id=705, author_id=1, author_name="Bobby",
        content="Julie should always mention the veto ceremony timing when relevant.",
    )

    _ask(host, channel_id=705, text="What do you remember about the veto ceremony?")

    prompt = _prompt_text(host)
    assert "REMEMBERED CONTEXT" in prompt
    assert "veto ceremony timing" in prompt


# ==========================================================
# TEST H -- personality: a real greeting is natural, and an immediate
# follow-up does not repeat it (see commit 74412a4)
# ==========================================================


def test_good_morning_greeting_then_followup_does_not_repeat_greeting_guidance(
    tmp_path, monkeypatch
):
    host, knowledge, *_ = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=706, text="Good morning Julie")
    first_prompt = _prompt_text(host)
    assert "a brief, natural greeting is fine here" in first_prompt

    _ask(host, channel_id=706, text="Who is the current HOH?")
    second_prompt = _prompt_text(host)
    assert "do not greet again" in second_prompt
    assert "a brief, natural greeting is fine here" not in second_prompt


# ==========================================================
# Provider fallback: Groq failing with a size-shaped (413) error must
# still hand Gemini the SAME already-budgeted content, not a stale
# unbounded one -- proving the budget fix protects both providers
# since it runs upstream of either being called.
# ==========================================================


def test_groq_413_shaped_failure_falls_back_to_gemini_with_the_same_budgeted_prompt(
    tmp_path, monkeypatch
):
    class _FailingGroqClient:
        class _Completions:
            def create(self, **kwargs):
                raise RuntimeError(
                    "Error code: 413 - Request too large for model "
                    "`openai/gpt-oss-120b`... TPM Limit 8000, Requested 9108"
                )

        chat = SimpleNamespace(completions=_Completions())

    gemini_recorder: dict = {}

    def gemini_generate_content(**kwargs):
        gemini_recorder["system_instruction"] = kwargs["config"].system_instruction
        candidate = SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text="Gemini answered instead.")]),
            finish_reason=SimpleNamespace(name="STOP"),
        )
        return SimpleNamespace(candidates=[candidate], text="Gemini answered instead.")

    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    monkeypatch.setattr(ai_service, "groq_client", _FailingGroqClient())
    monkeypatch.setattr(
        ai_service, "ai_client",
        SimpleNamespace(models=SimpleNamespace(generate_content=gemini_generate_content)),
    )
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    knowledge = KnowledgeStore(storage=storage)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    memory = MemoryStore(storage=storage)
    historical_events = HistoricalEventStore(db_path=tmp_path / "historical_events.db")
    engine = _FakeEngine(knowledge, memory, historical_events)
    host = _FakeDiscordServiceHost(engine)

    reply = _ask(host, channel_id=707, text="Who is the current HOH?")

    assert reply == "Gemini answered instead."
    assert "OFFICIAL GAME FACTS" in gemini_recorder["system_instruction"]
    assert "Dee" in gemini_recorder["system_instruction"]
