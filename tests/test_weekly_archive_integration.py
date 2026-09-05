"""Integration tests for weekly-archive conversational retrieval,
exercising the REAL generation path (DiscordService.generate_ai_reply()
-> services.ai_service.generate_julie_response()) -- mirrors
tests/test_live_feed_integration.py's fixtures and philosophy.

Covers the acceptance-criteria scenarios that have no other historical
retrieval mechanism today: HistoricalEventStore (database/
historical_events.py, production/historical_retrieval.py) is Phase 1,
HOH-only -- "who won the Week 8 veto?" and "who won the Week 8 BB
Blockbuster?" have nowhere else to be answered from. See production/
knowledge.py KnowledgeStore.close_week()/set_archived_week() and
services/ai_service.py format_weekly_archive().
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import services.ai_service as ai_service
from database.historical_events import HistoricalEventStore
from production.competition import CompetitionState
from production.house_status import HouseStatus
from production.knowledge import KnowledgeStore, KnowledgeType
from production.memory import MemoryStore
from services.discord import DiscordService
from services.logger import ProductionLogger


class _FakeWatcher:
    def __init__(self) -> None:
        self.house_status = SimpleNamespace(current=HouseStatus())
        self.competition = SimpleNamespace(current=CompetitionState())
        self.hamsterwatch = None


class _FakeEngine:
    def __init__(self, knowledge, memory, historical_events) -> None:
        self.watcher = _FakeWatcher()
        self.knowledge = knowledge
        self.memory = memory
        self.historical_events = historical_events

    def recent_updates(self, hours: float | None = None) -> list[str]:
        return []


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


def _setup(tmp_path: Path, monkeypatch, reply_text: str = "Yash won the Week 8 veto."):
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    monkeypatch.setattr(ai_service, "groq_client", _CapturingGroqClient(reply_text))
    monkeypatch.setattr(ai_service, "ai_client", None)

    from database.storage import Storage
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    knowledge = KnowledgeStore(storage=storage)
    memory = MemoryStore(storage=storage)
    historical_events = HistoricalEventStore(db_path=tmp_path / "historical_events.db")
    engine = _FakeEngine(knowledge, memory, historical_events)
    host = _FakeDiscordServiceHost(engine)
    return host, knowledge, historical_events


def _ask(host, channel_id, text, user_id=1):
    host._ai_cooldowns.pop(user_id, None)
    return asyncio.run(
        host.generate_ai_reply(
            user_id=user_id, channel_id=channel_id, user_text=text, author_name="Bobby",
        )
    )


def _prompt(host):
    return ai_service.groq_client.calls[-1]["messages"][0]["content"]


# ==========================================================
# A question naming a past, closed week retrieves its archived
# snapshot -- TEST 7/8 from the acceptance criteria.
# ==========================================================


def test_question_about_a_closed_past_week_retrieves_its_archive(tmp_path, monkeypatch):
    host, knowledge, historical_events = _setup(tmp_path, monkeypatch)
    knowledge.start_new_week(8)
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    knowledge.teach(KnowledgeType.STATE, "Devens", author_id=1, topic="BB_BLOCKBUSTER")
    knowledge.close_week()
    knowledge.start_new_week(9)
    knowledge.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    _ask(host, channel_id=1, text="Who won the Week 8 veto?")

    prompt = _prompt(host)
    assert "WEEKLY STATE ARCHIVE" in prompt
    assert "Week 8" in prompt
    assert "Yash" in prompt
    assert "Devens" in prompt


def test_question_about_the_current_week_does_not_add_an_archive_block(
    tmp_path, monkeypatch
):
    host, knowledge, historical_events = _setup(tmp_path, monkeypatch)
    knowledge.start_new_week(9)
    knowledge.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    _ask(host, channel_id=2, text="What's the state in Week 9?")

    assert "WEEKLY STATE ARCHIVE" not in _prompt(host)


def test_question_about_a_week_never_archived_adds_no_block(tmp_path, monkeypatch):
    host, knowledge, historical_events = _setup(tmp_path, monkeypatch)
    knowledge.start_new_week(9)

    _ask(host, channel_id=3, text="What happened in Week 3?")

    assert "WEEKLY STATE ARCHIVE" not in _prompt(host)


def test_ordinary_question_with_no_week_number_adds_no_archive_block(
    tmp_path, monkeypatch
):
    host, knowledge, historical_events = _setup(tmp_path, monkeypatch)
    knowledge.start_new_week(9)
    knowledge.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")

    _ask(host, channel_id=4, text="Who is the current HOH?")

    assert "WEEKLY STATE ARCHIVE" not in _prompt(host)


def test_current_state_still_outranks_a_stale_week_scoped_value_end_to_end(
    tmp_path, monkeypatch
):
    """The full production bug, end to end, exactly as specified for
    validation: CURRENT WEEK 9 (Barrett / Angela,Dee,Devens / veto+
    Have-Nots unconfirmed) coexists with HISTORICAL WEEK 7 (Dee /
    Drew,LaLa,Taylor / Yash / LaLa,Taylor,Mallory) in BOTH the
    KnowledgeStore weekly archive AND the separate, administrator-
    verified HistoricalEventStore (Phase 1, HOH-only). Asking for the
    CURRENT game state must reference Week 9's real values and must
    NOT let Week 7 override them; asking for the Week 7 HOH must still
    correctly answer Dee -- proving this fix does not destroy
    historical retrieval to achieve current-state correctness."""

    host, knowledge, historical_events = _setup(tmp_path, monkeypatch)
    knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")
    knowledge.teach(
        KnowledgeType.STATE, "Drew, LaLa, Taylor", author_id=1, topic="NOMINEES"
    )
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="VETO_WINNER")
    knowledge.teach(
        KnowledgeType.STATE, "LaLa, Taylor, Mallory", author_id=1, topic="HAVE_NOTS"
    )
    # Administrator-verified structured historical record for Week 7's
    # HOH -- the SEPARATE Phase 1 system (database/historical_events.py)
    # that "who was HOH in Week N?" actually answers from. This proves
    # the fix doesn't merely rely on the weekly archive for history.
    claim = historical_events.record_hoh_claim(
        season=28, cycle_sequence_number=7, week_number=7, winner="Dee",
        source_type="manual_admin_note", source_ref="validation-test",
    )
    historical_events.verify_hoh(claim.id, author_id=1)

    knowledge.start_new_week(9)
    knowledge.teach(KnowledgeType.STATE, "Barrett", author_id=1, topic="HOH")
    knowledge.teach(
        KnowledgeType.STATE, "Angela, Dee, Devens", author_id=1, topic="NOMINEES"
    )

    _ask(host, channel_id=5, text="What's the current game state?")

    prompt = _prompt(host)
    # "OFFICIAL GAME FACTS" also appears earlier in SYSTEM_INSTRUCTION's
    # own boilerplate rules text -- the rendered block itself always
    # starts with this exact, more specific header (see
    # format_official_state()).
    start = prompt.index("OFFICIAL GAME FACTS (admin-confirmed")
    # SYSTEM_INSTRUCTION's own boilerplate mentions "HOSTING GUIDANCE"
    # in passing well before the actual block -- anchor on its full,
    # rendered header instead (see format_response_guidance()).
    end = prompt.index("HOSTING GUIDANCE FOR THIS REPLY")
    official_block = prompt[start:end]

    assert "Hoh: Barrett" in official_block
    assert "Hoh: Dee" not in official_block
    assert "Drew" not in official_block
    assert "Yash" not in official_block
    assert "Mallory" not in official_block

    # Historical retrieval must still work: asking specifically about
    # Week 7 must still correctly answer Dee, via the SAME
    # HistoricalEventStore this fix left untouched.
    from production.historical_retrieval import retrieve_hoh

    week7_result = retrieve_hoh("What was the Week 7 HOH?", historical_events)
    assert len(week7_result.events) == 1
    winners = [
        p.houseguest
        for p in week7_result.events[0].participants
        if p.role == "WINNER"
    ]
    assert winners == ["DEE"]
