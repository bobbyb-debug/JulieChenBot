"""
Julie ChenBot Learned Knowledge
===============================

Durable, administrator-taught knowledge: facts, behavioral rules, and
corrections explicitly given to Julie via the /teach command family
(see commands/teach.py). This is deliberately separate from every
automated source Julie already has (RSS/parser-derived HouseStatus/
CompetitionState in production/house_status.py and production/
competition.py, the persisted AI chat_history in services/
ai_service.py) -- an administrator's explicit correction must survive
independently of all of those and must never be silently overridden
by the AI's own inference.

Persistence follows the exact pattern already established for A3
(ProductionEngine._persist_pending_events()/_load_pending_events() in
production/engine.py) and the game-state persistence work
(ProductionEngine._persist_game_state()/_load_game_state()): one
Storage key holding a JSON-safe list of dicts, loaded once at
construction and written immediately on every mutation via
Storage.set() (which is already atomic -- see database/storage.py).
A malformed record is logged and skipped, never allowed to prevent
startup, matching _load_pending_events()'s per-entry try/except.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Optional

from database.storage import Storage
from services.logger import ProductionLogger

logger = ProductionLogger.get("Knowledge")


# ==========================================================
# Knowledge Type
# ==========================================================


class KnowledgeType(str, Enum):
    """The knowledge shapes /teach supports.

    FACT/RULE accumulate (general, stable knowledge). CORRECTION
    explicitly supersedes something specific via the supersedes
    mechanism below, at the teacher's discretion. STATE is
    fundamentally different from all three: it represents one current
    game value for a given topic (see KnowledgeItem.topic /
    KnowledgeStore.active_state()) and a new STATE write for the same
    topic ALWAYS automatically supersedes the previous active STATE
    for that topic -- there is never more than one active STATE item
    per topic, by construction, not by teacher discipline.
    """

    FACT = "fact"

    RULE = "rule"

    CORRECTION = "correction"

    STATE = "state"


# ==========================================================
# Knowledge Item
# ==========================================================


@dataclass(slots=True)
class KnowledgeItem:
    """One piece of administrator-taught knowledge."""

    id: int

    type: KnowledgeType

    content: str

    author_id: int

    created_at: datetime

    updated_at: datetime

    active: bool = True

    # ID of the knowledge item this one explicitly supersedes, if any
    # (see KnowledgeStore.teach()'s supersedes parameter). Forward-only
    # reference on the newer item -- enough to audit "correction #45
    # superseded #12" without a second field to keep in sync on the
    # older item.
    supersedes: Optional[int] = None

    # Only meaningful for type == STATE: the game-state field this
    # item is the current value of (e.g. "HOH", "NOMINEES"), always
    # normalized upper-case by KnowledgeStore.teach(). None for every
    # other type. This is the key active_state() looks up by, and
    # what lets a new STATE write for the same topic find and
    # supersede the previous one automatically.
    topic: Optional[str] = None

    # Optional free-text source/reason a moderator supplied when
    # teaching this item (see commands/teach.py's /teach update
    # `reason` parameter) -- part of this item's audit trail, kept
    # separate from `content` so content stays exactly the taught
    # value (e.g. "Yash") rather than a value+rationale blob.
    note: Optional[str] = None

    # ======================================================
    # Serialization
    # ======================================================

    def to_dict(self) -> dict:
        """Converts to a JSON-safe dictionary for durable persistence."""

        return {
            "id": self.id,
            "type": self.type.value,
            "content": self.content,
            "author_id": self.author_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "active": self.active,
            "supersedes": self.supersedes,
            "topic": self.topic,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeItem":
        """Restores a KnowledgeItem from a previously persisted dictionary.

        Raises on genuinely malformed data (missing/wrong-shaped
        required fields, an unknown type, an unparsable timestamp) --
        callers (see KnowledgeStore._load()) are expected to catch
        this per-record and skip it rather than let one bad record
        block every other one, or startup itself.

        topic defaults to None for records persisted before STATE
        existed, matching the same backward-compatible-default pattern
        already used for `active` above.
        """

        supersedes = data.get("supersedes")

        return cls(
            id=int(data["id"]),
            type=KnowledgeType(data["type"]),
            content=str(data["content"]),
            author_id=int(data["author_id"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            active=bool(data.get("active", True)),
            supersedes=int(supersedes) if supersedes is not None else None,
            topic=data.get("topic"),
            note=data.get("note"),
        )


# ==========================================================
# Knowledge Store
# ==========================================================


class KnowledgeStore:
    """Durable store of administrator-taught knowledge, backed by the
    existing Storage abstraction -- no separate database, no new
    persistence framework.

    Deletion is soft (see forget()): a forgotten item stays in
    storage with active=False rather than being erased, so the
    knowledge ID an admin references never becomes ambiguous and the
    history remains auditable. Only active_items() feeds the AI
    context (see services/ai_service.py format_learned_knowledge()).
    """

    STORAGE_KEY = "knowledge"

    def __init__(self, storage: Optional[Storage] = None) -> None:
        self.storage = storage or Storage()
        self._items: list[KnowledgeItem] = self._load()

        logger.info(
            "Knowledge store initialized (%d item(s), %d active).",
            len(self._items),
            len(self.active_items()),
        )

    # ======================================================
    # Persistence
    # ======================================================

    def _load(self) -> list[KnowledgeItem]:
        items: list[KnowledgeItem] = []

        for data in self.storage.get(self.STORAGE_KEY, []):
            try:
                items.append(KnowledgeItem.from_dict(data))
            except Exception:
                logger.warning("Discarding malformed knowledge record: %r", data)

        return items

    def _persist(self) -> None:
        self.storage.set(
            self.STORAGE_KEY,
            [item.to_dict() for item in self._items],
        )

    # ======================================================
    # Mutation
    # ======================================================

    def teach(
        self,
        knowledge_type: KnowledgeType,
        content: str,
        author_id: int,
        supersedes: Optional[int] = None,
        topic: Optional[str] = None,
        note: Optional[str] = None,
    ) -> KnowledgeItem:
        """Records one new piece of authoritative knowledge.

        IDs are derived from the current maximum rather than a
        separately persisted counter: forgetting is soft (the record
        stays in self._items with active=False), so the highest ID
        already used never disappears from the list, and this stays
        safe/simple without an extra Storage key to keep in sync.

        supersedes, when given, names an existing knowledge item (of
        any type) that this new item explicitly replaces. The target
        must exist -- raises ValueError otherwise, before anything is
        created or persisted, so a typo'd ID can never silently create
        an orphaned correction. If the target exists, both the new
        item and the target's deactivation are applied to self._items
        in memory and written with a single _persist() call: there is
        no sequence of two separate persisted writes, so a failure
        partway through can never leave a persisted state where the
        new item exists but the item it was meant to replace is still
        active. If the target is already inactive, deactivating it
        again is simply a no-op (idempotent, matches forget()).

        topic is required for KnowledgeType.STATE (raises ValueError
        otherwise -- a STATE item with no topic could never be looked
        up or correctly superseded later) and rejected for every other
        type (raises ValueError -- topic only means something for
        STATE). For a STATE write, when supersedes is not explicitly
        given, this method automatically looks up the current
        active_state() for the same topic and supersedes it -- this is
        the whole mechanism behind "a new STATE: HOH = Barrett
        automatically replaces STATE: HOH = Yash" without the caller
        needing to already know the old item's ID. An explicit
        supersedes always takes precedence over the automatic lookup.
        """

        if knowledge_type == KnowledgeType.STATE:
            normalized_topic = (topic or "").strip().upper()
            if not normalized_topic:
                raise ValueError(
                    "A STATE item requires a topic (e.g. HOH, NOMINEES)."
                )
            topic = normalized_topic
            if supersedes is None:
                current = self.active_state(topic)
                if current is not None:
                    supersedes = current.id
        elif topic is not None:
            raise ValueError("topic is only valid for KnowledgeType.STATE.")

        superseded_item: Optional[KnowledgeItem] = None

        if supersedes is not None:
            superseded_item = self.get(supersedes)
            if superseded_item is None:
                raise ValueError(
                    f"No knowledge item #{supersedes} exists to supersede."
                )

        next_id = max((item.id for item in self._items), default=0) + 1
        now = datetime.now(UTC)

        item = KnowledgeItem(
            id=next_id,
            type=knowledge_type,
            content=content,
            author_id=author_id,
            created_at=now,
            updated_at=now,
            active=True,
            supersedes=supersedes,
            topic=topic,
            note=note,
        )

        self._items.append(item)

        if superseded_item is not None and superseded_item.active:
            superseded_item.active = False
            superseded_item.updated_at = now

        self._persist()

        logger.info(
            "Taught knowledge #%d (%s) by user %d: %s%s",
            item.id,
            knowledge_type.value,
            author_id,
            content,
            f" (supersedes #{supersedes})" if supersedes is not None else "",
        )

        return item

    def forget(self, item_id: int) -> bool:
        """Deactivates an active knowledge item. Returns False if no
        such active item exists (already forgotten, or never existed)
        -- forgetting is idempotent, never an error."""

        item = self.get(item_id)

        if item is None or not item.active:
            return False

        item.active = False
        item.updated_at = datetime.now(UTC)
        self._persist()

        logger.info("Forgot knowledge #%d.", item_id)

        return True

    # ======================================================
    # Reads
    # ======================================================

    def get(self, item_id: int) -> Optional[KnowledgeItem]:
        return next((item for item in self._items if item.id == item_id), None)

    def active_items(self) -> list[KnowledgeItem]:
        """Returns active knowledge in the order it was taught --
        this is what reaches the AI (see services/ai_service.py
        format_learned_knowledge()); forgotten items are excluded."""

        return [item for item in self._items if item.active]

    def all_items(self) -> list[KnowledgeItem]:
        return list(self._items)

    def active_state(self, topic: str) -> Optional[KnowledgeItem]:
        """Returns the currently active STATE item for one topic (e.g.
        "HOH", "NOMINEES"), or None if nothing has been taught for it.

        There is never more than one active STATE item per topic --
        teach() automatically supersedes the previous one on every new
        STATE write for the same topic -- so the first match found is
        the only one there should ever be.
        """

        normalized = topic.strip().upper()
        return next(
            (
                item
                for item in self._items
                if item.active
                and item.type == KnowledgeType.STATE
                and item.topic == normalized
            ),
            None,
        )
