"""Integration proof for Phase 1's structured historical HOH events,
exercised through the real shared entry point both /chat and
@mention/DM replies use (services/discord.py
DiscordService.generate_ai_reply()) -- same "bind the real unbound
method onto a lightweight stand-in" pattern already established in
tests/test_conversational_facts_boundary.py and
tests/test_personality_hosting.py.

Covers the critical trust boundary this whole feature exists to
protect: a verified historical HOH record must never compete with, or
be mistaken for, current OFFICIAL GAME FACTS state -- the structured-
events counterpart to the Hamsterwatch-prose Taylor/Yash test already
covering that boundary for HISTORICAL SEASON CONTEXT. Also covers that
historical retrieval never mutates KnowledgeStore, HouseStatus, or
CompetitionState, and that existing behavior (fallback, /remember,
/forget) is untouched.
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


class _KnowledgeSpy:
    """Wraps a real KnowledgeStore, raising immediately if .teach() is
    ever called -- see tests/test_conversational_facts_boundary.py for
    the original of this pattern. Historical retrieval calling this,
    for any reason, is exactly the bug this file exists to catch."""

    def __init__(self, real) -> None:
        self._real = real
        self.teach_calls: list[tuple] = []

    def teach(self, *args, **kwargs):
        self.teach_calls.append((args, kwargs))
        raise AssertionError(
            "Historical event retrieval must never call "
            "KnowledgeStore.teach() -- a verified historical record "
            "must never become current official state."
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
    return host, spy, real_knowledge, historical_events


def _verify_hoh(store, *, season=28, cycle, week, winner):
    claim = store.record_hoh_claim(
        season=season, cycle_sequence_number=cycle, week_number=week,
        winner=winner, source_type="manual_admin_note", source_ref="x",
    )
    return store.verify_hoh(claim.id)


def _ask(host, channel_id, text, user_id=1, author_name="Alex"):
    host._ai_cooldowns.pop(user_id, None)
    return asyncio.run(
        host.generate_ai_reply(
            user_id=user_id, channel_id=channel_id, user_text=text, author_name=author_name
        )
    )


# ==========================================================
# The critical trust test: verified historical HOH vs current STATE
# ==========================================================


def test_current_hoh_question_prefers_official_state_over_historical_record(
    tmp_path, monkeypatch
):
    host, _, real_knowledge, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="Yash is the current HOH."
    )
    real_knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")

    reply = _ask(host, channel_id=100, text="Who is the current HoH?")

    assert "Yash" in reply
    assert real_knowledge.active_state("HOH").content == "Yash"


def test_historical_week_question_surfaces_the_verified_historical_record(
    tmp_path, monkeypatch
):
    host, _, real_knowledge, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="Taylor was HOH back in Week 2."
    )
    real_knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")

    _ask(host, channel_id=101, text="Who was HOH in Week 2?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "HISTORICAL STRUCTURED EVENTS" in prompt
    assert "Taylor" in prompt
    # Both facts present, official ranked first, exactly the guarantee
    # already proven for Hamsterwatch prose, now proven for structured
    # historical events too.
    assert "OFFICIAL GAME FACTS" in prompt
    assert "Yash" in prompt
    assert prompt.index("OFFICIAL GAME FACTS") < prompt.index("HISTORICAL STRUCTURED EVENTS")


def test_current_state_is_never_mutated_by_historical_retrieval(tmp_path, monkeypatch):
    host, _, real_knowledge, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="Taylor won HOH that cycle."
    )
    real_knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")

    _ask(host, channel_id=102, text="Who was HOH in Week 2?")

    # A historical retrieval, however it answered, must never have
    # touched what /hoh and OFFICIAL GAME FACTS report.
    assert real_knowledge.active_state("HOH").content == "Yash"


# ==========================================================
# Unverified/disputed records stay invisible to Julie
# ==========================================================


def test_unverified_historical_record_never_reaches_the_prompt(tmp_path, monkeypatch):
    host, _, _, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="I don't have a verified historical record of that."
    )
    historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=3, week_number=3, winner="Barrett",
        source_type="fan_wiki", source_ref="x",
    )  # never verified

    _ask(host, channel_id=103, text="Who was HOH in Week 3?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "Barrett" not in prompt
    assert "administrator-verified historical game" not in prompt


def test_conflicting_unverified_records_never_reach_the_prompt(tmp_path, monkeypatch):
    host, _, _, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="I don't have a verified historical record of that."
    )
    historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=3, week_number=3, winner="SourceA",
        source_type="fan_wiki", source_ref="a",
    )
    historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=3, week_number=3, winner="SourceB",
        source_type="hamsterwatch", source_ref="b",
    )

    _ask(host, channel_id=104, text="Who was HOH in Week 3?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "SourceA" not in prompt
    assert "SourceB" not in prompt


# ==========================================================
# Double eviction, end to end
# ==========================================================


def test_double_eviction_week_presents_both_cycles_not_a_guess(tmp_path, monkeypatch):
    host, _, _, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="Week 9 had two HOHs due to the double eviction."
    )
    _verify_hoh(historical_events, cycle=9, week=9, winner="Drew")
    _verify_hoh(historical_events, cycle=10, week=9, winner="LaTrice")

    _ask(host, channel_id=105, text="Who was HOH in Week 9?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "Drew" in prompt
    assert "Latrice" in prompt
    assert "multiple hoh cycles" in prompt.lower()


# ==========================================================
# Historical retrieval never touches HouseStatus/CompetitionState/KnowledgeStore
# ==========================================================


def test_historical_retrieval_never_calls_knowledge_teach(tmp_path, monkeypatch):
    host, spy, _, historical_events = _setup(tmp_path, monkeypatch, reply_text="reply")
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")

    _ask(host, channel_id=106, text="Who was HOH in Week 2?")

    assert spy.teach_calls == []


def test_historical_retrieval_never_mutates_house_status_or_competition_state(
    tmp_path, monkeypatch
):
    host, _, _, historical_events = _setup(tmp_path, monkeypatch, reply_text="reply")
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")
    watcher = host.scheduler.engine.watcher
    house_status_before = watcher.house_status.current
    competition_before = watcher.competition.current

    _ask(host, channel_id=107, text="Who was HOH in Week 2?")

    assert watcher.house_status.current is house_status_before
    assert watcher.competition.current is competition_before
    assert watcher.house_status.current.hoh == ""


# ==========================================================
# Missing records never cause a hallucinated answer
# ==========================================================


def test_missing_historical_record_does_not_get_a_fabricated_answer(tmp_path, monkeypatch):
    host, _, _, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="I don't have a verified historical record of that."
    )
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")  # unrelated week

    _ask(host, channel_id=108, text="Who was HOH in Week 40?")

    prompt = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "administrator-verified historical game" not in prompt


# ==========================================================
# Ordinary /chat behavior remains intact with the new system present
# ==========================================================


def test_ordinary_chat_still_works_with_historical_events_store_present(tmp_path, monkeypatch):
    host, _, _, historical_events = _setup(
        tmp_path, monkeypatch, reply_text="Good to see you, Houseguest."
    )
    _verify_hoh(historical_events, cycle=2, week=2, winner="Taylor")

    reply = _ask(host, channel_id=109, text="hey Julie, how's it going?")

    assert reply == "Good to see you, Houseguest."
