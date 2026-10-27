"""Append-only, redacted audit trail for every autonomous tool invocation.

The audit log is deliberately separate from the model transcript: it is the
local record the user can inspect after the fact.  It must never contain raw
credentials, file bodies, or note contents, and it must not be world-readable.
"""

from __future__ import annotations

import asyncio
import datetime
import os
from pathlib import Path

from kakarot_daemon.scrubber import sanitize_text

MAX_SIZE_BYTES = 5 * 1024 * 1024
MAX_ENTRY_CHARS = 16_000


def _audit_paths() -> tuple[Path, Path]:
    configured = os.environ.get("KAKAROT_APP_DATA", "").strip()
    if configured:
        directory = Path(configured).expanduser()
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
        directory = base / "kakarot"
    return directory, directory / "audit_log.md"


AUDIT_DIR, AUDIT_FILE = _audit_paths()


def _secure_directory() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(AUDIT_DIR, 0o700)
    except OSError:
        # A filesystem without POSIX modes (or a read-only test mount) must not
        # prevent the daemon from starting; the log write itself will report
        # a real failure.
        pass


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def setup_audit_logger() -> None:
    _secure_directory()
    if AUDIT_FILE.exists():
        try:
            size = AUDIT_FILE.stat().st_size
        except OSError:
            return
        if size > MAX_SIZE_BYTES:
            now = datetime.datetime.now().strftime("%Y%m%d")
            archive_file = AUDIT_DIR / f"audit_log_archive_{now}.md"
            count = 1
            while archive_file.exists():
                archive_file = AUDIT_DIR / f"audit_log_archive_{now}_{count}.md"
                count += 1
            try:
                os.replace(AUDIT_FILE, archive_file)
                _secure_file(archive_file)
            except OSError:
                pass
    _secure_file(AUDIT_FILE)


# Rotate on import, matching the previous behaviour, but now with private modes.
setup_audit_logger()


def _safe_text(value: object, limit: int = 4_000) -> str:
    return sanitize_text(str(value or ""))[:limit]


async def log_action(
    intent: str,
    tool_name: str,
    details: str,
    status: str = "SUCCESS",
) -> None:
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    entry = (
        f"### [{timestamp}] {_safe_text(tool_name, 120) or 'unknown'}\n"
        f"* **Intent:** {_safe_text(intent, 500)}\n"
        f"* **Details:** {_safe_text(details, MAX_ENTRY_CHARS)}\n"
        f"* **Status:** {_safe_text(status, 500)}\n\n---\n\n"
    ).encode("utf-8")

    def _write_log() -> None:
        _secure_directory()
        fd = os.open(
            AUDIT_FILE,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            os.write(fd, entry)
        finally:
            os.close(fd)
        _secure_file(AUDIT_FILE)

    await asyncio.to_thread(_write_log)
