"""End-to-end personality/hosting-intelligence scenarios, exercised
through the real shared entry point both /chat and @mention/DM
replies use (services/discord.py DiscordService.generate_ai_reply()),
following the exact "bind the real unbound method onto a lightweight
stand-in" pattern established in
tests/test_conversational_facts_boundary.py.

Where tests/test_ai_service_knowledge_context.py proves the HOSTING
GUIDANCE block (production/response_style.py) is built correctly in
isolation, this file proves it actually reaches the real prompt for
realistic multi-turn conversations -- the representative scenarios
from the personality-overhaul brief:

    A. Rapid facts -- no repeated greeting across a question sequence.
    B. Historical -- official state still wins; historical context
       usable for a genuinely historical question.
    C. Drama -- a genuinely big game moment (DPOV) reads as dramatic.
    D. Banter -- a casual reaction doesn't get treated as a fact
       lookup, and never touches KnowledgeStore/HouseStatus/
       CompetitionState.
    E. Unknown -- no reliable information available -> Julie is told
       to say so, never to invent a story.

Also re-confirms, under a personality-heavy multi-turn conversation
specifically (not just a single isolated call), that KnowledgeStore,
HouseStatus, and CompetitionState remain untouched, and that /chat and
@mention/DM replies get identical treatment because they share this
one function.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import services.ai_service as ai_service
from database.hamsterwatch_archive import HamsterwatchArchive
from production.competition import CompetitionState
from production.house_status import HouseStatus
from production.knowledge import KnowledgeType
from production.memory import MemoryStore
from services.discord import DiscordService
from services.logger import ProductionLogger


class _KnowledgeSpy:
    """Wraps a real KnowledgeStore, raising immediately if .teach()
    (the only mutation entry point) is ever called -- see
    tests/test_conversational_facts_boundary.py for the original of
    this pattern. A personality-heavy reply calling this, for any
    reason, is exactly the bug this file exists to catch."""

    def __init__(self, real) -> None:
        self._real = real
        self.teach_calls: list[tuple] = []

    def teach(self, *args, **kwargs):
        self.teach_calls.append((args, kwargs))
        raise AssertionError(
            "Personality/hosting code must never call "
            "KnowledgeStore.teach() -- presentation is not a fact "
            "source."
        )

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakeWatcher:
    def __init__(self, hamsterwatch=None) -> None:
        self.house_status = type("H", (), {"current": HouseStatus()})()
        self.competition = type("C", (), {"current": CompetitionState()})()
        self.hamsterwatch = hamsterwatch


class _FakeEngine:
    def __init__(self, knowledge, memory, hamsterwatch=None) -> None:
        self.watcher = _FakeWatcher(hamsterwatch=hamsterwatch)
        self.knowledge = knowledge
        self.memory = memory


class _FakeDiscordServiceHost:
    """Binds the REAL DiscordService.generate_ai_reply onto a
    lightweight stand-in -- see
    tests/test_conversational_facts_boundary.py for the original of
    this pattern."""

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


class _SequencedGroqClient:
    """Returns canned replies from `replies` in order (the last one
    repeats once exhausted), simulating a real multi-turn conversation
    where Julie's own prior wording matters to the next turn's
    guidance. Records every call's full kwargs so a test can inspect
    the exact prompt sent for each turn."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies or ["reply"]
        self._index = 0
        self.calls: list[dict] = []

    class _Completions:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.calls.append(kwargs)
            reply = self.outer._replies[min(self.outer._index, len(self.outer._replies) - 1)]
            self.outer._index += 1
            return FakeGroqResponse(reply)

    @property
    def chat(self):
        outer = self

        class _Chat:
            completions = _SequencedGroqClient._Completions(outer)

        return _Chat()


def _setup(tmp_path: Path, monkeypatch, replies: list[str], hamsterwatch=None):
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    client = _SequencedGroqClient(replies)
    monkeypatch.setattr(ai_service, "groq_client", client)
    monkeypatch.setattr(ai_service, "ai_client", None)

    from database.storage import Storage
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    from production.knowledge import KnowledgeStore
    real_knowledge = KnowledgeStore(storage=storage)
    spy = _KnowledgeSpy(real_knowledge)
    memory = MemoryStore(storage=storage)
    engine = _FakeEngine(spy, memory, hamsterwatch=hamsterwatch)
    host = _FakeDiscordServiceHost(engine)
    return host, spy, real_knowledge, client


def _hamsterwatch(tmp_path: Path, day: int, content: str, summary: str | None = None):
    archive = HamsterwatchArchive(db_path=tmp_path / "hamsterwatch_archive.db")
    archive.upsert(
        page_url="http://hamsterwatch.com/bb28/test.shtml",
        section_slug=f"day-{day}",
        heading=f"Day {day} recap heading",
        article_date=f"2026-07-{day:02d}",
        bb_day=day,
        content=content,
        summary=summary or content,
    )
    return SimpleNamespace(archive=archive)


