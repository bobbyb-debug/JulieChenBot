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

import services.ai_service as ai_service
from production.house_status import HouseStatus
from production.competition import CompetitionState
from production.knowledge import KnowledgeType
from production.memory import MemoryStore
from services.discord import DiscordService


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
    def __init__(self) -> None:
        self.house_status = type("H", (), {"current": HouseStatus()})()
        self.competition = type("C", (), {"current": CompetitionState()})()


class _FakeEngine:
    def __init__(self, knowledge, memory) -> None:
        self.watcher = _FakeWatcher()
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
        from types import SimpleNamespace
        self.scheduler = SimpleNamespace(engine=engine)


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


def _setup(tmp_path: Path, monkeypatch, reply_text: str):
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
    engine = _FakeEngine(spy, memory)
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
