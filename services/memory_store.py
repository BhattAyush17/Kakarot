"""Local, source-aware long-term memory backed by SQLite and FTS5."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from core.paths import user_data_dir

SCHEMA_VERSION = 2
MAX_MEMORY_TEXT = 8_000
_lock = threading.RLock()


def database_path() -> Path:
    return Path(user_data_dir()) / "memory.sqlite3"


def _connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA cache_size=-4096")
    return connection


def initialize() -> None:
    with _lock, _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'fact',
                text TEXT NOT NULL CHECK(length(text) <= 8000),
                project TEXT NOT NULL DEFAULT '',
                source_uri TEXT NOT NULL DEFAULT '',
                source_locator TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 1.0,
                confirmed INTEGER NOT NULL DEFAULT 1,
                private INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                text, project, content='memories', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memory_fts(rowid, text, project)
                VALUES (new.id, new.text, new.project);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, text, project)
                VALUES ('delete', old.id, old.text, old.project);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, text, project)
                VALUES ('delete', old.id, old.text, old.project);
                INSERT INTO memory_fts(rowid, text, project)
                VALUES (new.id, new.text, new.project);
            END;
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
            "VALUES (?, ?)",
            (1, int(time.time())),
        )
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(memories)").fetchall()
        }
        for column, definition in (
            ("supersedes_id", "INTEGER REFERENCES memories(id)"),
            ("expires_at", "INTEGER"),
            ("content_hash", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE memories ADD COLUMN {column} {definition}"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_content_hash "
            "ON memories(content_hash)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
            "VALUES (?, ?)",
            (SCHEMA_VERSION, int(time.time())),
        )


def _clean_text(value: Any, *, limit: int = MAX_MEMORY_TEXT) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError("Memory text cannot be empty.")
    if len(text) > limit:
        raise ValueError(f"Memory text exceeds {limit} characters.")
    return text


def _row(row: sqlite3.Row, *, superseded_ids: set[int] | None = None) -> dict[str, Any]:
    result = dict(row)
    for key in ("confirmed", "private", "pinned"):
        if key in result:
            result[key] = bool(result[key])
    if superseded_ids is not None:
        result["superseded"] = int(result.get("id") or 0) in superseded_ids
    return result


def _superseded_ids(connection: sqlite3.Connection) -> set[int]:
    return {
        int(row["supersedes_id"])
        for row in connection.execute(
            "SELECT DISTINCT supersedes_id FROM memories "
            "WHERE supersedes_id IS NOT NULL"
        ).fetchall()
        if row["supersedes_id"] is not None
    }


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()


def remember(
    text: str,
    *,
    kind: str = "fact",
    project: str = "",
    private: bool = False,
    source_uri: str = "user",
    source_locator: str = "explicit",
    expires_at: int | None = None,
) -> dict[str, Any]:
    clean = _clean_text(text)
    clean_project = " ".join(str(project or "").split())[:200]
    clean_kind = re.sub(r"[^a-z0-9_-]", "", str(kind).lower())[:40] or "fact"
    now = int(time.time())
    initialize()
    digest = _content_hash(clean)
    with _lock, _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM memories WHERE content_hash = ? ORDER BY id LIMIT 1",
            (digest,),
        ).fetchone()
        if existing is not None:
            return _row(existing, superseded_ids=_superseded_ids(connection))
        cursor = connection.execute(
            """
            INSERT INTO memories(
                kind, text, project, source_uri, source_locator, private,
                content_hash, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_kind,
                clean,
                clean_project,
                str(source_uri or "")[:1000],
                str(source_locator or "")[:500],
                int(private),
                digest,
                int(expires_at) if expires_at is not None else None,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM memories WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _row(row, superseded_ids=_superseded_ids(connection))


def correct(
    memory_id: int,
    new_text: str,
    *,
    project: str | None = None,
    private: bool | None = None,
) -> dict[str, Any]:
    """Supersede one memory with a corrected record; never merge silently."""
    clean = _clean_text(new_text)
    now = int(time.time())
    initialize()
    digest = _content_hash(clean)
    with _lock, _connect() as connection:
        old = connection.execute(
            "SELECT * FROM memories WHERE id = ?", (int(memory_id),)
        ).fetchone()
        if old is None:
            raise ValueError("Memory to correct was not found.")
        duplicate = connection.execute(
            "SELECT * FROM memories WHERE content_hash = ? AND id != ? "
            "ORDER BY id LIMIT 1",
            (digest, int(memory_id)),
        ).fetchone()
        if duplicate is not None:
            connection.execute(
                "UPDATE memories SET supersedes_id = ? WHERE id = ?",
                (int(duplicate["id"]), int(memory_id)),
            )
            return _row(duplicate, superseded_ids=_superseded_ids(connection))
        cursor = connection.execute(
            """
            INSERT INTO memories(
                kind, text, project, source_uri, source_locator, private,
                content_hash, supersedes_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(old["kind"]),
                clean,
                str(old["project"]) if project is None else " ".join(project.split())[:200],
                "correction",
                f"corrects:{int(memory_id)}",
                int(bool(old["private"])) if private is None else int(private),
                digest,
                int(memory_id),
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM memories WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _row(row, superseded_ids=_superseded_ids(connection))


def list_memories(
    *, limit: int = 100, include_private: bool = True
) -> list[dict[str, Any]]:
    initialize()
    safe_limit = max(1, min(int(limit), 500))
    privacy = "" if include_private else "WHERE private = 0"
    with _lock, _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM memories {privacy} "
            "ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
        superseded = _superseded_ids(connection)
    return [_row(row, superseded_ids=superseded) for row in rows]


def _fts_query(query: str) -> str:
    terms = re.findall(r"[\w-]+", query, flags=re.UNICODE)[:12]
    if not terms:
        raise ValueError("Search query cannot be empty.")
    return " AND ".join(f'"{term.replace(chr(34), "")}"*' for term in terms)


def search(
    query: str,
    *,
    project: str = "",
    limit: int = 8,
    include_private: bool = False,
) -> list[dict[str, Any]]:
    initialize()
    conditions = [
        "memory_fts MATCH ?",
        (
            "NOT EXISTS ("
            "SELECT 1 FROM memories s WHERE s.supersedes_id = m.id"
            ")"
        ),
    ]
    parameters: list[Any] = [_fts_query(query)]
    if project:
        conditions.append("m.project = ?")
        parameters.append(" ".join(project.split())[:200])
    if not include_private:
        conditions.append("m.private = 0")
    parameters.append(max(1, min(int(limit), 20)))
    sql = f"""
        SELECT m.*, bm25(memory_fts) AS rank
        FROM memory_fts
        JOIN memories m ON m.id = memory_fts.rowid
        WHERE {" AND ".join(conditions)}
        ORDER BY m.pinned DESC, rank, m.updated_at DESC
        LIMIT ?
    """
    with _lock, _connect() as connection:
        rows = connection.execute(sql, parameters).fetchall()
        superseded = _superseded_ids(connection)
    return [_row(row, superseded_ids=superseded) for row in rows]


def forget(memory_id: int) -> bool:
    initialize()
    with _lock, _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM memories WHERE id = ?", (int(memory_id),)
        )
        return cursor.rowcount > 0


def purge_expired(*, now: int | None = None) -> int:
    """Delete expired auto-generated memories; explicit and pinned records persist."""
    initialize()
    current = int(now if now is not None else time.time())
    with _lock, _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL "
            "AND expires_at <= ? AND pinned = 0",
            (current,),
        )
        return max(0, cursor.rowcount)


def export_memories() -> list[dict[str, Any]]:
    """Full local export, including private and superseded records."""
    initialize()
    with _lock, _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM memories ORDER BY id"
        ).fetchall()
        superseded = _superseded_ids(connection)
    return [_row(row, superseded_ids=superseded) for row in rows]
