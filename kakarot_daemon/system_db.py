"""System-wide SQLite knowledge engine for Kakarot.

Manages the ``kakarot.db`` database that backs the file index, application
inventory, user notes (FTS5), and execution journal.  Every connection applies
the required PRAGMAs so the WAL journal, mmap window, and cache budget are
enforced regardless of which thread opens the handle.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# The canonical location sits next to the daemon's other state files.
_DEFAULT_DB_NAME = "kakarot.db"


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Execute the performance and durability PRAGMAs that must be set on every
    fresh connection (SQLite resets them per-handle)."""
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA mmap_size=268435456")   # 256 MiB
    conn.execute("PRAGMA cache_size=-8192")       # 8 MiB negative-KiB form


def get_db(data_dir: Path) -> sqlite3.Connection:
    """Return a connection to ``kakarot.db`` with all PRAGMAs applied."""
    db_path = data_dir / _DEFAULT_DB_NAME
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(db_path.parent, 0o700)
    except OSError:
        pass
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    _apply_pragmas(conn)
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        # The file may not exist until the first write; the next open retries.
        pass
    for sidecar in (Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        try:
            os.chmod(sidecar, 0o600)
        except OSError:
            pass
    return conn


def initialize_db(data_dir: Path) -> None:
    """Create the schema tables if they do not already exist.

    Call once during daemon startup so that the audit script and any
    downstream consumers find all four tables present.
    """
    conn = get_db(data_dir)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS app_inventory (
                id          INTEGER PRIMARY KEY,
                name        TEXT    NOT NULL,
                desktop_id  TEXT,
                exec_path   TEXT,
                icon        TEXT,
                discovered  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS file_index (
                id          INTEGER PRIMARY KEY,
                path        TEXT    NOT NULL UNIQUE,
                name        TEXT    NOT NULL,
                extension   TEXT,
                size_bytes  INTEGER,
                modified_at TEXT,
                indexed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS user_notes USING fts5(
                title,
                body,
                created_at UNINDEXED
            );

            CREATE TABLE IF NOT EXISTS execution_journal (
                id          INTEGER PRIMARY KEY,
                tool_name   TEXT    NOT NULL,
                arguments   TEXT,
                result      TEXT,
                ok          INTEGER NOT NULL DEFAULT 1,
                executed_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
    finally:
        conn.close()


def _execute_checkpoint(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except sqlite3.OperationalError as e:
            # PASSIVE checkpoints may legitimately lose the lock race against
            # active writers; that is not an error condition for maintenance.
            import logging
            logging.debug("WAL checkpoint deferred: %s", e)
    finally:
        conn.close()

import asyncio

async def wal_maintenance_loop(db_path: Path, interval_seconds: int = 1800) -> None:
    """Periodically execute a PASSIVE checkpoint on the WAL to constrain growth."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(_execute_checkpoint, db_path)
        except asyncio.CancelledError:
            break
        except Exception as e:
            import logging
            logging.warning(f"WAL checkpoint failed: {e}")
