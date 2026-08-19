"""
Julie ChenBot Long-Term Conversational Memory
==================================================

A user explicitly asking Julie to remember something ("Julie, remember
that we call the Have-Not room the Slop Dungeon") via /remember --
durable, but deliberately separate from both the rolling chat_messages
window (services/ai_service.py, bounded, recent-only) and from
KnowledgeStore/official game facts (production/knowledge.py,
admin-only, dashboard-authoritative).

Design choice -- explicit trigger only, not automatic extraction of
"meaningful" moments from ordinary conversation: an automatic
extractor needs an interpretive judgment call (an LLM call or a
heuristic) about what's "worth remembering," and either one can
misfire in both directions -- silently dropping something that
mattered, or manufacturing a "memory" out of a joke, a hypothetical,
or someone else's claim. An explicit /remember has none of that
failure mode: what's stored is exactly, verbatim, what someone asked
Julie to remember, with no inference step to get wrong -- reliable,
simple, deterministic, and easy to test.

Persistence follows the same pattern as KnowledgeStore/pending events:
one Storage key holding a JSON-safe list of dicts, loaded once at
construction, written immediately on every mutation via Storage.set()
(already atomic -- see database/storage.py). Scoped by channel_id,
matching services/ai_service.py's existing chat_messages scoping
exactly -- a DM's channel_id is unique to that DM, so a memory made in
a DM is never visible from a guild channel, and vice versa, with no
new privacy logic required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from database.storage import Storage
from services.logger import ProductionLogger

logger = ProductionLogger.get("Memory")


@dataclass(slots=True)
class MemoryItem:
    """One explicitly-remembered piece of conversational context."""

    id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryItem":
        return cls(
            id=int(data["id"]),
            channel_id=int(data["channel_id"]),
            author_id=int(data["author_id"]),
            author_name=str(data.get("author_name") or ""),
            content=str(data["content"]),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


_STOPWORDS = frozenset(
    {
        "the", "and", "for", "are", "was", "were", "you", "your",
        "what", "when", "where", "who", "why", "how", "did", "does",
        "with", "that", "this", "have", "has", "had", "not", "but",
        "julie", "please", "can", "could", "would", "should", "about",
    }
)


class MemoryStore:
    """Durable store of explicit long-term memories, backed by the
    existing Storage abstraction -- no separate database, no new
    persistence framework, and never shared with KnowledgeStore's
    "storage.json" key."""

    STORAGE_KEY = "long_term_memory"

    def __init__(self, storage: Optional[Storage] = None) -> None:
        self.storage = storage or Storage()
        self._items: list[MemoryItem] = self._load()

        logger.info(
            "Long-term memory store initialized (%d item(s)).",
            len(self._items),
        )

    def _load(self) -> list[MemoryItem]:
        items: list[MemoryItem] = []

        for data in self.storage.get(self.STORAGE_KEY, []):
            try:
                items.append(MemoryItem.from_dict(data))
            except Exception:
                logger.warning("Discarding malformed memory record: %r", data)

        return items

    def _persist(self) -> None:
        self.storage.set(
            self.STORAGE_KEY,
            [item.to_dict() for item in self._items],
        )

    def remember(
        self,
        channel_id: int,
        author_id: int,
        author_name: str,
        content: str,
    ) -> MemoryItem:
        """Records one new long-term memory. No supersede/conflict
        logic -- multiple memories can freely coexist, even
        contradictory ones; resolving that is a conversational
        problem for Julie/the Houseguests, not something this store
        tries to arbitrate."""

        next_id = max((item.id for item in self._items), default=0) + 1

        item = MemoryItem(
            id=next_id,
            channel_id=channel_id,
            author_id=author_id,
            author_name=author_name,
            content=content,
            created_at=datetime.now(UTC),
        )

        self._items.append(item)
        self._persist()

        logger.info(
            "Remembered #%d for channel %d (by %s): %s",
            item.id,
            channel_id,
            author_name,
            content,
        )

        return item

    def recall(self, channel_id: int, query: str, limit: int = 5) -> list[MemoryItem]:
        """Returns up to `limit` memories for this channel most
        relevant to `query`, via plain case-insensitive keyword-
        overlap scoring -- deliberately not an LLM call or embedding
        lookup, so this can never hallucinate a match that isn't
        actually there. Falls back to the `limit` most recent
        memories for the channel when no keyword overlaps at all, so
        a vague question ("what do we call that room again?") still
        gets some context rather than none.
        """

        scoped = [item for item in self._items if item.channel_id == channel_id]

        if not scoped:
            return []

        query_words = {
            word for word in query.lower().split()
            if len(word) > 2 and word not in _STOPWORDS
        }

        scored = []
        for item in scoped:
            content_words = set(item.content.lower().split())
            score = len(query_words & content_words)
            if score > 0:
                scored.append((score, item))

        if scored:
            scored.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
            return [item for _, item in scored[:limit]]

        return list(scoped[-limit:])

    def all_items(self) -> list[MemoryItem]:
        return list(self._items)
