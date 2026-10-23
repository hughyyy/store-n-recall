"""SQLite storage for facts, archives, and provenance.

Design invariants (see README.md / CONTEXT.md):
- Facts are immutable and never hard-deleted. There is no DELETE or UPDATE of
  fact content anywhere in this module; the only table mutation beyond INSERT
  is setting `archived_at` on archive/supersede. The FTS mirror has an insert
  trigger only, mirroring the guarantee.
- The archive is a visibility flag: archived rows stay forever and are reachable
  only through the scour path.
- One store per machine, shared by processes; WAL + busy timeout make concurrent
  writers safe. Connections are short-lived (one per operation), created on
  demand so the server starts with no store file present.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from .config import FactError

KINDS = ("fact", "decision", "gotcha", "convention")
DEFAULT_CONFIDENCE = 0.5
DEFAULT_LIMIT = 50
BUSY_TIMEOUT_MS = 5000
TOP_TAGS_LIMIT = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    tags        TEXT    NOT NULL,  -- JSON array, sorted + deduped
    confidence  REAL    NOT NULL,
    created_at  TEXT    NOT NULL,  -- ISO-8601 UTC
    archived_at TEXT              -- NULL while active
);
CREATE INDEX IF NOT EXISTS idx_facts_scope
    ON facts(project, archived_at, created_at DESC);
CREATE TABLE IF NOT EXISTS sources (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER NOT NULL REFERENCES facts(id),
    action  TEXT    NOT NULL,      -- 'stored' | 'archived'
    agent   TEXT    NOT NULL,
    session TEXT,
    at      TEXT    NOT NULL,
    note    TEXT,
    pointer TEXT
);
CREATE INDEX IF NOT EXISTS idx_sources_fact ON sources(fact_id);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(text, content='facts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS facts_fts_insert AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

_RECALL_COLUMNS = (
    "facts.id, facts.project, facts.kind, facts.text, facts.tags,"
    " facts.confidence, facts.created_at, facts.archived_at"
)
_ROW_KEYS = ("id", "project", "kind", "text", "tags", "confidence", "created_at", "archived_at")


def utc_now() -> str:
    """Millisecond-precision ISO-8601 UTC timestamp (lexicographic == chronological)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Argument normalization (validated before any write/read; errors are FactError
# with a reason the calling model can act on).
# ---------------------------------------------------------------------------

def _project(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactError("project is required and must be a non-empty string")
    return value.strip()


def _text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactError("fact is required and must be a non-empty string")
    return value.strip()


def _kind(value: str | None) -> str:
    if not isinstance(value, str):
        if value is None:
            value = "fact"
        else:
            raise FactError(
                f"unknown kind {value!r}: choose one of {', '.join(KINDS)}"
            )
    kind = value.strip().lower()
    if kind not in KINDS:
        raise FactError(
            f"unknown kind {value!r}: choose one of {', '.join(KINDS)}"
        )
    return kind


def _tags(value: Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raise FactError("tags must be a list of strings")
    out: list[str] = []
    for tag in value:
        if not isinstance(tag, str):
            raise FactError(f"tag {tag!r} must be a string")
        tag = tag.strip()
        if not tag:
            raise FactError("tags cannot contain empty strings")
        if tag not in out:
            out.append(tag)
    return sorted(out)


def _confidence(value: float | int | str | None, default: float = DEFAULT_CONFIDENCE) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise FactError(f"confidence must be a number between 0.0 and 1.0, got {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise FactError(f"confidence must be a number between 0.0 and 1.0, got {value!r}")
    if num != num or num in (float("inf"), float("-inf")):
        raise FactError(f"confidence must be a number between 0.0 and 1.0, got {value!r}")
    if not 0.0 <= num <= 1.0:
        raise FactError(f"confidence must be between 0.0 and 1.0, got {value!r}")
    return num


def _fact_id(value: int | str | None) -> int:
    if isinstance(value, bool):
        raise FactError(f"id must be a positive integer, got {value!r}")
    try:
        fid = int(value)
    except (TypeError, ValueError):
        raise FactError(f"id must be a positive integer, got {value!r}")
    if fid < 1:
        raise FactError(f"id must be a positive integer, got {value!r}")
    return fid


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FactError(f"limit must be a positive integer, got {value!r}")
    return value


def _query(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FactError(f"query must be a string, got {value!r}")
    q = value.strip()
    return q or None


def _fts_terms(query: str) -> list[str]:
    """Split a free-text query into FTS5 phrase tokens.

    Only word characters survive, so FTS syntax (quotes, parens, NOT/AND) and
    punctuation in real queries are inert — a search for `"alpha"` or `100%`
    matches the words, not the punctuation. A query with no word characters
    produces no terms and therefore matches nothing.
    """
    return [f'"{t}"' for t in re.findall(r"\w+", query)]


def _like_pattern(query: str) -> str:
    """Escape a literal query for LIKE so % and _ in facts match literally."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """Fact storage. Thread-safe only when each thread opens its own Store
    instance (sqlite3 connections are not shared across threads); concurrent
    processes each using their own store on the same file are safe via WAL."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        default_agent: str = "unknown",
        use_fts: bool = True,
    ) -> None:
        self.path = os.fspath(path)
        self.default_agent = default_agent
        self.fts = use_fts and _fts_available()
        # Deliberately touch nothing on disk here: the store file is created on
        # demand at the first connection, so a server can start and register
        # tools with no store file present.

    # -- lifecycle ----------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # Idempotent and race-safe (IF NOT EXISTS): concurrent first-use by
            # several processes serializes on the SQLite lock.
            conn.executescript(_SCHEMA)
            if self.fts:
                conn.executescript(_FTS_SCHEMA)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Write transaction: BEGIN IMMEDIATE so concurrent writers wait at the
        lock instead of failing with SQLITE_BUSY mid-transaction."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # -- fact records -------------------------------------------------------

    def _record(self, row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for key in _ROW_KEYS:
            value = row[key]
            if key == "tags":
                value = json.loads(value)
            if key == "archived_at" and value is None:
                continue  # present once archived, per the fact model
            record[key] = value
        record["sources"] = [dict(s) for s in self._sources(conn, row["id"])]
        for entry in record["sources"]:
            for k in ("session", "note", "pointer"):
                if entry[k] is None:
                    del entry[k]
        return record

    def _sources(self, conn: sqlite3.Connection, fact_id: int) -> sqlite3.Cursor:
        return conn.execute(
            "SELECT agent, session, at, action, note, pointer FROM sources"
            " WHERE fact_id = ? ORDER BY id",
            (fact_id,),
        )

    # -- store --------------------------------------------------------------

    def store_fact(
        self,
        project: str | None,
        fact: str,
        *,
        kind: str | None = None,
        tags: Sequence[str] | None = None,
        confidence: float | int | str | None = None,
        source: str | None = None,
        supersedes: int | str | None = None,
        agent: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Store one fact; with `supersedes`, archive the old fact in the same act."""
        if source is not None and (not isinstance(source, str) or not source.strip()):
            raise FactError("source must be a non-empty string")
        project = _project(project)
        text = _text(fact)
        kind = _kind(kind)
        tags = _tags(tags)
        confidence = _confidence(confidence)
        agent = agent or self.default_agent
        now = utc_now()

        target_id: int | None = None
        if supersedes is not None:
            target_id = _fact_id(supersedes)

        with self._write() as conn:
            if target_id is not None:
                old = conn.execute(
                    "SELECT id, project, archived_at FROM facts WHERE id = ?",
                    (target_id,),
                ).fetchone()
                if old is None:
                    raise FactError(f"cannot supersede: no fact with id {target_id}")
                if old["archived_at"] is not None:
                    raise FactError(f"cannot supersede: fact {target_id} is already archived")
                if old["project"] != project:
                    raise FactError(
                        f"cannot supersede: fact {target_id} belongs to project "
                        f"{old['project']!r}, not {project!r}"
                    )

            cur = conn.execute(
                "INSERT INTO facts (project, kind, text, tags, confidence, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (project, kind, text, json.dumps(tags), confidence, now),
            )
            new_id = cur.lastrowid
            conn.execute(
                "INSERT INTO sources (fact_id, action, agent, session, at, note, pointer)"
                " VALUES (?, 'stored', ?, ?, ?, NULL, ?)",
                (new_id, agent, session, now, source),
            )
            if target_id is not None:
                conn.execute(
                    "UPDATE facts SET archived_at = ? WHERE id = ?",
                    (now, target_id),
                )
                conn.execute(
                    "INSERT INTO sources (fact_id, action, agent, session, at, note, pointer)"
                    " VALUES (?, 'archived', ?, ?, ?, NULL, NULL)",
                    (target_id, agent, session, now),
                )

        return self.get_fact(new_id)

    def get_fact(self, fact_id: int) -> dict[str, Any]:
        """Full record (incl. sources) for any fact, active or archived."""
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT {_RECALL_COLUMNS} FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                raise FactError(f"no fact with id {fact_id}")
            return self._record(row, conn)

    # -- recall / scour -----------------------------------------------------

    def recall_facts(
        self,
        project: str | None,
        *,
        fact_id: int | str | None = None,
        query: str | None = None,
        kind: str | None = None,
        tags: Sequence[str] | None = None,
        min_confidence: float | int | str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Active facts, relevance-ranked when `query` given (FTS5; LIKE
        fallback is newest-first), newest-first otherwise."""
        project = _project(project)
        if fact_id is not None:
            fact_id = _fact_id(fact_id)
        kind = _kind(kind) if kind else None
        tags = _tags(tags)
        limit = _limit(limit)
        min_confidence = _confidence(min_confidence) if min_confidence is not None else None
        query = _query(query)

        facts = self._select(project, kind=kind, min_confidence=min_confidence,
                             active=True, fact_id=fact_id, query=query)
        facts = [f for f in facts if set(f["tags"]) >= set(tags)]
        if fact_id is not None:
            # `id` pins the record: filters may narrow it, but a missing or
            # archived target is an error, not an empty list.
            probe = self._select(project, kind=None, min_confidence=None,
                                 active=True, fact_id=fact_id, query=None)
            if not probe:
                raise FactError(f"no active fact with id {fact_id}")
        if query is not None and not self.fts:
            facts = sorted(facts, key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return facts[:limit]

    def scour_archives(
        self,
        project: str | None,
        *,
        fact_id: int | str | None = None,
        query: str | None = None,
        kind: str | None = None,
        tags: Sequence[str] | None = None,
        min_confidence: float | int | str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Archived facts only, most recently archived first."""
        project = _project(project)
        if fact_id is not None:
            fact_id = _fact_id(fact_id)
        kind = _kind(kind) if kind else None
        tags = _tags(tags)
        limit = _limit(limit)
        min_confidence = _confidence(min_confidence) if min_confidence is not None else None
        query = _query(query)

        facts = self._select(project, kind=kind, min_confidence=min_confidence,
                             active=False, fact_id=fact_id, query=query)
        facts = [f for f in facts if set(f["tags"]) >= set(tags)]
        if fact_id is not None:
            # `id` pins the record: filters may narrow it, but a target that is
            # not archived here is an error, not an empty list.
            probe = self._select(project, kind=None, min_confidence=None,
                                 active=False, fact_id=fact_id, query=None)
            if not probe:
                raise FactError(f"no archived fact with id {fact_id}")
        if query is not None and not self.fts:
            facts = sorted(facts, key=lambda r: (r["archived_at"], r["id"]), reverse=True)
        return facts[:limit]

    def _select(
        self,
        project: str,
        *,
        kind: str | None,
        min_confidence: float | None,
        active: bool,
        fact_id: int | None,
        query: str | None,
    ) -> list[dict[str, Any]]:
        if query is not None and self.fts and not _fts_terms(query):
            # FTS can't express an all-punctuation query as a match; it matches
            # nothing rather than everything.
            return []
        clauses = ["project = ?"]
        params: list[Any] = [project]
        if active:
            clauses.append("archived_at IS NULL")
        else:
            clauses.append("archived_at IS NOT NULL")
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if min_confidence is not None:
            clauses.append("confidence >= ?")
            params.append(min_confidence)
        if fact_id is not None:
            clauses.append("id = ?")
            params.append(fact_id)

        order = "created_at DESC, id DESC" if active else "archived_at DESC, id DESC"

        if query is not None and self.fts:
            match = " ".join(_fts_terms(query))
            sql = (
                f"SELECT {_RECALL_COLUMNS} FROM facts"
                " JOIN facts_fts ON facts.id = facts_fts.rowid"
                f" WHERE {' AND '.join(clauses)} AND facts_fts MATCH ?"
                " ORDER BY rank, id DESC"
            )
            params.append(match)
        elif query is not None:
            sql = (
                f"SELECT {_RECALL_COLUMNS} FROM facts"
                f" WHERE {' AND '.join(clauses)} AND text LIKE ? ESCAPE '\\'"
                f" ORDER BY {order}"
            )
            params.append(_like_pattern(query))
        else:
            sql = (
                f"SELECT {_RECALL_COLUMNS} FROM facts"
                f" WHERE {' AND '.join(clauses)} ORDER BY {order}"
            )

        with self._conn() as conn:
            rows = []
            for r in conn.execute(sql, params):
                record = dict(r)
                record["tags"] = json.loads(record["tags"])
                if record["archived_at"] is None:
                    # present once archived, per the fact model
                    del record["archived_at"]
                rows.append(record)
            return rows

    # -- archive ------------------------------------------------------------

    def archive_fact(
        self,
        fact_id: int | str,
        *,
        note: str | None = None,
        agent: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Retire a fact from active recall. Never removes the row."""
        fact_id = _fact_id(fact_id)
        if note is not None and (not isinstance(note, str) or not note.strip()):
            raise FactError("note must be a non-empty string")
        agent = agent or self.default_agent
        now = utc_now()

        with self._write() as conn:
            row = conn.execute(
                f"SELECT {_RECALL_COLUMNS} FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                raise FactError(f"no fact with id {fact_id}")
            if row["archived_at"] is not None:
                raise FactError(f"fact {fact_id} is already archived")
            conn.execute(
                "UPDATE facts SET archived_at = ? WHERE id = ?", (now, fact_id)
            )
            conn.execute(
                "INSERT INTO sources (fact_id, action, agent, session, at, note, pointer)"
                " VALUES (?, 'archived', ?, ?, ?, ?, NULL)",
                (fact_id, agent, session, now, note),
            )
            row = conn.execute(
                f"SELECT {_RECALL_COLUMNS} FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            return self._record(row, conn)

    # -- listing ------------------------------------------------------------

    def list_projects(self) -> list[dict[str, Any]]:
        """Projects with active/archived counts, top tags, latest activity."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT project,"
                " SUM(CASE WHEN archived_at IS NULL THEN 1 ELSE 0 END) AS active,"
                " COUNT(*) AS total,"
                " MAX(COALESCE(archived_at, created_at)) AS latest"
                " FROM facts GROUP BY project ORDER BY project"
            ).fetchall()
            tag_rows = conn.execute("SELECT project, tags FROM facts").fetchall()
            per_project: dict[str, dict[str, int]] = {}
            for r in tag_rows:
                counts = per_project.setdefault(r["project"], {})
                for tag in json.loads(r["tags"]):
                    counts[tag] = counts.get(tag, 0) + 1
        projects = []
        for r in rows:
            counts = per_project.get(r["project"], {})
            top = [tag for tag, _ in sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0])
            )][:TOP_TAGS_LIMIT]
            projects.append({
                "project": r["project"],
                "active": r["active"],
                "archived": r["total"] - r["active"],
                "top_tags": top,
                "latest_activity": r["latest"],
            })
        return projects


def _fts_available() -> bool:
    """FTS5 availability probe on an in-memory connection (never touches the store)."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
            return True
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False
