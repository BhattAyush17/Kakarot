"""SQLite reminder store with all database work outside the event loop."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Reminder:
    id: int
    title: str
    due_at: str
    completed: bool


class ReminderStore:
    """Each operation opens a short-lived SQLite connection in a worker."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _run_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self._path, timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reminders "
                "(id INTEGER PRIMARY KEY, title TEXT NOT NULL, due_at TEXT NOT NULL, "
                "completed INTEGER NOT NULL DEFAULT 0)"
            )
            result = operation(connection)
            connection.commit()
            return result
        finally:
            connection.close()
            # SQLite creates the database and its WAL/SHM sidecars with the
            # process umask; reminder titles are private user data.
            for candidate in (
                self._path,
                Path(str(self._path) + "-wal"),
                Path(str(self._path) + "-shm"),
            ):
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    pass

    async def add(self, title: str, due_at: str) -> Reminder:
        parsed = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("due_at must include a timezone")
        normalized = parsed.astimezone(timezone.utc).isoformat()

        def operation(connection: sqlite3.Connection) -> Reminder:
            cursor = connection.execute(
                "INSERT INTO reminders (title, due_at) VALUES (?, ?)",
                (title.strip(), normalized),
            )
            return Reminder(cursor.lastrowid, title.strip(), normalized, False)

        return await asyncio.to_thread(self._run_sync, operation)

    async def due(self, now: datetime | None = None) -> list[Reminder]:
        instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

        def operation(connection: sqlite3.Connection) -> list[Reminder]:
            rows = connection.execute(
                "SELECT id, title, due_at, completed FROM reminders "
                "WHERE completed = 0 AND due_at <= ? ORDER BY due_at LIMIT 20",
                (instant,),
            ).fetchall()
            return [Reminder(row[0], row[1], row[2], bool(row[3])) for row in rows]

        return await asyncio.to_thread(self._run_sync, operation)

    async def mark_completed(self, reminder_id: int) -> None:
        await asyncio.to_thread(
            self._run_sync,
            lambda connection: connection.execute(
                "UPDATE reminders SET completed = 1 WHERE id = ?", (reminder_id,)
            ),
        )

    async def close(self) -> None:
        """Retained for lifecycle symmetry; operations have no retained connection."""
