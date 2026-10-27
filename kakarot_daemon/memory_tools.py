"""Memory and deterministic file tools."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kakarot_daemon.system_db import get_db


def _data_dir() -> Path:
    configured = os.environ.get("KAKAROT_APP_DATA", "").strip()
    if configured:
        return Path(configured).expanduser()
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return base / "kakarot"


def _get_kakarot_db() -> sqlite3.Connection:
    conn = get_db(_data_dir())
    conn.row_factory = sqlite3.Row
    return conn


def _recent_file_scan(limit: int) -> list[dict[str, Any]]:
    """Bounded fallback for a database whose index has never been populated.

    The old tool returned an empty list forever because no indexer was wired
    up.  This scan is deliberately capped and skips hidden/symlinked entries;
    it is a recovery path, not a crawler.
    """
    home = Path.home()
    roots = [
        path
        for path in (home / "Documents", home / "Downloads", home / "Desktop")
        if path.is_dir()
    ]
    found: list[tuple[float, Path]] = []
    inspected = 0
    for root in roots:
        for directory, subdirs, names in os.walk(root, followlinks=False):
            subdirs[:] = [
                name for name in subdirs if not name.startswith(".")
            ][:40]
            for name in names:
                if name.startswith("."):
                    continue
                inspected += 1
                if inspected > 4000:
                    break
                path = Path(directory) / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    found.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            if inspected > 4000:
                break
        if inspected > 4000:
            break
    found.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "path": str(path),
            "modified_at": datetime.fromtimestamp(
                modified, tz=datetime.now().astimezone().tzinfo
            ).isoformat(),
            "size_bytes": path.stat().st_size if path.exists() else None,
        }
        for modified, path in found[:limit]
    ]


def get_recent_files(limit: int = 5) -> dict[str, Any]:
    """Return recently modified files from the index, with a bounded fallback."""
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 5
    conn = _get_kakarot_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT path, modified_at FROM file_index "
            "ORDER BY modified_at DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        conn.close()
    if rows:
        return {"ok": True, "recent_files": rows, "source": "index"}
    return {
        "ok": True,
        "recent_files": _recent_file_scan(limit),
        "source": "scan",
    }


def remember_fact(title: str, content: str) -> dict[str, Any]:
    """Strip credentials, then write to the persistent FTS5 notes table."""
    conn = _get_kakarot_db()
    try:
        from kakarot_daemon.scrubber import sanitize_text
        sanitized_title = sanitize_text(title)
        sanitized_content = sanitize_text(content)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user_notes (title, body) VALUES (?, ?)",
            (sanitized_title, sanitized_content),
        )
        conn.commit()
        return {"ok": True, "status": "Fact remembered successfully"}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        conn.close()


def _sanitize_fts5_query(query: str) -> str:
    """Normalize arbitrary user text into a safe FTS5 MATCH expression.

    FTS5 raises OperationalError on malformed syntax (unbalanced quotes,
    dangling operators, stray syntax chars). Collapse the query into a set of
    quoted phrase terms OR-ed together, which is always syntactically valid.
    """
    if not query or not query.strip():
        return '""'
    terms = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_\-']*", query)
    if not terms:
        return '""'
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms[:16])


def search_memory(query: str, limit: int = 3) -> dict[str, Any]:
    """Execute a sanitized FTS5 MATCH query using the snippet() function."""
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 3
    conn = _get_kakarot_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT title, snippet(user_notes, 1, '[', ']', '...', 10) as snippet
            FROM user_notes
            WHERE user_notes MATCH ?
            LIMIT ?
            """,
            (_sanitize_fts5_query(query), limit),
        )
        return {"ok": True, "results": [dict(r) for r in cursor.fetchall()]}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        conn.close()


async def schedule_reminder(
    reminders_store,
    task: str,
    relative_days: int = 0,
    time_str: str = "09:00",
) -> dict[str, Any]:
    """Compute an exact local timestamp before inserting into ReminderStore."""
    try:
        tz = datetime.now().astimezone().tzinfo
    except Exception:
        from datetime import timezone

        tz = timezone.utc

    now = datetime.now(tz)
    target_date = now.date() + timedelta(days=relative_days)

    hours, minutes = 9, 0
    if ":" in time_str:
        try:
            h, m = time_str.split(":")
            hours = int(h)
            minutes = int(m)
        except ValueError:
            pass

    target_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hours,
        minutes,
        tzinfo=tz,
    )

    if target_dt < now and relative_days == 0:
        target_dt += timedelta(days=1)

    reminder = await reminders_store.add(task.strip(), target_dt.isoformat())
    return {
        "ok": True,
        "id": reminder.id,
        "title": reminder.title,
        "due_at": reminder.due_at,
    }