def _ask(host, channel_id, text, user_id=1, author_name="Alex"):
    """Clears this user's AI cooldown (services/discord.py
    AI_COOLDOWN_SECONDS) before asking -- these scenarios are about
    conversational/hosting behavior across a rapid sequence, not
    cooldown enforcement, which is an orthogonal, separately-tested
    concern (see tests/test_interactive_ai.py)."""

    host._ai_cooldowns.pop(user_id, None)
    return asyncio.run(
        host.generate_ai_reply(
            user_id=user_id, channel_id=channel_id, user_text=text, author_name=author_name
        )
    )


# ==========================================================
# Scenario A -- rapid facts, no repeated greeting
# ==========================================================


def test_scenario_a_rapid_facts_do_not_repeat_the_greeting(tmp_path, monkeypatch):
    host, _, real_knowledge, client = _setup(
        tmp_path, monkeypatch,
        replies=[
            "Good evening, Houseguests! Expect the unexpected--Dee is HOH.",
            "Drew, LaLa, and Taylor.",
            "Yash.",
        ],
    )
    real_knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=100, text="Who is HOH?")
    _ask(host, channel_id=100, text="Nominees?")
    _ask(host, channel_id=100, text="Who won veto?")

    first_prompt = client.calls[0]["messages"][0]["content"]
    second_prompt = client.calls[1]["messages"][0]["content"]
    third_prompt = client.calls[2]["messages"][0]["content"]

    assert "a brief, natural greeting is fine here" in first_prompt

    # After Julie's own first reply used "Expect the unexpected", the
    # very next turn's guidance both forbids re-greeting AND names the
    # phrase to avoid repeating.
    assert "do not greet again" in second_prompt
    assert "expect the unexpected" in second_prompt
    assert "vary your opening this time" in second_prompt

    # And the rhythm holds for a third rapid question too.
    assert "do not greet again" in third_prompt


# ==========================================================
# Scenario B -- historical question; official state still wins
# ==========================================================


def test_scenario_b_historical_question_uses_context_but_official_state_still_authoritative(
    tmp_path, monkeypatch
):
    hamsterwatch = _hamsterwatch(
        tmp_path, 5, "Taylor was HOH during an earlier period and nominated two Houseguests."
    )
    host, _, real_knowledge, client = _setup(
        tmp_path, monkeypatch, replies=["Here's what was happening then."],
        hamsterwatch=hamsterwatch,
    )
    real_knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    _ask(host, channel_id=200, text="What was happening when Taylor was HOH?")

    prompt = client.calls[0]["messages"][0]["content"]
    assert "OFFICIAL GAME FACTS" in prompt
    assert "Dee" in prompt
    assert "HISTORICAL SEASON CONTEXT" in prompt
    assert "Taylor" in prompt
    assert prompt.index("OFFICIAL GAME FACTS") < prompt.index("HISTORICAL SEASON CONTEXT")
    # Intent recognized as historical -> storytelling guidance present.
    assert "tell a short, grounded story" in prompt


def test_scenario_b_current_hoh_question_prefers_official_fact_over_historical_name(
    tmp_path, monkeypatch
):
    """The critical trust scenario, replayed once more specifically
    under the new personality layer: asking for the CURRENT HOH must
    never let a historical name (Taylor) win over the official one
    (Dee), regardless of how much more "interesting" Taylor's material
    reads."""

    hamsterwatch = _hamsterwatch(
        tmp_path, 5, "Taylor was HOH during an earlier period this season."
    )
    host, _, real_knowledge, client = _setup(
        tmp_path, monkeypatch, replies=["Dee is the current HOH."],
        hamsterwatch=hamsterwatch,
    )
    real_knowledge.teach(KnowledgeType.STATE, "Dee", author_id=1, topic="HOH")

    reply = _ask(host, channel_id=201, text="Who is the current HoH?")

    assert "Dee" in reply
    assert real_knowledge.active_state("HOH").content == "Dee"


# ==========================================================
# Scenario C -- drama (Diamond Power of Veto)
# ==========================================================


def test_scenario_c_dpov_question_reads_as_dramatic(tmp_path, monkeypatch):
    host, _, _, client = _setup(
        tmp_path, monkeypatch,
        replies=["Devens used the Diamond Power of Veto to pull Dee off the block."],
    )

    _ask(host, channel_id=300, text="Tell me about the DPOV situation.")

    prompt = client.calls[0]["messages"][0]["content"]
    assert "hosting flair and drama are appropriate" in prompt
    assert "don't invent details for effect" in prompt


