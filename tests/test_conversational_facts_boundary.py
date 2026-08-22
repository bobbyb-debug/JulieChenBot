"""Integration proof of the absolute memory boundary: a conversational
message -- a user's claim, or Julie's own AI-generated reply -- must
NEVER become an official game fact (KnowledgeStore), no matter how
confidently either one states something. Exercised through the real
shared entry point both /chat and @mention/DM use
(services/discord.py DiscordService.generate_ai_reply()), not a
reimplementation of it -- see tests/test_interactive_ai.py's
_CooldownOnly for the same "bind the real unbound method onto a
lightweight stand-in" pattern used here.

Also covers: conversational history persists with author identity
through this exact path (E/F/I), and /forget clearing chat history
never touches official facts (L).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import services.ai_service as ai_service
from database.hamsterwatch_archive import HamsterwatchArchive
from production.house_status import HouseStatus
from production.competition import CompetitionState
from production.knowledge import KnowledgeType
from production.memory import MemoryStore
from services.discord import DiscordService
from services.logger import ProductionLogger


class _KnowledgeSpy:
    """Wraps a real KnowledgeStore, allowing every read but raising
    immediately if .teach() (the only mutation entry point -- see
    production/knowledge.py) is ever called. A conversational path
    calling this, for any reason, is exactly the bug this test exists
    to catch."""

    def __init__(self, real) -> None:
        self._real = real
        self.teach_calls: list[tuple] = []

    def teach(self, *args, **kwargs):
        self.teach_calls.append((args, kwargs))
        raise AssertionError(
            "Conversational/AI-chat code path must never call "
            "KnowledgeStore.teach() -- this would silently turn "
            "conversation into an official game fact."
        )

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakeWatcher:
    def __init__(self, hamsterwatch=None) -> None:
        self.house_status = type("H", (), {"current": HouseStatus()})()
        self.competition = type("C", (), {"current": CompetitionState()})()
        # Deliberately optional and defaulting to None (not simply
        # omitted): existing tests below construct this with no
        # hamsterwatch at all, exercising the exact same
        # getattr(engine.watcher, "hamsterwatch", None) fallback path
        # generate_ai_reply() must use for a real ProductionWatcher
        # whose HamsterwatchMonitor failed to construct (see
        # production/watcher.py) -- an attribute holding None and a
        # genuinely missing attribute are indistinguishable to that
        # getattr() call, so this is not a behavior change for them.
        self.hamsterwatch = hamsterwatch


class _FakeEngine:
    def __init__(self, knowledge, memory, hamsterwatch=None) -> None:
        self.watcher = _FakeWatcher(hamsterwatch=hamsterwatch)
        self.knowledge = knowledge
        self.memory = memory


class _FakeDiscordServiceHost:
    """Binds the REAL DiscordService.generate_ai_reply/_ai_cooldown_remaining
    methods onto a lightweight stand-in, exactly like
    tests/test_interactive_ai.py's _CooldownOnly does for the cooldown
    method alone -- avoids constructing a real discord.py Bot/Storage-
    backed ProductionEngine just to exercise this one method."""

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


class _HostileGroqClient:
    """Simulates Julie confidently, incorrectly, stating a game fact
    that was never taught -- proving her own generated text is never
    itself treated as authoritative."""

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
            completions = _HostileGroqClient._Completions(outer)

        return _Chat()


def _setup(tmp_path: Path, monkeypatch, reply_text: str, hamsterwatch=None):
    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    monkeypatch.setattr(ai_service, "groq_client", _HostileGroqClient(reply_text))
    monkeypatch.setattr(ai_service, "ai_client", None)

    from database.storage import Storage
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()

    real_knowledge = __import__("production.knowledge", fromlist=["KnowledgeStore"]).KnowledgeStore(
        storage=storage
    )
    spy = _KnowledgeSpy(real_knowledge)
    memory = MemoryStore(storage=storage)
    engine = _FakeEngine(spy, memory, hamsterwatch=hamsterwatch)
    host = _FakeDiscordServiceHost(engine)
    return host, spy, real_knowledge


# ==========================================================
# J/K: a false user claim, and Julie's own confident (wrong) reply,
# never become official facts.
# ==========================================================


def test_user_claiming_a_false_fact_never_writes_to_knowledge_store(
    tmp_path: Path, monkeypatch
) -> None:
    host, spy, real_knowledge = _setup(
        tmp_path, monkeypatch, reply_text="Yash is HOH."
    )

    reply = asyncio.run(
        host.generate_ai_reply(
            user_id=555,
            channel_id=42,
            user_text="@Julie ChenBot Yash is HOH!",
            author_name="Bobby",
        )
    )

    assert "Yash is HOH" in reply  # Julie did say it conversationally...
    assert spy.teach_calls == []  # ...but it never became an official fact.
    assert real_knowledge.active_state("HOH") is None


def test_julies_own_speculative_reply_never_becomes_official_fact(
    tmp_path: Path, monkeypatch
) -> None:
    """Even Julie's own confidently-wrong generated text (a
    hallucination, a joke, a guess) must never be written back into
    KnowledgeStore merely because she said it."""

    host, spy, real_knowledge = _setup(
        tmp_path, monkeypatch, reply_text="Absolutely, Taylor just won the veto!"
    )

    asyncio.run(
        host.generate_ai_reply(
            user_id=1, channel_id=1, user_text="who has veto?", author_name="Alex"
        )
    )

    assert spy.teach_calls == []
    assert real_knowledge.active_state("VETO_WINNER") is None


# ==========================================================
# E/F/I: conversational history persists with author identity through
# this exact shared /chat + @mention/DM path.
# ==========================================================


def test_conversation_persists_with_author_identity_through_generate_ai_reply(
    tmp_path: Path, monkeypatch
) -> None:
    host, _, _ = _setup(tmp_path, monkeypatch, reply_text="Good evening, Houseguest.")

    asyncio.run(
        host.generate_ai_reply(
            user_id=99, channel_id=7, user_text="hi Julie", author_name="Jordan"
        )
    )

    history = ai_service._recent_history(7)
    assert history[-2] == ("user", "hi Julie", "Jordan")
    assert history[-1] == ("model", "Good evening, Houseguest.", None)


# ==========================================================
# L: /forget clears conversational history without corrupting
# official facts.
# ==========================================================


def test_forget_clears_chat_history_but_leaves_official_facts_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    from database.storage import Storage
    from production.knowledge import KnowledgeStore
    from services.ai_service import clear_history

    monkeypatch.setattr(ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat.db")
    monkeypatch.setattr(Storage, "FILE", tmp_path / "storage.json")
    storage = Storage()
    knowledge = KnowledgeStore(storage=storage)
    knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    ai_service.update_and_get_history(88, "hello", author_id=1, author_name="Bobby")
    ai_service.append_ai_response(88, "hi there")

    removed = clear_history(88)

    assert removed == 2
    assert ai_service._recent_history(88) == []
    # /forget must never touch official facts.
    assert knowledge.active_state("HOH").content == "Yash"


# ==========================================================
# M: Historical Hamsterwatch context -- retrieved read-only
# background, additive to /chat, never authoritative, and never a
# path back into KnowledgeStore/HouseStatus/CompetitionState.
# ==========================================================


def _hamsterwatch(tmp_path: Path, day: int, content: str, summary: str | None = None):
    """A real, file-backed HamsterwatchArchive (not a mock) wrapped
    the same shape generate_ai_reply() actually reads
    (engine.watcher.hamsterwatch.archive) -- proving the real
    retrieval/formatting code path end to end, not a stand-in for
    it."""

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


def test_generate_ai_reply_works_normally_with_no_hamsterwatch_attribute_at_all(
    tmp_path: Path, monkeypatch
) -> None:
    """Matches a real ProductionWatcher whose HamsterwatchMonitor
    failed to construct (see production/watcher.py
    _register_builtin_monitors()) -- self.hamsterwatch is simply never
    set in that case. /chat must degrade to its exact pre-feature
    behavior, not error."""

    host, _, _ = _setup(tmp_path, monkeypatch, reply_text="Good evening, Houseguest.")
    del host.scheduler.engine.watcher.hamsterwatch  # simulate a genuinely absent attribute

    reply = asyncio.run(
        host.generate_ai_reply(user_id=1, channel_id=1, user_text="hi Julie", author_name="Alex")
    )

    assert reply == "Good evening, Houseguest."


def test_generate_ai_reply_works_normally_when_hamsterwatch_has_no_relevant_material(
    tmp_path: Path, monkeypatch
) -> None:
    hamsterwatch = _hamsterwatch(tmp_path, 3, "Completely unrelated content about breakfast.")
    host, _, _ = _setup(
        tmp_path, monkeypatch, reply_text="I don't have anything on that.",
        hamsterwatch=hamsterwatch,
    )

    reply = asyncio.run(
        host.generate_ai_reply(
            user_id=1, channel_id=1, user_text="What happened on Day 99?", author_name="Alex"
        )
    )

    assert reply == "I don't have anything on that."
    prompt = host.scheduler.engine.watcher.hamsterwatch.archive  # sanity: still queryable
    assert prompt.count() == 1
    # No misleading fallback material reached the model. SYSTEM_INSTRUCTION
    # itself always names "HISTORICAL SEASON CONTEXT" in its boundary
    # paragraph, so the real proof of "no block was rendered" is the
    # absence of the formatted block's own marker text, not that label.
    groq_calls = ai_service.groq_client.calls
    system_message = groq_calls[0]["messages"][0]["content"]
    assert "source: Hamsterwatch archive" not in system_message
    assert "breakfast" not in system_message


def test_generate_ai_reply_includes_historical_context_when_relevant_material_exists(
    tmp_path: Path, monkeypatch
) -> None:
    hamsterwatch = _hamsterwatch(
        tmp_path, 12, "LaLa and Devens discussed the veto plan in detail on day twelve."
    )
    host, _, _ = _setup(
        tmp_path, monkeypatch, reply_text="Here's what happened.", hamsterwatch=hamsterwatch
    )

    asyncio.run(
        host.generate_ai_reply(
            user_id=1, channel_id=1, user_text="What happened on Day 12?", author_name="Alex"
        )
    )

    system_message = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "HISTORICAL SEASON CONTEXT" in system_message
    assert "Day 12" in system_message
    assert "veto plan" in system_message


def test_critical_trust_official_hoh_outranks_historical_hamsterwatch_material(
    tmp_path: Path, monkeypatch
) -> None:
    """The exact scenario this feature must never regress on:

        OFFICIAL GAME STATE: HOH = Yash
        HISTORICAL HAMSTERWATCH MATERIAL: "Taylor was HOH during an
          earlier period."

    Both pieces of information must reach the model, correctly
    labeled and correctly prioritized (OFFICIAL GAME FACTS ahead of
    HISTORICAL SEASON CONTEXT, exactly as it already outranks LIVE
    FEED OBSERVATION -- see
    tests/test_ai_service_knowledge_context.py's
    test_official_state_outranks_historical_context_in_prompt_order
    for the same guarantee at the formatter level). This test proves
    what our code controls -- what Julie is given and how it's
    framed; it cannot prove what an actual AI provider does with that
    prompt, which is outside this codebase.
    """

    hamsterwatch = _hamsterwatch(
        tmp_path, 5, "Taylor was HOH during an earlier period this season."
    )
    host, _, real_knowledge = _setup(
        tmp_path, monkeypatch, reply_text="Yash is the current HOH.",
        hamsterwatch=hamsterwatch,
    )
    real_knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    reply = asyncio.run(
        host.generate_ai_reply(
            user_id=1, channel_id=1, user_text="Who is the current HOH?", author_name="Alex"
        )
    )

    assert "Yash" in reply

    system_message = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "OFFICIAL GAME FACTS" in system_message
    assert "Yash" in system_message
    assert "HISTORICAL SEASON CONTEXT" in system_message
    assert "Taylor" in system_message
    assert system_message.index("OFFICIAL GAME FACTS") < system_message.index(
        "HISTORICAL SEASON CONTEXT"
    )
    # Official state was never touched by any of this.
    assert real_knowledge.active_state("HOH").content == "Yash"


def test_historical_question_about_a_different_topic_still_retrieves_its_own_material(
    tmp_path: Path, monkeypatch
) -> None:
    """Second half of the critical-trust scenario: asking a genuinely
    historical question (not a current-state question) must still
    surface the relevant Hamsterwatch material -- official state
    outranking historical context for CURRENT-state questions must
    not mean historical context is suppressed outright."""

    hamsterwatch = _hamsterwatch(
        tmp_path, 5, "Taylor was HOH during an earlier period and nominated two Houseguests."
    )
    host, _, real_knowledge = _setup(
        tmp_path, monkeypatch, reply_text="Here's what was happening then.",
        hamsterwatch=hamsterwatch,
    )
    real_knowledge.teach(KnowledgeType.STATE, "Yash", author_id=1, topic="HOH")

    asyncio.run(
        host.generate_ai_reply(
            user_id=1, channel_id=1,
            user_text="What was happening when Taylor was HOH?", author_name="Alex",
        )
    )

    system_message = ai_service.groq_client.calls[0]["messages"][0]["content"]
    assert "HISTORICAL SEASON CONTEXT" in system_message
    assert "Taylor" in system_message
    assert "nominated" in system_message


def test_historical_context_retrieval_never_calls_knowledge_teach(
    tmp_path: Path, monkeypatch
) -> None:
    hamsterwatch = _hamsterwatch(tmp_path, 5, "Taylor was HOH during an earlier period.")
    host, spy, _ = _setup(
        tmp_path, monkeypatch, reply_text="reply", hamsterwatch=hamsterwatch
    )

    asyncio.run(
        host.generate_ai_reply(
            user_id=1, channel_id=1, user_text="What happened on Day 5?", author_name="Alex"
        )
    )

    assert spy.teach_calls == []


def test_historical_context_retrieval_never_mutates_house_status_or_competition_state(
    tmp_path: Path, monkeypatch
) -> None:
    hamsterwatch = _hamsterwatch(tmp_path, 5, "Taylor was HOH during an earlier period.")
    host, _, _ = _setup(
        tmp_path, monkeypatch, reply_text="reply", hamsterwatch=hamsterwatch
    )
    watcher = host.scheduler.engine.watcher
    house_status_before = watcher.house_status.current
    competition_before = watcher.competition.current

    asyncio.run(
        host.generate_ai_reply(
            user_id=1, channel_id=1, user_text="What happened on Day 5?", author_name="Alex"
        )
    )

    assert watcher.house_status.current is house_status_before
    assert watcher.competition.current is competition_before
    assert watcher.house_status.current.hoh == ""  # untouched, still the default
