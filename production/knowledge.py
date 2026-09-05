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

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Optional

from database.storage import Storage
from services.logger import ProductionLogger

logger = ProductionLogger.get("Knowledge")


def _canonicalize_topic(topic: str) -> str:
    """Normalizes a STATE topic string to one canonical spelling.

    Forensic production finding: KnowledgeStore's old normalization
    (`.strip().upper()` alone) treats "VETO WINNER" and "VETO_WINNER"
    as two genuinely different topics, since it never collapses a
    space/underscore difference. A moderator typing a topic slightly
    differently on two occasions (free-text entry via /teach update or
    the dashboard -- see production/batch_teach.py) then produces two
    independently-"active" STATE items for what a human considers the
    same real-world fact, silently breaking the "at most one active
    STATE item per topic" guarantee every reader (active_state(),
    format_official_state(), /hoh, /nominees, /veto) relies on. Both
    survive, and whichever one a caller happens to query by its exact
    spelling is what's shown as "official" -- which can be the stale
    one. Collapsing every run of whitespace to a single underscore
    here, applied at both write time (teach()) and read time
    (active_state()), makes every spelling variant resolve to the same
    stored topic going forward. See KnowledgeStore.dedupe_topics() for
    repairing spellings already persisted before this existed.
    """

    return re.sub(r"\s+", "_", topic.strip().upper())


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
    WEEK_META_KEY = "game_week"

    # STATE topics representing "this competition cycle's" result --
    # the fields a new reporting week must never blindly inherit from
    # the previous one just because nobody got around to re-teaching
    # them (see current_state() and start_new_week()/close_week()
    # below). Every other STATE topic (e.g. REMAINING_HOUSEGUESTS,
    # LAST_EVICTED, VOTE, EVICTED) is a running/one-time fact, not a
    # per-week value, and is deliberately NOT in this set -- it keeps
    # active_state()'s plain "most recent taught value" semantics
    # forever, exactly as before this feature existed.
    WEEK_SCOPED_TOPICS = frozenset(
        {"HOH", "NOMINEES", "VETO_WINNER", "VETO_USED", "BB_BLOCKBUSTER", "HAVE_NOTS"}
    )

    def __init__(self, storage: Optional[Storage] = None) -> None:
        self.storage = storage or Storage()
        self._items: list[KnowledgeItem] = self._load()
        self._load_week_meta()

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

    def _load_week_meta(self) -> None:
        """Restores the current reporting week's boundary and the
        historical weekly archive (see start_new_week()/close_week()
        below). Absence of this key means week-tracking has never been
        turned on for this deployment: current_week/week_started_at
        stay None, and current_state() below behaves EXACTLY like
        active_state() always has -- no filtering, fully backward
        compatible until an admin explicitly starts week tracking via
        start_new_week()."""

        data = self.storage.get(self.WEEK_META_KEY, None) or {}

        self.current_week: Optional[int] = data.get("current_week")

        started_raw = data.get("started_at")
        self.week_started_at: Optional[datetime] = (
            datetime.fromisoformat(started_raw) if started_raw else None
        )

        self.week_archive: dict[int, dict] = {
            int(week): record for week, record in (data.get("archive") or {}).items()
        }

    def _persist_week_meta(self) -> None:
        self.storage.set(
            self.WEEK_META_KEY,
            {
                "current_week": self.current_week,
                "started_at": (
                    self.week_started_at.isoformat() if self.week_started_at else None
                ),
                "archive": {str(week): record for week, record in self.week_archive.items()},
            },
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
            normalized_topic = _canonicalize_topic(topic or "")
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

    def reactivate(self, item_id: int) -> bool:
        """Reactivates a deactivated knowledge item IN PLACE -- the
        same id, type, content, author_id, created_at, and topic;
        only `active` flips back to True (and `updated_at` advances).
        Never creates a new item. Returns False if no such inactive
        item exists (already active, or never existed) -- reactivating
        is idempotent, never an error, mirroring forget()'s posture.

        A STATE item is a special case: this store guarantees at most
        one active STATE item per topic (see active_state() and
        teach()'s own auto-supersede-on-write) -- every command and
        the AI chat context rely on that being true. Reactivating a
        STATE item whose topic currently has a DIFFERENT active STATE
        item would silently break that guarantee (two "current" values
        for one topic). So if one exists, it is deactivated first --
        exactly as if this item had just been re-taught for that
        topic -- keeping the invariant intact rather than adding a
        second, competing rule for STATE items to work around it.
        """

        item = self.get(item_id)

        if item is None or item.active:
            return False

        if item.type == KnowledgeType.STATE and item.topic:
            currently_active = self.active_state(item.topic)
            if currently_active is not None and currently_active.id != item.id:
                currently_active.active = False
                currently_active.updated_at = datetime.now(UTC)
                logger.info(
                    "Reactivating #%d superseded currently active STATE "
                    "#%d for topic %s.",
                    item_id,
                    currently_active.id,
                    item.topic,
                )

        item.active = True
        item.updated_at = datetime.now(UTC)
        self._persist()

        logger.info("Reactivated knowledge #%d.", item_id)

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

        # Deliberately NOT _canonicalize_topic() here -- this stays a
        # plain, literal-string lookup (matching every already-
        # persisted topic exactly as stored), so production/
        # state_sync.py's alias-aware resolver can still query several
        # distinct literal spellings of the same real-world topic (see
        # its own docstring) and tell whether they genuinely agree.
        # Canonicalizing the query here would collapse every spelling
        # variant into the same string before this method ever sees
        # them, making that distinction impossible to observe. New
        # writes are already canonical at the source (see teach()),
        # and dedupe_topics() migrates anything persisted before that
        # existed -- this method needs no canonicalization of its own
        # for either case to resolve correctly once that has run.
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

    def current_state(self, topic: str) -> Optional[KnowledgeItem]:
        """Like active_state(), but additionally enforces the current
        reporting week's boundary for WEEK_SCOPED_TOPICS: a value
        taught before this week started is NOT "current" merely
        because nobody has re-taught it since -- it returns None
        (meaning "unconfirmed this week"), the same way active_state()
        already returns None for a topic never taught at all.

        This is the fix for the production bug where a genuinely stale
        value (e.g. last week's HOH) kept being served indefinitely as
        "OFFICIAL GAME FACTS... ground truth" -- see /hoh, /nominees,
        /veto, and services/ai_service.py format_official_state(),
        every one of which must call this instead of active_state()
        for a WEEK_SCOPED_TOPICS field.

        A topic outside WEEK_SCOPED_TOPICS (REMAINING_HOUSEGUESTS,
        LAST_EVICTED, VOTE, EVICTED, or anything else an admin has
        taught) is unaffected -- it keeps active_state()'s plain
        "most recently taught value, however old" semantics, since
        those are running/one-time facts, not per-week values.

        When no week has ever been started (week_started_at is None,
        the default for a deployment that hasn't turned on week
        tracking yet -- see start_new_week()), this is identical to
        active_state(): fully backward compatible, no behavior change
        until an admin explicitly starts tracking weeks.
        """

        item = self.active_state(topic)
        if item is None:
            return None

        normalized = _canonicalize_topic(topic)
        if normalized in self.WEEK_SCOPED_TOPICS and self.week_started_at is not None:
            if item.created_at < self.week_started_at:
                return None

        return item

    def current_state_items(self) -> list[KnowledgeItem]:
        """Every active STATE item that counts as confirmed for the
        CURRENT reporting week -- the set services/ai_service.py
        format_official_state() and the admin API's /api/v1/game-state
        render. A WEEK_SCOPED_TOPICS item whose only active value
        predates the current week's start (see current_state()) is
        excluded; every other active STATE item (including any
        WEEK_SCOPED_TOPICS item already re-taught this week) is
        included unchanged.
        """

        result = []
        for item in self.active_items():
            if item.type != KnowledgeType.STATE or not item.topic:
                continue
            if item.topic in self.WEEK_SCOPED_TOPICS:
                if self.current_state(item.topic) is None:
                    continue
            result.append(item)
        return result

    # ======================================================
    # Weekly state boundary
    # ======================================================
    #
    # Deliberately time-based, not a new field on every KnowledgeItem:
    # "which week is this STATE value for" is derived from WHEN it was
    # taught (created_at) relative to the recorded week-start boundary,
    # rather than stamping every write with an explicit week number.
    # This needed zero schema/migration changes to any already-
    # persisted KnowledgeItem, and it means current_state() above is a
    # pure read -- starting or closing a week never rewrites a single
    # existing KnowledgeItem.

    def start_new_week(
        self, week: int, *, started_at: Optional[datetime] = None
    ) -> None:
        """Begins a new reporting week: current_state() for every
        WEEK_SCOPED_TOPICS field immediately starts returning None
        (UNCONFIRMED) until each is explicitly re-taught with a
        created_at at or after `started_at` (defaults to now).

        Deliberately does not touch a single KnowledgeItem -- no STATE
        value is deleted, deactivated, or overwritten by this call.
        The previous week's values are still fully readable via
        active_state()/all_items(); they simply stop counting as
        "current" (see current_state()). Call close_week() first if
        the outgoing week's values should be preserved as a queryable
        historical snapshot (see close_week() below) -- the two are
        deliberately separate operations (matching the dashboard's own
        CLOSE WEEK / START NEW WEEK controls) so closing a week never
        silently starts a new one, and vice versa.
        """

        self.current_week = week
        self.week_started_at = started_at or datetime.now(UTC)
        self._persist_week_meta()

        logger.info(
            "Started week %d (boundary=%s).",
            week,
            self.week_started_at.isoformat(),
        )

    def close_week(self, *, closed_at: Optional[datetime] = None) -> dict:
        """Freezes the CURRENT reporting week's full STATE snapshot
        (every active STATE item, not just WEEK_SCOPED_TOPICS -- a
        historical query about REMAINING_HOUSEGUESTS or LAST_EVICTED
        for a past week should work too) into the queryable weekly
        archive (see archived_week()), keyed by the current week
        number. Raises ValueError if no week has been started yet --
        there is nothing to close.

        Does not itself start a new week or touch current_state()'s
        behavior -- call start_new_week() afterward (see that method's
        own docstring for why these stay separate operations).
        """

        if self.current_week is None:
            raise ValueError(
                "No current week is set -- call start_new_week() first."
            )

        snapshot = {
            item.topic: item.content
            for item in self.active_items()
            if item.type == KnowledgeType.STATE and item.topic
        }

        return self.set_archived_week(
            self.current_week,
            snapshot,
            started_at=self.week_started_at,
            closed_at=closed_at,
        )

    def archived_week(self, week: int) -> Optional[dict]:
        """Returns the frozen snapshot for `week` (see close_week()/
        set_archived_week()), or None if nothing has been archived for
        it. Never derived from the live current-week state -- if
        `week` is the currently-open week, it hasn't been closed yet
        and this correctly returns None; the caller should read
        current_state()/current_state_items() for the live value
        instead."""

        return self.week_archive.get(week)

    def set_archived_week(
        self,
        week: int,
        snapshot: dict,
        *,
        started_at: Optional[datetime] = None,
        closed_at: Optional[datetime] = None,
    ) -> dict:
        """Directly records (or overwrites) week `week`'s historical
        snapshot -- the primitive close_week() itself uses, also
        exposed directly so an administrator can backfill a week that
        predates week-tracking ever being turned on (e.g. recording
        what Week 7/8 actually ended with, once this feature first
        ships, so historical questions about them work immediately
        rather than only from the first week tracked live).

        Never touches current_week/week_started_at or any
        KnowledgeItem -- purely an archive write.
        """

        record = {
            "week": week,
            "started_at": started_at.isoformat() if started_at else None,
            "closed_at": (closed_at or datetime.now(UTC)).isoformat(),
            "snapshot": dict(snapshot),
        }

        self.week_archive[week] = record
        self._persist_week_meta()

        logger.info("Archived week %d snapshot: %s", week, snapshot)

        return record

    # ======================================================
    # Topic-spelling repair
    # ======================================================

    def dedupe_topics(self) -> list[str]:
        """Repairs STATE topic-spelling drift that predates
        _canonicalize_topic() -- e.g. "VETO WINNER" and "VETO_WINNER"
        independently taught as if they were different topics for the
        same real-world fact (a free-text moderator entry point --
        see production/batch_teach.py -- with no canonicalization
        before this existed). That silently broke the "at most one
        active STATE item per topic" guarantee active_state() and
        every reader of it rely on: both stayed active, and whichever
        exact spelling a caller queried decided what showed up as
        "official" -- which could be the stale one.

        Idempotent and safe to call on every startup (same posture as
        ProductionEngine.reconcile_game_state_from_knowledge()):

        1. Rewrites every STATE item's topic to its canonical spelling
           (in place -- content, author, created_at, id all untouched).
        2. For any canonical topic that now has more than one active
           item (the actual conflict this closes), keeps the most
           recently updated as active and deactivates the rest -- same
           as any other automatic supersede, never a delete.

        Returns the canonical topics that had a genuine conflict
        repaired (step 2), for the caller to log. A pure spelling
        rewrite with no resulting conflict (step 1 only) is not
        reported here -- it changed no reader-visible fact, only the
        stored spelling.
        """

        rewrote_any = False
        for item in self._items:
            if item.type != KnowledgeType.STATE or not item.topic:
                continue
            canonical = _canonicalize_topic(item.topic)
            if item.topic != canonical:
                item.topic = canonical
                rewrote_any = True

        by_topic: dict[str, list[KnowledgeItem]] = {}
        for item in self._items:
            if item.active and item.type == KnowledgeType.STATE and item.topic:
                by_topic.setdefault(item.topic, []).append(item)

        repaired: list[str] = []
        now = datetime.now(UTC)
        for topic, active_candidates in by_topic.items():
            if len(active_candidates) <= 1:
                continue
            winner = max(active_candidates, key=lambda candidate: candidate.updated_at)
            for candidate in active_candidates:
                if candidate is not winner:
                    candidate.active = False
                    candidate.updated_at = now
            repaired.append(topic)

        if rewrote_any or repaired:
            self._persist()
            logger.info(
                "Knowledge topic dedupe: canonicalized spellings=%s, "
                "conflicts repaired for topics=%s.",
                rewrote_any,
                repaired,
            )

        return repaired