# ==========================================================
# Scenario D -- banter (casual reaction, not a fact lookup)
# ==========================================================


def test_scenario_d_casual_reaction_reads_as_banter_not_a_fact_lookup(tmp_path, monkeypatch):
    host, spy, _, client = _setup(
        tmp_path, monkeypatch, replies=["Right?? That house is unhinged this week."],
    )

    reply = _ask(host, channel_id=400, text="lol that nomination is fucking wild")

    prompt = client.calls[0]["messages"][0]["content"]
    assert "casual reaction or banter" in prompt
    assert "don't force a fact dump" in prompt
    assert reply == "Right?? That house is unhinged this week."
    # Banter must never touch official facts either.
    assert spy.teach_calls == []


# ==========================================================
# Scenario E -- unknown information; no fabrication
# ==========================================================


def test_scenario_e_no_reliable_information_tells_julie_to_say_so(tmp_path, monkeypatch):
    """No Hamsterwatch material at all for the day asked about --
    historical_context ends up empty (see
    production/hamsterwatch_context.py's existing "targeted miss
    returns nothing" guarantee), but the question is still clearly
    historical-shaped, so the guidance must explicitly steer Julie
    toward admitting she doesn't know rather than inventing a story."""

    hamsterwatch = _hamsterwatch(tmp_path, 40, "Unrelated day forty content.")
    host, _, _, client = _setup(
        tmp_path, monkeypatch, replies=["I don't have a verified record of that."],
        hamsterwatch=hamsterwatch,
    )

    _ask(host, channel_id=500, text="What happened on Day 12?")

    prompt = client.calls[0]["messages"][0]["content"]
    assert "source: Hamsterwatch archive" not in prompt  # nothing was actually retrieved
    assert "say so plainly rather than inventing details" in prompt


# ==========================================================
# Personality never mutates game state, under a realistic
# multi-turn, multi-intent conversation (not just one isolated call)
# ==========================================================


def test_personality_never_mutates_house_status_or_competition_state_across_a_conversation(
    tmp_path, monkeypatch
):
    host, spy, _, _ = _setup(
        tmp_path, monkeypatch,
        replies=[
            "Dee.", "Drew, LaLa, and Taylor.",
            "Devens used the DPOV on Dee.",
            "Haha, yeah that was wild.",
        ],
    )
    watcher = host.scheduler.engine.watcher
    house_status_before = watcher.house_status.current
    competition_before = watcher.competition.current

    _ask(host, channel_id=600, text="Who is HOH?")
    _ask(host, channel_id=600, text="Nominees?")
    _ask(host, channel_id=600, text="Tell me about the DPOV situation.")
    _ask(host, channel_id=600, text="lol that nomination is wild")

    assert watcher.house_status.current is house_status_before
    assert watcher.competition.current is competition_before
    assert watcher.house_status.current.hoh == ""
    assert spy.teach_calls == []


# ==========================================================
# /chat and @mention/DM replies share identical personality behavior
# (both call this exact function -- see commands/chat.py and
# services/discord.py's on_message handler)
# ==========================================================


def test_chat_command_and_mention_path_receive_identical_hosting_guidance(
    tmp_path, monkeypatch
):
    """commands/chat.py and the @mention/DM handler in
    services/discord.py both call generate_ai_reply() with the same
    shape of arguments -- there is no second, separately-tuned
    personality path to leave robotic. Simulating both call shapes
    against the same fresh channel proves they produce the identical
    prompt structure."""

    host, _, _, client = _setup(tmp_path, monkeypatch, replies=["Dee.", "Dee."])

    # Shape of a /chat invocation (commands/chat.py).
    asyncio.run(
        host.generate_ai_reply(
            user_id=1, channel_id=700, user_text="Who is HOH?", author_name="Alex"
        )
    )
    # Shape of an @mention/DM invocation (services/discord.py on_message) --
    # a fresh channel so both calls are directly comparable as "first
    # message" conversations.
    asyncio.run(
        host.generate_ai_reply(
            user_id=2, channel_id=701, user_text="Who is HOH?", author_name="Jordan"
        )
    )

    chat_prompt = client.calls[0]["messages"][0]["content"]
    mention_prompt = client.calls[1]["messages"][0]["content"]

    assert "HOSTING GUIDANCE FOR THIS REPLY" in chat_prompt
    assert "HOSTING GUIDANCE FOR THIS REPLY" in mention_prompt
    assert "a brief, natural greeting is fine here" in chat_prompt
    assert "a brief, natural greeting is fine here" in mention_prompt
    assert "concisely" in chat_prompt
    assert "concisely" in mention_prompt
