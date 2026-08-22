"""Integration proof for the KNOWLEDGE_SUMMARY feature (production/
knowledge_summary.py + services/ai_service.py
format_knowledge_summary_guidance()), exercised through the real
shared entry point both /chat and @mention/DM replies use
(services/discord.py DiscordService.generate_ai_reply()) -- same "bind
the real unbound method onto a lightweight stand-in" pattern already
established in tests/test_historical_hoh_boundary.py and
tests/test_conversational_facts_boundary.py.

Covers the trust guarantees this feature exists to protect: a broad
"tell me everything you know" question gets a capability-overview
instruction appended to the real prompt (and only that question shape
-- never an ordinary factual one), the guidance never leaks a secret
or config value, and the feature never mutates KnowledgeStore,
HouseStatus, or CompetitionState, nor introduces a cross-channel
memory dump.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import services.ai_service as ai_service
from database.historical_events import HistoricalEventStore
from production.competition import CompetitionState
from production.house_status import HouseStatus
from production.knowledge import KnowledgeType
from production.memory import MemoryStore
from services.discord import DiscordService
from services.logger import ProductionLogger


class _KnowledgeSpy:
    """Wraps a real KnowledgeStore, raising immediately if .teach() is
    ever called -- see tests/test_conversational_facts_boundary.py for
    the original of this pattern. A broad knowledge-summary question
    calling this, for any reason, is exactly the bug this file exists
    to catch."""

    def __init__(self, real) -> None:
        self._real = real
        self.teach_calls: list[tuple] = []

    def teach(self, *args, **kwargs):
        self.teach_calls.append((args, kwargs))
        raise AssertionError(
            "A knowledge-summary question must never call "
            "KnowledgeStore.teach() -- it only ever describes existing "
            "knowledge, never writes any."
        )

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakeWatcher:
    def __init__(self) -> None:
        self.house_status = type("H", (), {"current": HouseStatus()})()
        self.competition = type("C", (), {"current": CompetitionState()})()
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


class FakeGroqMessage:
    def __init__(self, content):
        self.content = content


class FakeGroqChoice:
    def __init__(self, content):
        self.message = FakeGroqMessage(content)


class FakeGroqResponse:
    def __init__(self, content):
        self.choices = [FakeGroqChoice(content)]


class _CapturingGroqClient:
    def __init__(self, reply_text: str) -> None:
        self._reply_text = reply_text
        self.calls: list[dict] = []

    class _Completions:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.calls.append(kwargs)
            return FakeGroqResponse(self.outer._reply_text)

    @property
    def chat(self):
        outer = self

        class _Chat:
            completions = _CapturingGroqClient._Completions(outer)

        return _Chat()


def _setup(tmp_path: Path, monkeypatch, reply_text: str):
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    monkeypatch.setattr(ai_service, "groq_client", _CapturingGroqClient(reply_text))
    monkeypatch.setattr(ai_service, "ai_client", None)

    from database.storage import Storage
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    from production.knowledge import KnowledgeStore
    real_knowledge = KnowledgeStore(storage=storage)
    spy = _KnowledgeSpy(real_knowledge)
    memory = MemoryStore(storage=storage)
    historical_events = HistoricalEventStore(db_path=tmp_path / "historical_events.db")
    engine = _FakeEngine(spy, memory, historical_events)
    host = _FakeDiscordServiceHost(engine)
    return host, spy, real_knowledge, memory, historical_events


def _ask(host, channel_id, text, user_id=1, author_name="Alex", is_moderator=False):
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


# ==========================================================
# The broad question actually reaches the guidance block; an
# ordinary one does not.
# ==========================================================


def test_broad_knowledge_question_gets_the_summary_guidance_block(tmp_path, monkeypatch):
    host, *_ = _setup(tmp_path, monkeypatch, reply_text="Here's what I know about...")

    _ask(host, channel_id=200, text="Tell me everything you know.")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "KNOWLEDGE SUMMARY GUIDANCE" in prompt


def test_ordinary_current_state_question_does_not_get_the_guidance_block(
    tmp_path, monkeypatch
):
    host, _, real_knowledge, *_ = _setup(
        tmp_path, monkeypatch, reply_text="Yash is the current HOH."
    )
    real_knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    _ask(host, channel_id=201, text="Who is the current HoH?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "KNOWLEDGE SUMMARY GUIDANCE" not in prompt


def test_topic_scoped_everything_question_does_not_get_the_guidance_block(
    tmp_path, monkeypatch
):
    """"Tell me everything you know about X" is a scoped question, not
    a capability overview -- see production/knowledge_summary.py."""

    host, *_ = _setup(tmp_path, monkeypatch, reply_text="Here's what happened...")

    _ask(host, channel_id=202, text="Tell me everything you know about Taylor's HOH")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "KNOWLEDGE SUMMARY GUIDANCE" not in prompt


# ==========================================================
# The broad question still leaves existing fact blocks/trust order
# intact, never mutates state, and never leaks a secret.
# ==========================================================


def test_broad_knowledge_question_still_surfaces_official_state(tmp_path, monkeypatch):
    host, _, real_knowledge, *_ = _setup(
        tmp_path, monkeypatch, reply_text="Here's what I know..."
    )
    real_knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    _ask(host, channel_id=203, text="Tell me everything you know.")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "OFFICIAL GAME FACTS" in prompt
    assert "Yash" in prompt
    # The guidance is a how-to-answer instruction, not a fact -- it
    # must come after the real fact blocks, never before one.
    assert prompt.index("OFFICIAL GAME FACTS") < prompt.index("KNOWLEDGE SUMMARY GUIDANCE")


def test_broad_knowledge_question_never_mutates_knowledge_store(tmp_path, monkeypatch):
    host, _, real_knowledge, *_ = _setup(
        tmp_path, monkeypatch, reply_text="Here's what I know..."
    )

    _ask(host, channel_id=204, text="Tell me everything you know.")

    assert real_knowledge.active_items() == []


def test_broad_knowledge_question_never_leaks_configured_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "super-secret-admin-key")
    monkeypatch.setenv("DISCORD_TOKEN", "super-secret-discord-token")
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-gemini-key")

    host, *_ = _setup(tmp_path, monkeypatch, reply_text="Here's what I know...")

    _ask(host, channel_id=205, text="Tell me everything you know.")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "super-secret-admin-key" not in prompt
    assert "super-secret-discord-token" not in prompt
    assert "super-secret-gemini-key" not in prompt


def test_broad_knowledge_question_does_not_widen_memory_recall_scope(tmp_path, monkeypatch):
    """A broad question must not become an excuse to dump MemoryStore
    across channels -- format_long_term_memory() must still only ever
    receive the normal channel-scoped recall() result, exactly as for
    any other question."""

    host, _, _, memory, _ = _setup(tmp_path, monkeypatch, reply_text="Here's what I know...")
    memory.remember(channel_id=999, author_id=1, author_name="Bobby", content="a private note")

    _ask(host, channel_id=206, text="Tell me everything you know.")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "a private note" not in prompt


# ==========================================================
# Moderator vs normal-user mode, end to end through the real
# generate_ai_reply() path (see production/authorization.py and
# commands/chat.py / services/discord.py's on_message handler, which
# are what actually compute is_moderator from a real discord.Member --
# this file exercises generate_ai_reply() with that bool already
# decided, matching how those two real callers invoke it).
# ==========================================================


def test_moderator_flag_produces_the_moderator_briefing(tmp_path, monkeypatch):
    host, *_ = _setup(tmp_path, monkeypatch, reply_text="Here's the briefing...")

    _ask(host, channel_id=207, text="Tell me everything you know.", is_moderator=True)

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "MODERATOR BRIEFING" in prompt


def test_non_moderator_never_gets_the_moderator_briefing(tmp_path, monkeypatch):
    host, *_ = _setup(tmp_path, monkeypatch, reply_text="Here's what I know...")

    _ask(host, channel_id=208, text="Tell me everything you know.", is_moderator=False)

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "MODERATOR BRIEFING" not in prompt


def test_moderator_flag_is_ignored_for_an_ordinary_question(tmp_path, monkeypatch):
    """is_moderator must never widen what an ORDINARY question
    surfaces -- it only ever changes the KNOWLEDGE_SUMMARY guidance,
    which itself only appears for a genuine broad question."""

    host, _, real_knowledge, *_ = _setup(
        tmp_path, monkeypatch, reply_text="Yash is the current HOH."
    )
    real_knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    _ask(host, channel_id=209, text="Who is the current HoH?", is_moderator=True)

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "KNOWLEDGE SUMMARY GUIDANCE" not in prompt
    assert "MODERATOR BRIEFING" not in prompt


# ==========================================================
# Full read-only guarantee: HouseStatus and CompetitionState (not
# just KnowledgeStore) are never mutated by a broad question either.
# ==========================================================


def test_broad_knowledge_question_never_mutates_house_status_or_competition(
    tmp_path, monkeypatch
):
    host, _, _, _, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="Here's what I know..."
    )
    # Deep copies, not the same reference -- comparing the live object
    # to itself would trivially "pass" even if it had been mutated.
    house_status_before = copy.deepcopy(host.scheduler.engine.watcher.house_status.current)
    competition_before = copy.deepcopy(host.scheduler.engine.watcher.competition.current)

    _ask(host, channel_id=210, text="Tell me everything you know.", is_moderator=True)

    assert host.scheduler.engine.watcher.house_status.current == house_status_before
    assert host.scheduler.engine.watcher.competition.current == competition_before


def test_broad_knowledge_question_never_writes_a_historical_event(tmp_path, monkeypatch):
    host, _, _, _, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="Here's what I know..."
    )

    _ask(host, channel_id=211, text="Tell me everything you know.")

    assert historical_events.known_hoh_winners() == []


# ==========================================================
# The summary is honest about Phase 1's HOH-only scope and about
# whether verified historical data actually exists yet.
# ==========================================================


def test_summary_admits_no_verified_historical_hoh_when_none_recorded(tmp_path, monkeypatch):
    host, *_ = _setup(tmp_path, monkeypatch, reply_text="Here's what I know...")

    _ask(host, channel_id=212, text="Tell me everything you know.")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "no verified historical hoh record has been entered yet" in prompt.lower()


def test_summary_reports_a_real_verified_historical_hoh_record(tmp_path, monkeypatch):
    host, _, _, _, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="Here's what I know..."
    )
    claim = historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=1, week_number=1, winner="Taylor",
        source_type="manual_admin_note", source_ref="x",
    )
    historical_events.verify_hoh(claim.id)

    _ask(host, channel_id=213, text="Tell me everything you know.")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "1 known winner" in prompt
    assert "phase 1" in prompt.lower()
    assert "do not have structured records for" in prompt.lower()
