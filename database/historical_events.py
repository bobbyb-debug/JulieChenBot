"""
Julie ChenBot Historical Event Store
========================================

Structured, administrator-verified Big Brother game history -- HOH
winners by game cycle, to start (see the module docstring in
production/historical_retrieval.py for how this is queried, and
services/ai_service.py format_historical_events() for how a result
reaches the model).

Deliberately NOT KnowledgeStore: KnowledgeType has no concept of a
season, a game cycle, or a per-player role, and forcing "Cycle 6 HOH
was Player X" into a FACT string would make it unqueryable prose --
see docs/history/ for the full architecture research this module
implements Phase 1 of. Deliberately NOT HamsterwatchArchive either:
that module stays a source-prose archive; this one holds verified,
structured records extracted from (but never merged with) sources
like it.

Storage backend
----------------
A dedicated SQLite database, matching the exact pattern already used
for HamsterwatchArchive and the chat-history store: real indexing,
real querying, no new persistence paradigm introduced. Never uses the
Storage/JSON-blob abstraction (database/storage.py) -- that mechanism
rewrites its entire file on every write and has no query capability,
which is exactly why HamsterwatchArchive already lives outside it and
why this module does too.

Game-cycle identity
--------------------
The central design decision of this module: a game cycle (one HOH
reign) is identified by (season, cycle_sequence_number) -- NOT
(season, week_number). Big Brother runs double- and triple-eviction
episodes, where two or three full HOH reigns occur within the same
calendar week; (season, week_number) cannot tell those cycles apart,
but (season, cycle_sequence_number) always can, because the sequence
number is an explicit, administrator-asserted "this was the Nth HOH
reign of the season" -- never auto-incremented from insertion order,
which would silently break the moment a cycle is backfilled out of
chronological entry order. week_number is retained purely as
descriptive, non-unique display metadata.

Verification model
--------------------
Every historical_event row starts UNVERIFIED. Only an explicit
administrator action (verify_hoh()/correct_hoh() below) can ever set
a row to ADMIN_VERIFIED -- there is no automatic promotion on insert,
on a second source agreeing, or on any AI involvement (there is none
in this module at all). A CROSS_CHECKED status is representable in
the verification_status vocabulary for future use but nothing in this
module ever sets or reads it -- Phase 1 has no automated cross-source
comparison to trigger it, so exposing it as a real workflow now would
be unearned complexity.

Multiple UNVERIFIED rows may coexist for the same (cycle_id,
event_type) -- this is how a source disagreement is represented (see
reject_hoh()). Only one row may ever be ADMIN_VERIFIED and active for
a given (cycle_id, event_type) at a time; this is enforced by a
partial SQLite unique index, not merely application discipline, so a
bug cannot silently create two "current" answers for one cycle.

Corrections never mutate a verified row in place. correct_hoh()
creates a new row (immediately ADMIN_VERIFIED), points its
`supersedes` field at the old row's id -- the same forward-only
reference already used by production/knowledge.py KnowledgeItem.
supersedes, reused rather than reinvented -- and flips the old row's
status to CORRECTED. Nothing is ever deleted; the full history of
what Julie believed, and why it changed, stays reconstructable by
walking the supersedes chain.

Read paths used by chat/prompt retrieval (see
production/historical_retrieval.py) only ever return ADMIN_VERIFIED,
active rows -- there is no parameter that lets an unverified or
rejected record reach that path. Reviewing everything (any status),
for administrator use only, goes through the separate
find_hoh_candidates() method.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from config import DATABASE

HISTORICAL_EVENTS_FILE = DATABASE / "historical_events.db"

# Kept small and closed for Phase 1 (HOH only), but the *table* does
# not enforce this as a DB-level CHECK constraint -- see
# _ensure_schema()'s own comment for why. This tuple is the
# application-level guard instead.
SUPPORTED_EVENT_TYPES = ("HOH",)

# CROSS_CHECKED is representable but never produced or consumed by
# this module in Phase 1 (see module docstring). REJECTED is a
# candidate that lost a conflict round and was never accepted.
# CORRECTED is a record that *was* ADMIN_VERIFIED and has since been
# superseded by a newer verified record -- a different situation from
# REJECTED, kept as a different value so the two are never confused
# when reconstructing history.
VERIFICATION_STATUSES = (
    "UNVERIFIED",
    "CROSS_CHECKED",
    "ADMIN_VERIFIED",
    "REJECTED",
    "CORRECTED",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_player(name: str) -> str:
    """Same normalization discipline already used for KnowledgeStore
    STATE topics (production/knowledge.py teach()) -- strip and
    uppercase. Deliberately NOT fuzzy: two different strings are never
    silently treated as the same houseguest by this function or
    anything that calls it."""

    return name.strip().upper()


class DuplicateCycleError(ValueError):
    """Raised when create_cycle() is asked to create a cycle whose
    (season, cycle_sequence_number) already exists -- see that
    method's docstring for why this is a distinct path from the
    get-or-create convenience record_hoh_claim() uses internally."""


class CycleNotFoundError(ValueError):
    pass


class EventNotFoundError(ValueError):
    pass


class AlreadyVerifiedError(ValueError):
    """Raised by verify_hoh() when a different row is already the
    ADMIN_VERIFIED record for this (cycle_id, event_type) -- this is
    the partial-unique-index invariant surfacing as a catchable
    Python exception rather than a raw sqlite3.IntegrityError, so a
    caller (the /teach historical-hoh command) can present it as "this
    looks like a correction" rather than a crash."""


