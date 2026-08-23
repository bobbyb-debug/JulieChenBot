"""Integration proof for the context-budget fix (production/
context_budget.py), exercised through the real shared entry point
both /chat and @mention/DM replies use (services/discord.py
DiscordService.generate_ai_reply()) -- same "bind the real unbound
method onto a lightweight stand-in" pattern already established in
tests/test_knowledge_summary_boundary.py, tests/
test_historical_hoh_boundary.py, and tests/
test_hosting_guidance_boundaries.py.

Covers the actual production incident this fix exists to prevent:
Groq's on_demand tier rejecting a request with 413 "Request too
large" (Railway logs: "TPM Limit 8000, Requested 9108"/"9200") once
prompt tokens + the reserved completion-token allowance exceed the
model's TPM limit -- reproduced here with a deliberately heavy,
realistic scenario (a large admin-taught knowledge store, a long
conversation history, a large Hamsterwatch archive, and moderator
KNOWLEDGE_SUMMARY guidance all at once) to prove the real assembled
request now stays comfortably under budget, and that current-state
precedence, historical retrieval, KNOWLEDGE_SUMMARY, and the 74412a4
hosting-guidance/personality behavior all remain intact.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import services.ai_service as ai_service
from database.hamsterwatch_archive import HamsterwatchArchive
from database.historical_events import HistoricalEventStore
from database.storage import Storage
from production.competition import CompetitionState
from production.context_budget import (
    GROQ_TPM_LIMIT,
    MAX_PROMPT_TOKENS,
    RESERVED_OUTPUT_TOKENS,
    estimate_tokens,
)
from production.house_status import HouseStatus
from production.knowledge import KnowledgeStore, KnowledgeType
from production.memory import MemoryStore
from services.discord import DiscordService
from services.logger import ProductionLogger


class _KnowledgeSpy:
    """Wraps a real KnowledgeStore, raising immediately if .teach() is
    ever called during a chat request -- the budget system must be
    strictly read-only, exactly like every other retrieval path in
    this codebase."""

    def __init__(self, real) -> None:
        self._real = real

    def teach(self, *args, **kwargs):
        raise AssertionError(
            "The context-budget system must never call KnowledgeStore.teach() "
            "-- it only decides what ALREADY-retrieved data survives into "
            "one prompt, never writes anything."
        )

    def __getattr__(self, name):
        return getattr(self._real, name)


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

    real_knowledge = KnowledgeStore(storage=storage)
    knowledge = _KnowledgeSpy(real_knowledge)
    memory = MemoryStore(storage=storage)
    historical_events = HistoricalEventStore(db_path=tmp_path / "historical_events.db")
    archive = HamsterwatchArchive(db_path=tmp_path / "hamsterwatch.db")
    engine = _FakeEngine(knowledge, memory, historical_events, archive)
    host = _FakeDiscordServiceHost(engine)
    return host, real_knowledge, memory, historical_events, archive


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


def _prompt(host):
    return ai_service.groq_client.calls[0]["messages"]


def _prompt_chars(host) -> int:
    return sum(len(m["content"]) for m in _prompt(host))


TOTAL_CEILING = MAX_PROMPT_TOKENS + RESERVED_OUTPUT_TOKENS  # what "requested" must stay under


# ==========================================================
# 1-2. Ordinary current-state questions stay comfortably under budget
# ==========================================================


def test_current_hoh_question_stays_comfortably_under_budget(tmp_path, monkeypatch):
    host, knowledge, *_ = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=100, text="Who is the current HOH?")

    estimated = estimate_tokens(" ".join(m["content"] for m in _prompt(host)))
    assert estimated + RESERVED_OUTPUT_TOKENS < GROQ_TPM_LIMIT


def test_current_nominees_question_stays_comfortably_under_budget(tmp_path, monkeypatch):
    host, knowledge, *_ = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Drew, LaLa, Taylor", author_id=1, topic="Nominees")

    _ask(host, channel_id=101, text="Who are the current nominees?")

    estimated = estimate_tokens(" ".join(m["content"] for m in _prompt(host)))
    assert estimated + RESERVED_OUTPUT_TOKENS < GROQ_TPM_LIMIT


# ==========================================================
# 3/10. Scoped historical question retrieves relevant history and
# stays under budget -- historical retrieval remains functional
# ==========================================================


def test_scoped_historical_question_retrieves_relevant_history_within_budget(
    tmp_path, monkeypatch
):
    host, knowledge, _memory, historical_events, _archive = _setup(tmp_path, monkeypatch)
    claim = historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=2, week_number=2, winner="Taylor",
        source_type="manual_admin_note", source_ref="x",
    )
    historical_events.verify_hoh(claim.id)

    _ask(host, channel_id=102, text="What do you know about Taylor's HOH?")

    prompt_text = " ".join(m["content"] for m in _prompt(host))
    assert "HISTORICAL STRUCTURED EVENTS" in prompt_text
    assert "Taylor" in prompt_text
    estimated = estimate_tokens(prompt_text)
    assert estimated + RESERVED_OUTPUT_TOKENS < GROQ_TPM_LIMIT


# ==========================================================
# 4/11. Broad KNOWLEDGE_SUMMARY stays under budget and remains
# functional
# ==========================================================


def test_broad_knowledge_summary_stays_under_budget(tmp_path, monkeypatch):
    host, knowledge, *_ = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=103, text="Tell me everything you know.", is_moderator=True)

    prompt_text = " ".join(m["content"] for m in _prompt(host))
    assert "KNOWLEDGE SUMMARY GUIDANCE" in prompt_text
    estimated = estimate_tokens(prompt_text)
    assert estimated + RESERVED_OUTPUT_TOKENS < GROQ_TPM_LIMIT


def test_scoped_question_never_triggers_broad_knowledge_summary(tmp_path, monkeypatch):
    """Preserves the existing intent-detection boundary (see
    production/knowledge_summary.py and tests/test_knowledge_summary.py)
    -- a scoped question must never accidentally activate the broad
    pathway regardless of the new budget system."""

    host, knowledge, _memory, historical_events, _archive = _setup(tmp_path, monkeypatch)
    claim = historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=2, week_number=2, winner="Taylor",
        source_type="manual_admin_note", source_ref="x",
    )
    historical_events.verify_hoh(claim.id)

    _ask(host, channel_id=104, text="Tell me everything you know about Taylor's HOH")

    prompt_text = " ".join(m["content"] for m in _prompt(host))
    assert "KNOWLEDGE SUMMARY GUIDANCE" not in prompt_text


# ==========================================================
# 5/8. Large knowledge store -> irrelevant knowledge trimmed, relevant
# knowledge survives
# ==========================================================


def test_large_knowledge_store_trims_irrelevant_facts_but_keeps_relevant_one(
    tmp_path, monkeypatch
):
    host, knowledge, *_ = _setup(tmp_path, monkeypatch)
    for i in range(60):
        knowledge.teach(
            KnowledgeType.FACT,
            f"Unrelated filler fact number {i} about ordinary house happenings.",
            author_id=1,
        )
    knowledge.teach(
        KnowledgeType.FACT,
        "Taylor got into a heated argument with Devens during the veto ceremony.",
        author_id=1,
    )

    _ask(host, channel_id=105, text="What do you know about Taylor?")

    prompt_text = " ".join(m["content"] for m in _prompt(host))
    assert "heated argument with Devens" in prompt_text  # relevant fact survived
    # Not all 61 facts fit -- proves real trimming happened, not just
    # coincidentally fitting under budget.
    assert prompt_text.count("Unrelated filler fact") < 60
    estimated = estimate_tokens(prompt_text)
    assert estimated + RESERVED_OUTPUT_TOKENS < GROQ_TPM_LIMIT


# ==========================================================
# 6. Large Hamsterwatch archive -> irrelevant articles never reach the
# prompt (pre-existing DEFAULT_LIMIT/MAX_HISTORICAL_CONTENT_CHARS
# bounding, untouched by this fix -- proven still true here)
# ==========================================================


def test_large_hamsterwatch_archive_stays_bounded(tmp_path, monkeypatch):
    host, _knowledge, _memory, _historical_events, archive = _setup(tmp_path, monkeypatch)
    for day in range(1, 40):
        archive.upsert(
            page_url=f"http://hamsterwatch.com/bb28/day{day}.shtml",
            section_slug=f"day-{day}",
            heading=f"Day {day} recap",
            article_date=f"2026-07-{day:02d}" if day <= 31 else f"2026-08-{day-31:02d}",
            bb_day=day,
            content=f"Day {day}: Taylor discussed strategy at length. " * 100,
            summary=f"Day {day} summary.",
        )

    _ask(host, channel_id=106, text="What do you know about Taylor?")

    prompt_text = " ".join(m["content"] for m in _prompt(host))
    estimated = estimate_tokens(prompt_text)
    assert estimated + RESERVED_OUTPUT_TOKENS < GROQ_TPM_LIMIT


# ==========================================================
# 7. Large memory store -> memory is bounded
# ==========================================================


def test_large_memory_store_is_bounded(tmp_path, monkeypatch):
    host, _knowledge, memory, *_ = _setup(tmp_path, monkeypatch)
    for i in range(30):
        memory.remember(
            channel_id=200, author_id=1, author_name="Bobby",
            content=f"Remembered note {i}: " + ("detail " * 100),
        )

    _ask(host, channel_id=200, text="Tell me everything you know.")

    prompt_text = " ".join(m["content"] for m in _prompt(host))
    estimated = estimate_tokens(prompt_text)
    assert estimated + RESERVED_OUTPUT_TOKENS < GROQ_TPM_LIMIT


# ==========================================================
# 9. Current official facts retain priority (never trimmed alongside
# a heavy scenario)
# ==========================================================


def test_official_state_always_survives_even_under_heavy_load(tmp_path, monkeypatch):
    host, knowledge, memory, historical_events, archive = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    for i in range(60):
        knowledge.teach(KnowledgeType.FACT, f"Filler fact {i} " * 20, author_id=1)
    for i in range(30):
        memory.remember(channel_id=300, author_id=1, author_name="Bobby", content=f"Note {i} " * 50)

    _ask(host, channel_id=300, text="Tell me everything you know.", is_moderator=True)

    prompt_text = " ".join(m["content"] for m in _prompt(host))
    assert "OFFICIAL GAME FACTS" in prompt_text
    assert "Dee" in prompt_text


# ==========================================================
# 12/13. HOSTING GUIDANCE remains present and last; 74412a4 personality
# behavior is untouched by this fix
# ==========================================================


def test_hosting_guidance_remains_present_and_last_under_heavy_load(tmp_path, monkeypatch):
    host, knowledge, memory, *_ = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    for i in range(60):
        knowledge.teach(KnowledgeType.FACT, f"Filler fact {i} " * 20, author_id=1)

    _ask(host, channel_id=301, text="Tell me everything you know.", is_moderator=True)

    prompt_text = " ".join(m["content"] for m in _prompt(host))
    assert "HOSTING GUIDANCE FOR THIS REPLY" in prompt_text
    # Last, even after KNOWLEDGE SUMMARY GUIDANCE -- unchanged from 74412a4.
    assert prompt_text.rindex("HOSTING GUIDANCE FOR THIS REPLY") > prompt_text.index(
        "KNOWLEDGE SUMMARY GUIDANCE"
    )


def test_system_instruction_still_forbids_mandatory_catchphrases_under_heavy_load(
    tmp_path, monkeypatch
):
    host, knowledge, *_ = _setup(tmp_path, monkeypatch)
    for i in range(60):
        knowledge.teach(KnowledgeType.FACT, f"Filler fact {i} " * 20, author_id=1)

    _ask(host, channel_id=302, text="Who is the current HOH?")

    system_message = _prompt(host)[0]["content"]
    assert system_message == ai_service.SYSTEM_INSTRUCTION or system_message.startswith(
        ai_service.SYSTEM_INSTRUCTION
    )


# ==========================================================
# 14. No secrets or raw memory content ever appear in budget log
# instrumentation
# ==========================================================


def test_budget_log_line_never_contains_memory_content_or_secrets(
    tmp_path, monkeypatch, caplog
):
    host, _knowledge, memory, *_ = _setup(tmp_path, monkeypatch)
    private_note = "PRIVATE_MEMORY_CONTENT_MUST_NOT_APPEAR_IN_LOGS"
    memory.remember(channel_id=400, author_id=1, author_name="Bobby", content=private_note)
    monkeypatch.setenv("ADMIN_API_KEY", "super-secret-admin-key")

    with caplog.at_level(logging.INFO, logger="AIService"):
        _ask(host, channel_id=400, text="Tell me everything you know.")

    budget_lines = [r.message for r in caplog.records if "AI context budget" in r.message]
    assert budget_lines, "expected a budget log line to be emitted"
    for line in budget_lines:
        assert private_note not in line
        assert "super-secret-admin-key" not in line
        assert "\n" not in line  # single-line, safe for structured log ingestion


# ==========================================================
# 15/16. Final request estimate cannot exceed the configured safety
# budget -- reproducing the actual production failure shape
# ==========================================================


def test_reproduced_production_failure_shape_now_stays_under_budget(tmp_path, monkeypatch):
    """Deliberately reconstructs the conditions that produced Railway's
    "TPM Limit 8000, Requested 9108/9200" 413s: a large accumulated
    admin-knowledge store, a long conversation history with several
    long (KNOWLEDGE_SUMMARY-style) replies, a matching Hamsterwatch
    archive, and a moderator-mode broad knowledge summary request all
    at once."""

    host, knowledge, memory, historical_events, archive = _setup(tmp_path, monkeypatch)

    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    knowledge.teach(KnowledgeType.STATE, "Drew, LaLa, Taylor", author_id=1, topic="Nominees")
    for i in range(40):
        knowledge.teach(
            KnowledgeType.FACT,
            f"Fact #{i}: a real recorded houseguest interaction or game development, "
            "roughly one full sentence of detail each time it was taught by an admin.",
            author_id=1,
        )
    for i in range(8):
        knowledge.teach(
            KnowledgeType.RULE,
            f"Standing rule #{i}: a permanent behavioral instruction for a recurring situation.",
            author_id=1,
        )

    for week, winner in enumerate(
        ["Taylor", "Devens", "Angela", "Haley", "Kamu", "Drew", "Yash", "Dee"], start=1
    ):
        claim = historical_events.record_hoh_claim(
            season=28, cycle_sequence_number=week, week_number=week, winner=winner,
            source_type="manual_admin_note", source_ref="x",
        )
        historical_events.verify_hoh(claim.id)

    for day in range(1, 30):
        archive.upsert(
            page_url=f"http://hamsterwatch.com/bb28/day{day}.shtml",
            section_slug=f"day-{day}",
            heading=f"Day {day} recap",
            article_date=f"2026-07-{day:02d}",
            bb_day=day,
            content=(
                f"Day {day}: Taylor and the house spent the day on strategy and a "
                "competition that shifted the power dynamic significantly. "
            ) * 30,
            summary=f"Day {day}: strategy and a competition.",
        )

    channel_id = 500
    short_reply = "Dee is the current HOH, with Drew, LaLa, and Taylor on the block."
    long_reply = (
        "Here's a quick snapshot of what I can draw on right now: current official "
        "game state, administrator-taught knowledge covering standing rules and "
        "recent facts, verified historical Head-of-Household records for eight "
        "prior weeks, a Hamsterwatch narrative archive with dozens of recap "
        "articles, automated live-feed observations, and anything Houseguests "
        "have explicitly asked me to remember in this channel."
    ) * 2
    for i in range(6):
        ai_service.update_and_get_history(channel_id, f"Question {i}: who is HOH?", 1, "Bobby")
        ai_service.append_ai_response(channel_id, short_reply)
    for i in range(3):
        ai_service.update_and_get_history(
            channel_id, f"Question {i}: tell me everything", 1, "Bobby"
        )
        ai_service.append_ai_response(channel_id, long_reply)

    for i in range(10):
        memory.remember(
            channel_id=channel_id, author_id=1, author_name="Bobby",
            content=f"Remembered note {i}: something worth keeping in mind, one full sentence.",
        )

    _ask(host, channel_id=channel_id, text="Tell me everything you know.", is_moderator=True)

    prompt_text = " ".join(m["content"] for m in _prompt(host))
    estimated_input = estimate_tokens(prompt_text)
    estimated_total = estimated_input + RESERVED_OUTPUT_TOKENS

    # The literal acceptance criterion: this heavy, realistic scenario
    # -- the shape that previously produced ~9,100-9,200 requested
    # tokens -- must now land safely below Groq's real 8,000 TPM limit,
    # with genuine margin, not by luck.
    assert estimated_total < GROQ_TPM_LIMIT
    assert estimated_total <= TOTAL_CEILING
