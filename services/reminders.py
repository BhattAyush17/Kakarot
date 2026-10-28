"""Restart-safe local reminders with a durable notification outbox."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from services.chat_bus import broadcast_event, has_active_session
from services.memory_store import database_path
from services.memory_store import purge_expired as purge_expired_memories

LOGGER = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    path = Path(database_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def initialize() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY,
                message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 1000),
                due_at REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'scheduled',
                operation_id TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY,
                reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
                occurrence_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def create(
    message: str,
    *,
    delay_seconds: float,
    operation_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    clean = " ".join(str(message or "").split())
    if not clean:
        raise ValueError("Reminder message cannot be empty.")
    if len(clean) > 1000:
        raise ValueError("Reminder message exceeds 1000 characters.")
    delay = float(delay_seconds)
    if delay < 0 or delay > 10 * 365 * 24 * 3600:
        raise ValueError("Reminder delay is outside the supported range.")
    current = float(now if now is not None else time.time())
    operation = str(operation_id or uuid.uuid4())
    initialize()
    with _connect() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO reminders(
                message, due_at, operation_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (clean, current + delay, operation, int(current), int(current)),
        )
        row = connection.execute(
            "SELECT * FROM reminders WHERE operation_id = ?", (operation,)
        ).fetchone()
    return _row(row)


def list_reminders(*, include_completed: bool = False) -> list[dict[str, Any]]:
    initialize()
    where = "" if include_completed else "WHERE state IN ('scheduled', 'firing')"
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM reminders {where} ORDER BY due_at, id LIMIT 500"
        ).fetchall()
    return [_row(row) for row in rows]


def cancel(reminder_id: int) -> bool:
    initialize()
    now = int(time.time())
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE reminders SET state = 'cancelled', updated_at = ?
            WHERE id = ? AND state = 'scheduled'
            """,
            (now, int(reminder_id)),
        )
        return cursor.rowcount > 0


def enqueue_due(*, now: float | None = None) -> int:
    initialize()
    current = float(now if now is not None else time.time())
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        due = connection.execute(
            "SELECT id FROM reminders WHERE state = 'scheduled' AND due_at <= ?",
            (current,),
        ).fetchall()
        for row in due:
            reminder_id = int(row["id"])
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_outbox(
                    reminder_id, occurrence_key, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (reminder_id, f"once:{reminder_id}", current, int(current)),
            )
            connection.execute(
                "UPDATE reminders SET state = 'firing', updated_at = ? WHERE id = ?",
                (int(current), reminder_id),
            )
        connection.commit()
    return len(due)


def pending_notifications(*, now: float | None = None) -> list[dict[str, Any]]:
    initialize()
    current = float(now if now is not None else time.time())
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT o.id AS outbox_id, o.attempts, r.id, r.message, r.due_at
            FROM notification_outbox o
            JOIN reminders r ON r.id = o.reminder_id
            WHERE o.state = 'pending' AND o.next_attempt_at <= ?
            ORDER BY o.id LIMIT 20
            """,
            (current,),
        ).fetchall()
    return [_row(row) for row in rows]


def mark_delivery(outbox_id: int, *, delivered: bool, now: float | None = None) -> None:
    current = float(now if now is not None else time.time())
    with _connect() as connection:
        if delivered:
            row = connection.execute(
                "SELECT reminder_id FROM notification_outbox WHERE id = ?",
                (int(outbox_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE notification_outbox SET state = 'delivered' WHERE id = ?",
                (int(outbox_id),),
            )
            connection.execute(
                "UPDATE reminders SET state = 'delivered', updated_at = ? WHERE id = ?",
                (int(current), int(row["reminder_id"])),
            )
        else:
            connection.execute(
                """
                UPDATE notification_outbox
                SET attempts = attempts + 1, next_attempt_at = ?
                WHERE id = ?
                """,
                (current + 30, int(outbox_id)),
            )


def _desktop_notify(message: str) -> bool:
    """Show a reminder in the GNOME notification centre.

    Uses the same native ``org.freedesktop.Notifications`` client as the
    daemon rather than ``notify-send``, so reminders respect Do Not Disturb
    and land in the notification centre like every other desktop alert.
    """
    try:
        from kakarot_daemon.notifications import notify_reminder
    except ImportError:
        LOGGER.warning("Kakarot notification client unavailable; reminder suppressed")
        return False
    try:
        asyncio.run(notify_reminder(message))
    except Exception as exc:  # never let an alert take down the scheduler
        LOGGER.warning("Could not deliver reminder notification: %s", exc)
        return False
    return True


async def run_scheduler(stop_event: asyncio.Event) -> None:
    initialize()
    last_retention_pass = 0.0
    while not stop_event.is_set():
        await asyncio.to_thread(enqueue_due)
        notifications = await asyncio.to_thread(pending_notifications)
        for notification in notifications:
            message = str(notification["message"])
            persona_connected = has_active_session()
            broadcast_event(
                {
                    "type": "kakarot.notification",
                    "kind": "reminder",
                    "id": notification["id"],
                    "message": message,
                }
            )
            delivered = await asyncio.to_thread(_desktop_notify, message)
            # An active persona event counts as a usable local delivery path.
            delivered = delivered or persona_connected
            await asyncio.to_thread(
                mark_delivery, int(notification["outbox_id"]), delivered=delivered
            )
        if time.time() - last_retention_pass > 60:
            last_retention_pass = time.time()
            await asyncio.to_thread(purge_expired_memories)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        except TimeoutError:
            pass
