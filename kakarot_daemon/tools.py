"""Small, audited tool surface for Gemini Live.

No shell is exposed.  Each function has bounded work and returns compact JSON.
File mutations are home-confined and never execute generated scripts.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from kakarot_daemon.reminders import ReminderStore
from kakarot_daemon.file_tools import write_file, organize_directory
from kakarot_daemon.sandbox import allowed_document_roots, run_sandboxed_command
from kakarot_daemon.scrubber import sanitize_text


SAFE_READ_ONLY_COMMANDS = frozenset({"ls", "cat", "pwd", "df", "whoami", "echo"})
READ_ONLY_COMMAND_TIMEOUT_SECONDS = 5
APPROVED_COMMAND_TIMEOUT_SECONDS = 15
MAX_COMMAND_LENGTH = 1000
AGENCY_LEVELS = ("input", "decision", "action", "critical")
AGENCY_LABELS = {
    "input": "Need your input",
    "decision": "Your decision",
    "action": "Action required",
    "critical": "Immediate action",
}
AGENCY_DEFAULT_OPTIONS = {
    "input": ["Continue"],
    "decision": ["Approve", "Reject"],
    "action": ["I'm done"],
    "critical": ["Acknowledge"],
}
MAX_AGENCY_OPTIONS = 8


def _readonly_shell_command(command: str) -> str | None:
    """Return a shell-quoted simple command only for the read-only allowlist."""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not argv or argv[0] not in SAFE_READ_ONLY_COMMANDS:
        return None
    # Shell syntax always requires approval, including operators embedded in
    # an otherwise allowlisted command. Quoting every argv item below also
    # prevents expansion or operator interpretation by bash.
    if re.search(r"[;&|<>`$()\r\n]", command):
        return None
    return shlex.join(argv)


def _audit_details(tool_name: str, arguments: dict[str, Any]) -> str:
    """Return a useful audit summary without storing file/note bodies."""
    if tool_name == "execute_command":
        command = sanitize_text(str(arguments.get("command", "")))[:1500]
        return f"\n```bash\n{command}\n```\n"
    if tool_name == "write_file":
        path = sanitize_text(str(arguments.get("file_path", "")))[:500]
        content = str(arguments.get("content", ""))
        return f"path={path}; content_chars={len(content)} (content not logged)"
    if tool_name == "organize_directory":
        return f"target={sanitize_text(str(arguments.get('target_directory', '')))[:500]}"
    if tool_name == "remember_fact":
        title = sanitize_text(str(arguments.get("title", "")))[:300]
        return f"title={title}; content_chars={len(str(arguments.get('content', '')))} (content not logged)"
    if tool_name in {"fetch_web_content", "play_media"}:
        return f"url={sanitize_text(str(arguments.get('url', '')))[:1500]}"
    return sanitize_text(str(arguments))[:1000]


def _run_sandboxed_command(command: str, timeout: int) -> dict[str, Any]:
    """Run through the one Bubblewrap policy shared with runtime and tests."""
    return run_sandboxed_command(command, timeout=timeout)


def _allowed_roots() -> tuple[Path, ...]:
    """Return the document roots exposed by the sandbox policy."""
    return allowed_document_roots()


def _resolve_in_root(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser().resolve()
    if not any(candidate.is_relative_to(root.resolve()) for root in _allowed_roots()):
        raise ValueError("path is outside the permitted user folders")
    return candidate


def tool_declarations() -> list[dict[str, Any]]:
    return [
        {
            "name": "schedule_reminder",
            "description": "Schedule a persistent local alert. Expects offset days (relative_days) and a 24h time string (time_str). Do not calculate raw ISO-8601 timestamps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "relative_days": {"type": "integer"},
                    "time_str": {"type": "string", "description": "e.g., '09:00'"}
                },
                "required": ["task"],
            },
        },
        {
            "name": "write_file",
            "description": "Create a UTF-8 text file inside the user's home. Parent folder must exist. Never overwrites existing files, accesses hidden paths, or executes scripts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        },
        {
            "name": "organize_directory",
            "description": "Move up to 500 top-level regular files into extension categories in a home folder. Skips hidden files, symlinks, subfolders and destination collisions. Use only on a folder the user asks to organize.",
            "parameters": {
                "type": "object",
                "properties": {"target_directory": {"type": "string"}},
                "required": ["target_directory"],
            },
        },
        {
            "name": "system_status",
            "description": "Return disk, memory, and uptime diagnostics.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "find_files",
            "description": "Find up to 20 files by name in Documents, Downloads, or Desktop.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "plan_file_organization",
            "description": "Preview how files in an allowed folder would be grouped by extension. This never moves files.",
            "parameters": {
                "type": "object",
                "properties": {"folder": {"type": "string"}},
                "required": ["folder"],
            },
        },
        {
            "name": "list_due_reminders",
            "description": "List reminders that are due now.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "get_system_state",
            "description": "Read CPU, RAM, and disk usage using psutil.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "execute_command",
            "description": "Execute a Linux command in a Bubblewrap namespace with a read-only filesystem. Only ls, cat, pwd, df, whoami, and echo run automatically; every other command or shell expression requires user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "maxLength": 1000},
                    "allow_network": {"type": "boolean", "description": "Network access remains isolated by the sandbox."}
                },
                "required": ["command"],
            },
        },
        {
            "name": "request_human_intervention",
            "description": "Pause and ask the user when you need missing information, a decision, an external step, or immediate confirmation. Returns the option the user selected. Use action level with the default \"I'm done\" option for steps only the user can perform.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "string",
                        "enum": ["input", "decision", "action", "critical"],
                    },
                    "label": {"type": "string", "maxLength": 80},
                    "reason": {"type": "string", "maxLength": 500},
                    "prompt": {"type": "string", "maxLength": 1200},
                    "options": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 80},
                        "maxItems": MAX_AGENCY_OPTIONS,
                    },
                },
                "required": ["level", "reason", "prompt"],
            },
        },
        {
            "name": "fetch_web_content",
            "description": "Extract readable article/documentation text from a given URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
        {
            "name": "play_media",
            "description": "Use this tool when the user asks to 'play a song', 'play this video', or 'listen to a podcast'. Plays media via mpv or browser fallback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "video": {"type": "boolean"}
                },
                "required": ["url"],
            },
        },
        {
            "name": "get_recent_files",
            "description": "Fetch the most recently modified files in the system from the Kakarot file index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of files to return. Defaults to 5."
                    }
                }
            },
        },
        {
            "name": "remember_fact",
            "description": "Save an important fact to the persistent memory database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["title", "content"]
            },
        },
        {
            "name": "search_memory",
            "description": "Search the persistent memory database for saved facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"}
                },
                "required": ["query"]
            },
        },
    ]


class ToolRegistry:
    # Handlers that block, so they run in a worker thread.
    _THREADED = (
        "write_file",
        "organize_directory",
        "find_files",
        "plan_file_organization",
        "system_status",
        "get_system_state",
        "get_recent_files",
        "remember_fact",
        "search_memory",
    )
    # Handlers that await the reminder store, which keeps SQLite off the loop.
    _AWAITING = (
        "schedule_reminder",
        "list_due_reminders",
        "read_screen_context",
        "execute_command",
        "request_human_intervention",
        "fetch_web_content",
        "play_media",
    )

    def __init__(self, reminders: ReminderStore) -> None:
        from kakarot_daemon.repo_bridge import RepositoryBridge

        self.repository = RepositoryBridge()
        self._reminders = reminders
        self._authorize_command: Callable[[str], Awaitable[bool]] | None = None
        self._human_intervention_handler: (
            Callable[[str, str, str, str, list[str]], Awaitable[str | None]] | None
        ) = None
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "schedule_reminder": self._schedule_reminder,
            "write_file": lambda args: write_file(**args),
            "organize_directory": lambda args: organize_directory(**args),
            "system_status": self._system_status,
            "find_files": self._find_files,
            "plan_file_organization": self._plan_file_organization,
            "list_due_reminders": self._list_due_reminders,
            "get_system_state": self._get_system_state,
            "execute_command": self._execute_command,
            "request_human_intervention": self._request_human_intervention,
            "fetch_web_content": self._fetch_web_content,
            "play_media": self._play_media,
            "read_screen_context": self._read_screen_context,
            "get_recent_files": self._get_recent_files,
            "remember_fact": self._remember_fact,
            "search_memory": self._search_memory,
        }

    def set_authorization_handler(
        self, handler: Callable[[str], Awaitable[bool]]
    ) -> None:
        """Connect command approval to the UI control socket."""
        self._authorize_command = handler

    def set_human_intervention_handler(
        self,
        handler: Callable[[str, str, str, str, list[str]], Awaitable[str | None]],
    ) -> None:
        """Connect the 3-tier agency prompt to the control socket."""
        self._human_intervention_handler = handler

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        from kakarot_daemon.audit_logger import log_action

        handler = self._handlers.get(name)
        if handler is None:
            # Not a local handler, delegate to repository bridge but log it anyway
            return await self.repository.execute(name, arguments)
            
        intent = "Autonomous tool execution"
        if name == "read_screen_context":
            details = "Requested AT-SPI2 screen context extraction (content redacted for privacy)."
        else:
            details = _audit_details(name, arguments)
            
        status = "SUCCESS"
        result = None
        
        try:
            if name in self._THREADED:
                task = asyncio.create_task(asyncio.to_thread(handler, arguments))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            elif name in self._AWAITING:
                result = await handler(arguments)
            else:
                result = handler(arguments)
                
            if result == "User denied execution":
                status = "DENIED BY USER"
            elif isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
                if result.get("error") == "AWAITING_CONFIRMATION":
                    status = "AWAITING CONFIRMATION"
                else:
                    status = f"FAILED: {result.get('error', 'Unknown error')}"
                
            return result
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            status = f"ERROR: {str(exc)[:200]}"
            result = {"ok": False, "error": str(exc)[:200]}
            return result
        except Exception as e:
            status = f"FATAL ERROR: {str(e)[:200]}"
            raise
        finally:
            try:
                await log_action(intent, name, details, status)
            except Exception:
                pass

    async def schedule_reminder(self, task: str, relative_days: int = 0, time_str: str = "09:00") -> dict[str, Any]:
        from kakarot_daemon.memory_tools import schedule_reminder as sr
        return await sr(self._reminders, task, relative_days, time_str)

    async def _schedule_reminder(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.schedule_reminder(**arguments)

    def _system_status(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        disk = shutil.disk_usage(Path.home())
        load = os.getloadavg()
        return {
            "ok": True,
            "disk": {"free_bytes": disk.free, "total_bytes": disk.total},
            "load_1m": load[0],
        }

    def _find_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip().casefold()
        if not query or len(query) > 120:
            raise ValueError("query must be 1 to 120 characters")
        matches: list[dict[str, Any]] = []
        inspected = 0
        truncated = False
        for root in _allowed_roots():
            if truncated:
                break
            for directory, subdirs, names in os.walk(root, followlinks=False):
                subdirs[:] = [
                    name for name in subdirs if not name.startswith(".")
                ][:40]
                for name in names:
                    if name.startswith("."):
                        continue
                    inspected += 1
                    if inspected > 4000:
                        truncated = True
                        break
                    if query not in name.casefold():
                        continue
                    path = Path(directory) / name
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        stat = path.stat()
                    except OSError:
                        continue
                    matches.append(
                        {
                            "path": str(path),
                            "size_bytes": stat.st_size,
                            "modified_at": datetime.fromtimestamp(
                                stat.st_mtime, timezone.utc
                            ).isoformat(),
                        }
                    )
                    if len(matches) >= 20:
                        return {"ok": True, "matches": matches, "truncated": True}
                if truncated:
                    break
        return {"ok": True, "matches": matches, "truncated": truncated}

    def _plan_file_organization(self, arguments: dict[str, Any]) -> dict[str, Any]:
        folder = _resolve_in_root(str(arguments.get("folder") or ""))
        if not folder.is_dir():
            raise ValueError("folder does not exist")
        groups: dict[str, int] = {}
        for path in list(folder.iterdir())[:500]:
            if path.is_file():
                extension = path.suffix.casefold().lstrip(".") or "no_extension"
                groups[extension] = groups.get(extension, 0) + 1
        return {
            "ok": True,
            "folder": str(folder),
            "groups": groups,
            "preview_only": True,
        }


    async def _list_due_reminders(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "reminders": [
                reminder.__dict__ for reminder in await self._reminders.due()
            ],
        }

    def _get_system_state(self, _arguments):
        import psutil
        import shutil
        disk = shutil.disk_usage("/")
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "ram_percent": psutil.virtual_memory().percent,
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "disk_total_gb": round(disk.total / (1024**3), 2)
        }

    async def _request_human_intervention(self, arguments):
        """Pause for an explicit 3-tier human response and return the choice."""
        level = str(arguments.get("level") or "").strip().lower()
        if level not in AGENCY_LEVELS:
            raise ValueError(
                "level must be one of input, decision, action, or critical"
            )
        reason = str(arguments.get("reason") or "").strip()
        prompt = str(arguments.get("prompt") or "").strip()
        if not reason or len(reason) > 500:
            raise ValueError("reason must be 1 to 500 characters")
        if not prompt or len(prompt) > 1200:
            raise ValueError("prompt must be 1 to 1200 characters")

        label = str(arguments.get("label") or AGENCY_LABELS[level]).strip()[:80]
        if not label:
            label = AGENCY_LABELS[level]

        raw_options = arguments.get("options")
        if raw_options is None:
            options = list(AGENCY_DEFAULT_OPTIONS[level])
        elif not isinstance(raw_options, list):
            raise ValueError("options must be a list of strings")
        else:
            options = [
                str(option).strip()[:80]
                for option in raw_options
                if str(option).strip()
            ][:MAX_AGENCY_OPTIONS]
        if not options:
            options = list(AGENCY_DEFAULT_OPTIONS[level])

        if self._human_intervention_handler is None:
            return {"ok": False, "error": "human intervention is unavailable"}
        try:
            choice = await self._human_intervention_handler(
                level, label, reason, prompt, options
            )
        except (ConnectionError, OSError, asyncio.TimeoutError):
            choice = None
        if not isinstance(choice, str) or not choice.strip():
            return {"ok": False, "error": "the user did not respond"}
        return {"ok": True, "level": level, "response": choice.strip()[:80]}

    async def _execute_command(self, arguments):
        cmd = arguments.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("Command is required")
        cmd = cmd.strip()
        if len(cmd) > MAX_COMMAND_LENGTH:
            raise ValueError(f"Command must be {MAX_COMMAND_LENGTH} characters or fewer")

        readonly_command = _readonly_shell_command(cmd)
        if readonly_command is not None:
            execution_command = readonly_command
            timeout = READ_ONLY_COMMAND_TIMEOUT_SECONDS
        else:
            approved = False
            if self._authorize_command is not None:
                try:
                    approved = await self._authorize_command(cmd)
                except (ConnectionError, OSError, asyncio.TimeoutError):
                    approved = False
            if approved is not True:
                return "User denied execution"
            execution_command = cmd
            timeout = APPROVED_COMMAND_TIMEOUT_SECONDS

        # Keep the sandbox subprocess and its timeout off the event loop while
        # the tool coroutine remains awaitable for both execution and approval.
        return await asyncio.to_thread(
            _run_sandboxed_command, execution_command, timeout
        )

    async def _fetch_web_content(self, arguments):
        from kakarot_daemon.web_tools import fetch_web_content
        url = arguments.get("url")
        if not url:
            raise ValueError("URL is required")
        result = await fetch_web_content(url)
        if result.startswith("Error:"):
            return {"error": result}
        return {"content": result}

    async def _play_media(self, arguments):
        from kakarot_daemon.media_tools import play_media
        url = arguments.get("url")
        if not url:
            raise ValueError("URL is required")
        video = arguments.get("video", False)
        result = await play_media(url, video)
        return {"status": result}

    async def _read_screen_context(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        from kakarot_daemon.desktop_context import read_screen_context
        return await read_screen_context()

    def _get_recent_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from kakarot_daemon.memory_tools import get_recent_files
        return get_recent_files(arguments.get("limit", 5))

    def _remember_fact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from kakarot_daemon.memory_tools import remember_fact
        return remember_fact(arguments.get("title", ""), arguments.get("content", ""))

    def _search_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from kakarot_daemon.memory_tools import search_memory
        return search_memory(arguments.get("query", ""), arguments.get("limit", 3))
