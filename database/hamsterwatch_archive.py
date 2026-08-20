"""
Julie ChenBot Hamsterwatch Archive
====================================

Persistent, queryable storage for Hamsterwatch BB28 recap content.

Owns storage and retrieval only: idempotent upserts keyed by
(page_url, section_slug), meaningful-vs-cosmetic change detection,
and the reusable retrieval layer (by recency, BB day, date, or
keyword) that /recap and future AI features read from.

This module never fetches a URL and never talks to an AI provider —
matching the fetching/parsing/storage/retrieval/AI separation used
throughout Julie's production pipeline. Callers (the Hamsterwatch
monitor, /recap) own the fetch and the AI call; this module owns
"do we already have this, did it meaningfully change, and what's
relevant to a given query."

Storage backend
----------------
A dedicated SQLite database (consistent with the existing chat
history store in services/ai_service.py — no new persistence
paradigm is introduced). A FTS5 virtual table indexes heading and
content so player/topic keyword retrieval doesn't require standing
up a vector database or other extra infrastructure.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from config import DATABASE

ARCHIVE_FILE = DATABASE / "hamsterwatch_archive.db"

# A content edit is treated as "meaningful" (and therefore
# announcement-worthy) once it changes the normalized text by at
# least this many characters, or by this fraction of the previous
# length — whichever triggers first. Below both thresholds, the
# edit is stored (so retrieval always reflects the latest text) but
# does not count as a change worth telling Discord about. Typo
# fixes and small wording tweaks live under this line; a newly
# added paragraph does not.
SIGNIFICANT_LENGTH_DELTA = 40
SIGNIFICANT_RELATIVE_DELTA = 0.05


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ==========================================================
# Records
# ==========================================================


@dataclass(slots=True)
class ArchivedArticle:
    """One stored Hamsterwatch recap section."""

    id: int
    source: str
    page_url: str
    section_slug: str
    heading: str
    article_date: str | None
    bb_day: int | None
    content: str
    summary: str
    content_hash: str
    first_seen_at: str
    last_changed_at: str
    updated_at: str


@dataclass(slots=True)
class UpsertOutcome:
    """Result of storing one parsed section."""

    article: ArchivedArticle
    is_new: bool
    significant_change: bool


# ==========================================================
# Archive
# ==========================================================


class HamsterwatchArchive:
    """Idempotent SQLite-backed store for Hamsterwatch recap content."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or ARCHIVE_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._ensure_schema(connection)

    # ------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL DEFAULT 'Hamsterwatch',
                page_url TEXT NOT NULL,
                section_slug TEXT NOT NULL,
                heading TEXT NOT NULL,
                article_date TEXT,
                bb_day INTEGER,
                content TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_changed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(page_url, section_slug)
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
                heading, content,
                content='articles', content_rowid='id'
            )
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
                INSERT INTO articles_fts(rowid, heading, content)
                VALUES (new.id, new.heading, new.content);
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
                INSERT INTO articles_fts(articles_fts, rowid, heading, content)
                VALUES ('delete', old.id, old.heading, old.content);
                INSERT INTO articles_fts(rowid, heading, content)
                VALUES (new.id, new.heading, new.content);
            END
            """
        )
        connection.commit()

    # ------------------------------------------------------
    # Writes
    # ------------------------------------------------------

    def upsert(
        self,
        *,
        page_url: str,
        section_slug: str,
        heading: str,
        article_date: str | None,
        bb_day: int | None,
        content: str,
        summary: str,
        source: str = "Hamsterwatch",
    ) -> UpsertOutcome:
        """Stores one recap section, idempotently.

        Re-importing the same (page_url, section_slug) never creates
        a duplicate row. An unchanged normalized-content hash is a
        no-op change-wise (article is still touched so updated_at
        stays current). A changed hash updates the stored content
        and reports whether the edit was significant enough to be
        announcement-worthy (see SIGNIFICANT_* thresholds above).
        """

        digest = _content_hash(content)
        now = _now()

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM articles WHERE page_url = ? AND section_slug = ?",
                (page_url, section_slug),
            ).fetchone()

            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO articles (
                        source, page_url, section_slug, heading, article_date,
                        bb_day, content, summary, content_hash,
                        first_seen_at, last_changed_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source, page_url, section_slug, heading, article_date,
                        bb_day, content, summary, digest,
                        now, now, now,
                    ),
                )
                connection.commit()
                article = self._get_by_id(connection, cursor.lastrowid)
                return UpsertOutcome(article=article, is_new=True, significant_change=True)

            existing = self._row_to_article(row)

            if existing.content_hash == digest:
                connection.execute(
                    "UPDATE articles SET updated_at = ? WHERE id = ?",
                    (now, existing.id),
                )
                connection.commit()
                existing.updated_at = now
                return UpsertOutcome(article=existing, is_new=False, significant_change=False)

            significant = self._is_significant_change(existing.content, content)
            last_changed_at = now if significant else existing.last_changed_at

            connection.execute(
                """
                UPDATE articles SET
                    heading = ?, article_date = ?, bb_day = ?, content = ?,
                    summary = ?, content_hash = ?, last_changed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    heading, article_date, bb_day, content,
                    summary, digest, last_changed_at, now,
                    existing.id,
                ),
            )
            connection.commit()
            article = self._get_by_id(connection, existing.id)
            return UpsertOutcome(article=article, is_new=False, significant_change=significant)

    @staticmethod
    def _is_significant_change(previous: str, updated: str) -> bool:
        delta = abs(len(updated) - len(previous))
        if delta >= SIGNIFICANT_LENGTH_DELTA:
            return True
        baseline = max(len(previous), 1)
        return (delta / baseline) >= SIGNIFICANT_RELATIVE_DELTA

    # ------------------------------------------------------
    # Reads
    # ------------------------------------------------------

    @staticmethod
    def _row_to_article(row: sqlite3.Row) -> ArchivedArticle:
        return ArchivedArticle(
            id=row["id"],
            source=row["source"],
            page_url=row["page_url"],
            section_slug=row["section_slug"],
            heading=row["heading"],
            article_date=row["article_date"],
            bb_day=row["bb_day"],
            content=row["content"],
            summary=row["summary"],
            content_hash=row["content_hash"],
            first_seen_at=row["first_seen_at"],
            last_changed_at=row["last_changed_at"],
            updated_at=row["updated_at"],
        )

    def _get_by_id(self, connection: sqlite3.Connection, article_id: int) -> ArchivedArticle:
        row = connection.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        return self._row_to_article(row)

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM articles").fetchone()
            return int(row["n"])

    def known_page_urls(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT DISTINCT page_url FROM articles").fetchall()
            return {row["page_url"] for row in rows}

    def latest_page_url(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT page_url FROM articles
                ORDER BY COALESCE(bb_day, -1) DESC, COALESCE(article_date, '') DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            return row["page_url"] if row else None

    def recent(self, limit: int = 5) -> list[ArchivedArticle]:
        """Returns the most recent articles, newest BB day/date first."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM articles
                ORDER BY COALESCE(bb_day, -1) DESC, COALESCE(article_date, '') DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_article(row) for row in rows]

    def by_bb_day(self, bb_day: int) -> list[ArchivedArticle]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM articles WHERE bb_day = ? ORDER BY id", (bb_day,)
            ).fetchall()
            return [self._row_to_article(row) for row in rows]

    def by_date(self, article_date: str) -> list[ArchivedArticle]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM articles WHERE article_date = ? ORDER BY id",
                (article_date,),
            ).fetchall()
            return [self._row_to_article(row) for row in rows]

    def search(self, keywords: list[str], limit: int = 5) -> list[ArchivedArticle]:
        """Keyword/player/topic search over heading+content via FTS5.

        Each keyword is matched as an independent phrase, OR'd
        together, so a query for player names finds any article
        mentioning any of them. Ranked by FTS relevance (bm25), then
        recency.
        """

        terms = [kw.strip() for kw in keywords if kw and kw.strip()]
        if not terms:
            return []

        # FTS5 phrase syntax: wrap each term in double quotes so
        # punctuation/apostrophes in names don't get parsed as query
        # operators. Embedded quotes are stripped rather than escaped
        # since a literal quote inside a phrase query has no clean
        # escape in FTS5's default tokenizer.
        phrases = [f'"{term.replace(chr(34), "")}"' for term in terms if term.replace(chr(34), "")]
        if not phrases:
            return []
        match_query = " OR ".join(phrases)

        with self._connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT articles.* FROM articles
                    JOIN articles_fts ON articles_fts.rowid = articles.id
                    WHERE articles_fts MATCH ?
                    ORDER BY bm25(articles_fts), COALESCE(articles.bb_day, -1) DESC
                    LIMIT ?
                    """,
                    (match_query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # A malformed query (e.g. a term that is only punctuation
                # after stripping) degrades to "no keyword match" rather
                # than surfacing a retrieval-layer error to callers.
                return []
            return [self._row_to_article(row) for row in rows]

    def find_relevant(
        self,
        keywords: list[str] | None = None,
        limit: int = 5,
    ) -> list[ArchivedArticle]:
        """Returns up to `limit` articles relevant to the given
        keywords (player names, topics), filling any remaining slots
        with the most recent articles.

        This is the entry point /recap (and future AI features) use
        to pull a small, targeted slice of Hamsterwatch history —
        never the whole archive.
        """

        matched = self.search(keywords or [], limit=limit)

        if len(matched) >= limit:
            return matched[:limit]

        seen_ids = {article.id for article in matched}
        for article in self.recent(limit=limit + len(matched)):
            if article.id in seen_ids:
                continue
            matched.append(article)
            seen_ids.add(article.id)
            if len(matched) >= limit:
                break

        return matched[:limit]
