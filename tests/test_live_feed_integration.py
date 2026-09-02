"""Integration tests for recent-live-feed conversational retrieval,
exercising the REAL generation path (DiscordService.generate_ai_reply()
-> services.ai_service.generate_julie_response()), not just isolated
helpers. Mirrors tests/test_reaction_guidance_integration.py's
fixtures and philosophy. See tests/test_live_feed_window.py for
production/live_feed_window.py's own unit tests.
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
from services.discord import DiscordService
from services.logger import ProductionLogger


# ==========================================================
# Fixtures -- same shape as test_reaction_guidance_integration.py's,
# extended with a configurable recent_updates() on the fake engine.
# ==========================================================


class _FakeWatcher:
    def __init__(self) -> None:
        self.house_status = SimpleNamespace(current=HouseStatus(hoh="Dee"))
        self.competition = SimpleNamespace(current=CompetitionState())
        self.hamsterwatch = None


class _FakeEngine:
    def __init__(self, knowledge, memory, historical_events, recent_updates_result=None) -> None:
        self.watcher = _FakeWatcher()
        self.knowledge = knowledge
        self.memory = memory
        self.historical_events = historical_events
        self._recent_updates_result = recent_updates_result or []
        self.recent_updates_calls: list[float | None] = []

    def recent_updates(self, hours: float | None = None) -> list[str]:
        self.recent_updates_calls.append(hours)
        return self._recent_updates_result


class _RaisingEngine(_FakeEngine):
    def recent_updates(self, hours: float | None = None) -> list[str]:
        raise RuntimeError("simulated recent_updates() failure")


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


def _setup(tmp_path: Path, monkeypatch, *, recent_updates_result=None, engine_cls=_FakeEngine,
           reply_text: str = "Dee is the current HOH."):
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
    engine = engine_cls(knowledge, memory, historical_events, recent_updates_result)
    host = _FakeDiscordServiceHost(engine)
    return host, knowledge, engine


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
    return ai_service.groq_client.calls[-1]["messages"][0]["content"]


# ==========================================================
# Ordinary messages -- must not pay any recent-feed cost at all.
# ==========================================================


def test_ordinary_question_never_calls_recent_updates(tmp_path, monkeypatch):
    host, knowledge, engine = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=1, text="Who is the current HOH?")

    assert engine.recent_updates_calls == []
    assert "RECENT LIVE-FEED ACTIVITY" not in _prompt(host)


# ==========================================================
# Retrieval -- generic, player-specific, empty window.
# ==========================================================


def test_generic_recent_feed_request_is_retrieved_and_rendered(tmp_path, monkeypatch):
    host, knowledge, engine = _setup(
        tmp_path, monkeypatch, recent_updates_result=["Drew nominated Devens."]
    )
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=2, text="What happened in the last 5 hours?")

    assert engine.recent_updates_calls == [5.0]
    prompt = _prompt(host)
    assert "RECENT LIVE-FEED ACTIVITY" in prompt
    assert "last 5 hours" in prompt
    assert "Drew nominated Devens." in prompt


def test_player_specific_request_narrows_and_labels_the_block(tmp_path, monkeypatch):
    host, knowledge, engine = _setup(
        tmp_path,
        monkeypatch,
        recent_updates_result=["Drew nominated Devens.", "LaLa did laundry."],
    )
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=3, text="What has Devens been up to recently?")

    prompt = _prompt(host)
    assert "focused on mentions of devens" in prompt
    assert "Drew nominated Devens." in prompt
    assert "LaLa did laundry." not in prompt


def test_empty_recent_window_gets_an_honest_not_a_confident_negative(tmp_path, monkeypatch):
    host, knowledge, engine = _setup(tmp_path, monkeypatch, recent_updates_result=[])
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=4, text="What happened in the last hour?")

    prompt = _prompt(host)
    assert "RECENT LIVE-FEED ACTIVITY" in prompt
    assert "nothing was captured here" in prompt.lower()
    assert "never state with confidence that literally nothing happened" in prompt.lower()


def test_recent_updates_failure_degrades_gracefully(tmp_path, monkeypatch):
    host, knowledge, engine = _setup(tmp_path, monkeypatch, engine_cls=_RaisingEngine)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    # Must not raise -- generate_ai_reply() degrades to no recent-feed
    # block, same posture as the Hamsterwatch/historical-events blocks.
    reply = _ask(host, channel_id=5, text="What happened in the last hour?")

    assert reply
    assert "RECENT LIVE-FEED ACTIVITY" not in _prompt(host)


# ==========================================================
# Coexistence with other retrieval layers -- must stay logically
# distinct, official state always authoritative.
# ==========================================================


def test_recent_feed_coexists_with_official_state_correct_order(tmp_path, monkeypatch):
    host, knowledge, engine = _setup(
        tmp_path, monkeypatch, recent_updates_result=["Drew nominated Devens."]
    )
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=6, text="What happened recently, and who is currently HOH?")

    prompt = _prompt(host)
    assert "OFFICIAL GAME FACTS" in prompt
    assert "RECENT LIVE-FEED ACTIVITY" in prompt
    # Official state must appear before the recent-feed block -- trust
    # hierarchy is reflected in assembly order (see
    # generate_julie_response()'s own docstring).
    assert prompt.index("OFFICIAL GAME FACTS") < prompt.index("RECENT LIVE-FEED ACTIVITY")


def test_recent_feed_block_explicitly_defers_to_official_state(tmp_path, monkeypatch):
    """Authority test: the rendered block itself must say official
    state wins on a conflict -- not just rely on assembly order."""

    host, knowledge, engine = _setup(
        tmp_path, monkeypatch, recent_updates_result=["Someone claimed a new HOH."]
    )
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=7, text="What happened in the last hour?")

    prompt = _prompt(host)
    start = prompt.index("RECENT LIVE-FEED ACTIVITY")
    block = prompt[start:start + 800]
    assert "official game facts is correct" in block.lower()
    assert "never use this alone to answer who currently holds hoh" in block.lower()


def test_recent_feed_coexists_with_historical_events(tmp_path, monkeypatch):
    host, knowledge, engine = _setup(
        tmp_path, monkeypatch, recent_updates_result=["Drew nominated Devens."]
    )
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    claim = engine.historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=1, week_number=1, winner="Taylor",
        source_type="manual_admin_note", source_ref="test",
    )
    engine.historical_events.verify_hoh(claim.id)

    _ask(host, channel_id=8, text="What happened recently, and what happened during Taylor's HOH?")

    prompt = _prompt(host)
    assert "RECENT LIVE-FEED ACTIVITY" in prompt
    assert "HISTORICAL STRUCTURED EVENTS" in prompt


def test_recent_feed_coexists_with_conversation_history(tmp_path, monkeypatch):
    """Conversational momentum: a follow-up in the same channel should
    reuse the existing conversation-history mechanism -- no separate
    memory system is introduced by this feature."""

    host, knowledge, engine = _setup(
        tmp_path,
        monkeypatch,
        recent_updates_result=["Drew nominated Devens.", "Devens talked strategy with LaLa."],
    )
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=9, text="What happened in the last 5 hours?")
    _ask(host, channel_id=9, text="What about Devens?")

    # Second call's prompt includes the first turn as real history.
    second_prompt = ai_service.groq_client.calls[-1]["messages"]
    joined = " ".join(m["content"] for m in second_prompt)
    assert "What happened in the last 5 hours?" in joined


# ==========================================================
# Reaction engine coexistence -- both features together, correct
# ordering, R1 non-regression.
# ==========================================================


def test_recent_feed_and_situational_reaction_coexist_in_order(tmp_path, monkeypatch):
    host, knowledge, engine = _setup(
        tmp_path, monkeypatch, recent_updates_result=["A total blindside just happened."]
    )
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=10, text="What happened in the last hour?? That's a blindside!!")

    prompt = _prompt(host)
    feed_idx = prompt.index("RECENT LIVE-FEED ACTIVITY")
    hosting_idx = prompt.index("HOSTING GUIDANCE FOR THIS REPLY")
    reaction_idx = prompt.index("SITUATIONAL REACTION")
    assert feed_idx < hosting_idx < reaction_idx


def test_r1_sentence_ceiling_still_fixed_with_recent_feed_present(tmp_path, monkeypatch):
    host, knowledge, engine = _setup(
        tmp_path, monkeypatch, recent_updates_result=["Drew nominated Devens."]
    )
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=11, text="What happened in the last hour?")

    prompt = _prompt(host)
    start = prompt.index("HOSTING GUIDANCE FOR THIS REPLY")
    end = prompt.index("SITUATIONAL REACTION")
    hosting_block = prompt[start:end].lower()
    assert "1-3 sentences" not in hosting_block


# ==========================================================
# Context-budget pressure -- the non-negotiable requirement.
# ==========================================================


def test_large_recent_window_plus_current_state_stays_within_budget(tmp_path, monkeypatch):
    from production.context_budget import GROQ_TPM_LIMIT, RESERVED_OUTPUT_TOKENS, estimate_tokens

    many_updates = [f"Update number {i} about a lengthy strategy conversation." for i in range(60)]
    host, knowledge, engine = _setup(tmp_path, monkeypatch, recent_updates_result=many_updates)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    knowledge.teach(KnowledgeType.STATE, "Drew, LaLa, Taylor", author_id=1, topic="Nominees")

    _ask(host, channel_id=12, text="What happened in the last 24 hours?")

    prompt = _prompt(host)
    total = estimate_tokens(prompt)
    assert total <= GROQ_TPM_LIMIT - RESERVED_OUTPUT_TOKENS


def test_large_recent_window_plus_huge_knowledge_and_historical_stays_within_budget(
    tmp_path, monkeypatch
):
    """The scenario this feature's own spec calls out explicitly:
    large live-feed window + current state + historical retrieval
    together must not reproduce the original Groq 413."""

    from production.context_budget import GROQ_TPM_LIMIT, RESERVED_OUTPUT_TOKENS, estimate_tokens

    many_updates = [f"Update {i}: " + ("strategy talk " * 20) for i in range(40)]
    host, knowledge, engine = _setup(tmp_path, monkeypatch, recent_updates_result=many_updates)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    for i in range(20):
        knowledge.teach(
            KnowledgeType.FACT, f"Some very long taught fact number {i} " * 30, author_id=1
        )
    claim = engine.historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=1, week_number=1, winner="Taylor",
        source_type="manual_admin_note", source_ref="test",
    )
    engine.historical_events.verify_hoh(claim.id)

    _ask(
        host, channel_id=13,
        text="What happened in the last 24 hours, and what happened during Taylor's HOH?",
    )

    prompt = _prompt(host)
    total = estimate_tokens(prompt)
    safe_ceiling = GROQ_TPM_LIMIT - RESERVED_OUTPUT_TOKENS
    assert total <= safe_ceiling, f"prompt estimate {total} exceeds safe ceiling {safe_ceiling}"


# ==========================================================
# Provider fallback -- Groq success, Groq failure -> Gemini receives
# the same bounded recent-feed context.
# ==========================================================


def test_gemini_fallback_receives_the_same_recent_live_feed_block(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    monkeypatch.setattr(ai_service, "groq_client", object())

    def _failing_groq(*args, **kwargs):
        return None

    monkeypatch.setattr(ai_service, "_try_groq_chat", _failing_groq)

    captured = {}

    def _fake_gemini(contents, system_instruction, max_tokens, temperature):
        captured["system_instruction"] = system_instruction
        return "Not much I can verify from the last hour."

    monkeypatch.setattr(ai_service, "ai_client", object())
    monkeypatch.setattr(ai_service, "_try_gemini_chat", _fake_gemini)

    from database.storage import Storage
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    from production.knowledge import KnowledgeStore
    knowledge = KnowledgeStore(storage=storage)
    memory = MemoryStore(storage=storage)
    historical_events = HistoricalEventStore(db_path=tmp_path / "historical_events.db")
    engine = _FakeEngine(knowledge, memory, historical_events, ["Drew nominated Devens."])
    host = _FakeDiscordServiceHost(engine)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=14, text="What happened in the last hour?")

    assert "RECENT LIVE-FEED ACTIVITY" in captured["system_instruction"]
    assert "Drew nominated Devens." in captured["system_instruction"]
    # No duplicate retrieval during fallback -- recent_updates() was
    # only ever called once by generate_ai_reply(), regardless of
    # which provider ultimately answers.
    assert engine.recent_updates_calls == [1.0]