# ==========================================================
# Records
# ==========================================================


@dataclass(slots=True)
class GameCycle:
    """One chronological Big Brother HOH reign. Identity is
    (season, cycle_sequence_number) -- see this module's docstring."""

    id: int
    season: int
    cycle_sequence_number: int
    week_number: Optional[int]
    created_at: str
    updated_at: str


@dataclass(slots=True)
class HistoricalEvent:
    """One structured historical fact attached to a game cycle.
    Phase 1 only ever creates event_type == "HOH" rows."""

    id: int
    cycle_id: int
    event_type: str
    verification_status: str
    source_type: str
    source_ref: str
    excerpt: Optional[str]
    bb_day: Optional[int]
    author_id: Optional[int]
    supersedes: Optional[int]
    active: bool
    created_at: str
    updated_at: str
    # Populated by read methods that also fetch participants (the HOH
    # winner, for Phase 1) -- not a stored column.
    participants: tuple["EventParticipant", ...] = ()
    # Denormalized display fields, populated by read methods via a
    # join back to game_cycle -- not stored columns either. Exists so
    # a caller (production/historical_retrieval.py, services/
    # ai_service.py format_historical_events()) can label an event by
    # its meaningful (season, cycle_sequence_number, week_number)
    # rather than the opaque internal cycle_id foreign key.
    cycle_season: Optional[int] = None
    cycle_sequence_number: Optional[int] = None
    cycle_week_number: Optional[int] = None


@dataclass(slots=True)
class EventParticipant:
    id: int
    event_id: int
    houseguest: str
    role: str


# ==========================================================
# Store
# ==========================================================


class HistoricalEventStore:
    """SQLite-backed store for structured, administrator-verified
    historical game events. See module docstring for the full
    architecture this implements."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or HISTORICAL_EVENTS_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._ensure_schema(connection)

    # ------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS game_cycle (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season INTEGER NOT NULL,
                cycle_sequence_number INTEGER NOT NULL,
                week_number INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(season, cycle_sequence_number)
            )
            """
        )
        # event_type and event_participant.role are deliberately plain
        # TEXT, not a DB-level CHECK/enum: SQLite cannot widen a CHECK
        # constraint's allowed set without rebuilding the table, which
        # would make adding NOMINATION/VETO/EVICTION in a later phase
        # require exactly the kind of migration this whole design
        # exists to avoid. SUPPORTED_EVENT_TYPES above is the Phase 1
        # guard instead, enforced in Python at the one function that
        # ever inserts a row.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS historical_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL REFERENCES game_cycle(id),
                event_type TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                excerpt TEXT,
                bb_day INTEGER,
                author_id INTEGER,
                supersedes INTEGER REFERENCES historical_event(id),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # The one DB-enforced trust-boundary invariant: at most one
        # ADMIN_VERIFIED, active row per (cycle_id, event_type). A
        # correction must flip the old row's status away from
        # ADMIN_VERIFIED in the same transaction that verifies the
        # new one, or this index rejects the write outright -- see
        # verify_hoh()/correct_hoh().
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_historical_event_one_verified
            ON historical_event(cycle_id, event_type)
            WHERE verification_status = 'ADMIN_VERIFIED' AND active = 1
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS event_participant (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES historical_event(id),
                houseguest TEXT NOT NULL,
                role TEXT NOT NULL,
                UNIQUE(event_id, houseguest, role)
            )
            """
        )
        connection.commit()

    # ------------------------------------------------------
    # Row <-> dataclass
    # ------------------------------------------------------

    @staticmethod
    def _row_to_cycle(row: sqlite3.Row) -> GameCycle:
        return GameCycle(
            id=row["id"],
            season=row["season"],
            cycle_sequence_number=row["cycle_sequence_number"],
            week_number=row["week_number"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> HistoricalEvent:
        return HistoricalEvent(
            id=row["id"],
            cycle_id=row["cycle_id"],
            event_type=row["event_type"],
            verification_status=row["verification_status"],
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            excerpt=row["excerpt"],
            bb_day=row["bb_day"],
            author_id=row["author_id"],
            supersedes=row["supersedes"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_participant(row: sqlite3.Row) -> EventParticipant:
        return EventParticipant(
            id=row["id"],
            event_id=row["event_id"],
            houseguest=row["houseguest"],
            role=row["role"],
        )

    def _participants_for(
        self, connection: sqlite3.Connection, event_id: int
    ) -> tuple[EventParticipant, ...]:
        rows = connection.execute(
            "SELECT * FROM event_participant WHERE event_id = ? ORDER BY id",
            (event_id,),
        ).fetchall()
        return tuple(self._row_to_participant(row) for row in rows)

    def _get_event(
        self, connection: sqlite3.Connection, event_id: int
    ) -> HistoricalEvent:
        row = connection.execute(
            "SELECT * FROM historical_event WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise EventNotFoundError(f"No historical_event #{event_id}.")
        return self._hydrate(connection, self._row_to_event(row))

    # ------------------------------------------------------
    # Cycles
    # ------------------------------------------------------

    def create_cycle(
        self,
        *,
        season: int,
        cycle_sequence_number: int,
        week_number: Optional[int] = None,
    ) -> GameCycle:
        """Creates a new game cycle. Raises DuplicateCycleError if
        (season, cycle_sequence_number) already exists -- this is the
        explicit, safe rejection path for a genuine identity collision
        (see record_hoh_claim() for the get-or-create convenience path
        used when the intent is simply "record this cycle's HOH,
        creating the cycle if needed")."""

        now = _now()
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO game_cycle
                        (season, cycle_sequence_number, week_number, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (season, cycle_sequence_number, week_number, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateCycleError(
                    f"Season {season} cycle {cycle_sequence_number} already exists."
                ) from exc
            connection.commit()
            row = connection.execute(
                "SELECT * FROM game_cycle WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_cycle(row)

    def get_cycle(
        self, *, season: int, cycle_sequence_number: int
    ) -> Optional[GameCycle]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM game_cycle WHERE season = ? AND cycle_sequence_number = ?",
                (season, cycle_sequence_number),
            ).fetchone()
            return self._row_to_cycle(row) if row else None

    def cycles_for_week(self, *, season: int, week_number: int) -> list[GameCycle]:
        """Every cycle sharing this week number, ordered by sequence
        -- 1 for a normal week, 2+ for a double/triple eviction. Never
        picks one; see production/historical_retrieval.py for how a
        caller decides what to do with more than one match."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM game_cycle
                WHERE season = ? AND week_number = ?
                ORDER BY cycle_sequence_number
                """,
                (season, week_number),
            ).fetchall()
            return [self._row_to_cycle(row) for row in rows]

    def known_seasons(self) -> list[int]:
        """Every season with at least one cycle recorded. Used by
        retrieval to decide whether "which season" is actually
        ambiguous (see production/historical_retrieval.py) -- Phase 1
        deliberately has no CURRENT_SEASON concept (see module
        docstring), so this is the only season signal retrieval has."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT season FROM game_cycle ORDER BY season"
            ).fetchall()
            return [row["season"] for row in rows]

    def set_week_number(
        self, *, season: int, cycle_sequence_number: int, week_number: Optional[int]
    ) -> GameCycle:
        """Corrects a cycle's descriptive week label. A plain,
        immediate update -- week_number is metadata, not a verified
        fact, so this deliberately does NOT go through the
        supersede/correction machinery reserved for historical_event
        rows (see module docstring: "Cycle 6 HOH was Player B" needs
        supersession; "this was actually Week 6, not Week 5" does
        not)."""

        cycle = self.get_cycle(season=season, cycle_sequence_number=cycle_sequence_number)
        if cycle is None:
            raise CycleNotFoundError(
                f"Season {season} cycle {cycle_sequence_number} does not exist."
            )
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE game_cycle SET week_number = ?, updated_at = ? WHERE id = ?",
                (week_number, now, cycle.id),
            )
            connection.commit()
        return GameCycle(
            id=cycle.id, season=cycle.season,
            cycle_sequence_number=cycle.cycle_sequence_number,
            week_number=week_number, created_at=cycle.created_at, updated_at=now,
        )

    # ------------------------------------------------------
    # HOH events -- writes
    # ------------------------------------------------------

    def record_hoh_claim(
        self,
        *,
        season: int,
        cycle_sequence_number: int,
        winner: str,
        source_type: str,
        source_ref: str,
        week_number: Optional[int] = None,
        excerpt: Optional[str] = None,
        bb_day: Optional[int] = None,
        author_id: Optional[int] = None,
    ) -> HistoricalEvent:
        """Records a new, UNVERIFIED HOH claim for a cycle, creating
        the cycle if it doesn't exist yet (get-or-create -- unlike
        create_cycle(), this is the convenience path for "just record
        this," not a strict identity-collision check). Never sets
        verification_status to anything but UNVERIFIED -- see
        verify_hoh() for the separate, explicit promotion step.

        Multiple UNVERIFIED claims can coexist for the same cycle --
        this is intentional (see reject_hoh()) and is how a source
        disagreement gets represented rather than resolved by
        insertion order.
        """

        cycle = self.get_cycle(season=season, cycle_sequence_number=cycle_sequence_number)
        if cycle is None:
            cycle = self.create_cycle(
                season=season,
                cycle_sequence_number=cycle_sequence_number,
                week_number=week_number,
            )
        elif week_number is not None and cycle.week_number != week_number:
            cycle = self.set_week_number(
                season=season, cycle_sequence_number=cycle_sequence_number,
                week_number=week_number,
            )

        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO historical_event
                    (cycle_id, event_type, verification_status, source_type,
                     source_ref, excerpt, bb_day, author_id, supersedes, active,
                     created_at, updated_at)
                VALUES (?, 'HOH', 'UNVERIFIED', ?, ?, ?, ?, ?, NULL, 1, ?, ?)
                """,
                (cycle.id, source_type, source_ref, excerpt, bb_day, author_id, now, now),
            )
            event_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO event_participant (event_id, houseguest, role) VALUES (?, ?, 'WINNER')",
                (event_id, _normalize_player(winner)),
            )
            connection.commit()
            return self._get_event(connection, event_id)

    def verify_hoh(self, event_id: int, *, author_id: Optional[int] = None) -> HistoricalEvent:
        """Explicitly promotes an UNVERIFIED (or REJECTED-and-
        reconsidered) claim to ADMIN_VERIFIED. Raises
        AlreadyVerifiedError if a *different* row already holds
        ADMIN_VERIFIED for this cycle+event_type -- this method never
        auto-supersedes anything; that only ever happens through
        correct_hoh()'s explicit, single-transaction swap. This is
        the concrete guard against Decision 3 (supersession is
        admin-triggered only, never automatic on write, insertion
        order, or a second source appearing).
        """

        now = _now()
        with self._connect() as connection:
            event = self._get_event(connection, event_id)
            try:
                connection.execute(
                    "UPDATE historical_event SET verification_status = 'ADMIN_VERIFIED', "
                    "author_id = COALESCE(?, author_id), updated_at = ? WHERE id = ?",
                    (author_id, now, event_id),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise AlreadyVerifiedError(
                    f"Cycle {event.cycle_id} already has a verified {event.event_type} "
                    "record -- use correct_hoh() to explicitly supersede it."
                ) from exc
            return self._get_event(connection, event_id)

    def reject_hoh(self, event_id: int, *, author_id: Optional[int] = None) -> HistoricalEvent:
        """Marks a competing, never-accepted claim REJECTED -- the
        losing side of a conflict an administrator resolved by
        verifying a different claim instead. Distinct from
        CORRECTED (§ module docstring): REJECTED means "was never
        accepted"; CORRECTED means "was accepted, later replaced."
        The row is never deleted."""

        now = _now()
        with self._connect() as connection:
            self._get_event(connection, event_id)
            connection.execute(
                "UPDATE historical_event SET verification_status = 'REJECTED', "
                "author_id = COALESCE(?, author_id), updated_at = ? WHERE id = ?",
                (author_id, now, event_id),
            )
            connection.commit()
            return self._get_event(connection, event_id)

    def correct_hoh(
        self,
        *,
        old_event_id: int,
        winner: str,
        source_type: str,
        source_ref: str,
        excerpt: Optional[str] = None,
        bb_day: Optional[int] = None,
        author_id: Optional[int] = None,
    ) -> HistoricalEvent:
        """The ONLY path that supersedes an already-ADMIN_VERIFIED
        record. Creates a new row, immediately ADMIN_VERIFIED, with
        `supersedes` pointing at old_event_id; in the same
        transaction, flips the old row's status to CORRECTED (it
        stays `active=1` -- never deleted, fully traceable). Mirrors
        production/knowledge.py KnowledgeStore.teach()'s exact
        "append the new item, deactivate the superseded one, one
        commit" pattern.
        """

        now = _now()
        with self._connect() as connection:
            old_event = self._get_event(connection, old_event_id)
            if old_event.verification_status != "ADMIN_VERIFIED":
                raise ValueError(
                    f"historical_event #{old_event_id} is not currently "
                    "ADMIN_VERIFIED -- nothing to correct."
                )

            # The old row must stop being ADMIN_VERIFIED BEFORE the new
            # row can be inserted as ADMIN_VERIFIED -- the partial
            # unique index enforces this ordering itself (inserting
            # the new row first would collide with the still-verified
            # old one). Both writes are one transaction, so a failure
            # partway through can never leave two verified rows, or
            # zero, for this cycle.
            connection.execute(
                "UPDATE historical_event SET verification_status = 'CORRECTED', "
                "updated_at = ? WHERE id = ?",
                (now, old_event_id),
            )

            cursor = connection.execute(
                """
                INSERT INTO historical_event
                    (cycle_id, event_type, verification_status, source_type,
                     source_ref, excerpt, bb_day, author_id, supersedes, active,
                     created_at, updated_at)
                VALUES (?, ?, 'ADMIN_VERIFIED', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    old_event.cycle_id, old_event.event_type, source_type, source_ref,
                    excerpt, bb_day, author_id, old_event_id, now, now,
                ),
            )
            new_event_id = cursor.lastrowid

            connection.execute(
                "INSERT INTO event_participant (event_id, houseguest, role) VALUES (?, ?, 'WINNER')",
                (new_event_id, _normalize_player(winner)),
            )
            connection.commit()
            return self._get_event(connection, new_event_id)

    # ------------------------------------------------------
    # HOH events -- reads for Julie (verified + active ONLY)
    # ------------------------------------------------------

    def _cycle_by_id(self, connection: sqlite3.Connection, cycle_id: int) -> Optional[GameCycle]:
        row = connection.execute(
            "SELECT * FROM game_cycle WHERE id = ?", (cycle_id,)
        ).fetchone()
        return self._row_to_cycle(row) if row else None

    def _hydrate(self, connection: sqlite3.Connection, event: HistoricalEvent) -> HistoricalEvent:
        """Attaches participants and denormalized cycle display
        fields to a freshly-read event -- see HistoricalEvent's own
        field comments for why these aren't stored columns."""

        event.participants = self._participants_for(connection, event.id)
        cycle = self._cycle_by_id(connection, event.cycle_id)
        if cycle is not None:
            event.cycle_season = cycle.season
            event.cycle_sequence_number = cycle.cycle_sequence_number
            event.cycle_week_number = cycle.week_number
        return event

    def _verified_events_for_cycle_ids(
        self, connection: sqlite3.Connection, cycle_ids: list[int]
    ) -> list[HistoricalEvent]:
        if not cycle_ids:
            return []
        placeholders = ",".join("?" for _ in cycle_ids)
        rows = connection.execute(
            f"""
            SELECT * FROM historical_event
            WHERE cycle_id IN ({placeholders})
              AND event_type = 'HOH'
              AND verification_status = 'ADMIN_VERIFIED'
              AND active = 1
            ORDER BY cycle_id
            """,
            cycle_ids,
        ).fetchall()
        events = [self._row_to_event(row) for row in rows]
        return [self._hydrate(connection, event) for event in events]

    def verified_hoh_for_cycle(
        self, *, season: int, cycle_sequence_number: int
    ) -> Optional[HistoricalEvent]:
        """The single verified HOH record for one exact cycle, or
        None. Never returns an unverified or rejected/corrected
        record -- this is a read path Julie's prompt can safely use
        directly."""

        cycle = self.get_cycle(season=season, cycle_sequence_number=cycle_sequence_number)
        if cycle is None:
            return None
        with self._connect() as connection:
            events = self._verified_events_for_cycle_ids(connection, [cycle.id])
            return events[0] if events else None

    def verified_hoh_for_week(self, *, season: int, week_number: int) -> list[HistoricalEvent]:
        """Every verified HOH record for cycles sharing this week --
        0, 1 (normal week), or 2+ (double/triple eviction). Deliberately
        returns the full list rather than picking one; see
        production/historical_retrieval.py for how a caller presents
        more than one result without guessing."""

        cycles = self.cycles_for_week(season=season, week_number=week_number)
        with self._connect() as connection:
            return self._verified_events_for_cycle_ids(
                connection, [c.id for c in cycles]
            )

    def verified_hoh_for_player(self, houseguest: str) -> list[HistoricalEvent]:
        """Every verified cycle where this exact (normalized) player
        name won HOH -- used for "what happened during Taylor's HOH?"
        style, player-anchored questions. Exact match only, on names
        already recorded via record_hoh_claim() -- never fuzzy (see
        _normalize_player())."""

        normalized = _normalize_player(houseguest)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT he.* FROM historical_event he
                JOIN event_participant ep ON ep.event_id = he.id
                WHERE he.event_type = 'HOH'
                  AND he.verification_status = 'ADMIN_VERIFIED'
                  AND he.active = 1
                  AND ep.houseguest = ? AND ep.role = 'WINNER'
                ORDER BY he.cycle_id
                """,
                (normalized,),
            ).fetchall()
            events = [self._row_to_event(row) for row in rows]
            return [self._hydrate(connection, event) for event in events]

    def known_hoh_winners(self) -> list[str]:
        """Every distinct, normalized player name that has ever won a
        verified HOH -- used by retrieval to safely detect "which
        player is this question about" without fuzzy matching (see
        production/historical_retrieval.py) -- only ever names players
        Julie already has real recorded, verified data for."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ep.houseguest FROM event_participant ep
                JOIN historical_event he ON he.id = ep.event_id
                WHERE he.event_type = 'HOH' AND he.verification_status = 'ADMIN_VERIFIED'
                  AND he.active = 1 AND ep.role = 'WINNER'
                """
            ).fetchall()
            return [row["houseguest"] for row in rows]

    # ------------------------------------------------------
    # Reads for administrator review ONLY -- never call these from
    # the chat/prompt path. Includes unverified/rejected/corrected
    # rows, which Julie must never see (Decision 2).
    # ------------------------------------------------------

    def find_hoh_candidates(
        self, *, season: int, cycle_sequence_number: int
    ) -> list[HistoricalEvent]:
        """Every historical_event row (any status) for one cycle's
        HOH slot -- for admin-facing duplicate/conflict review only.
        Deliberately not used anywhere in the retrieval path Julie's
        prompt is built from."""

        cycle = self.get_cycle(season=season, cycle_sequence_number=cycle_sequence_number)
        if cycle is None:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM historical_event
                WHERE cycle_id = ? AND event_type = 'HOH'
                ORDER BY created_at
                """,
                (cycle.id,),
            ).fetchall()
            events = [self._row_to_event(row) for row in rows]
            return [self._hydrate(connection, event) for event in events]
