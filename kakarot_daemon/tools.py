"""Small, audited tool surface for Gemini Live.

No shell is exposed.  Each function has bounded work and returns compact JSON.
File mutations are home-confined and never execute generated scripts.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kakarot_daemon.file_tools import organize_directory, write_file
from kakarot_daemon.reminders import ReminderStore
from kakarot_daemon.sandbox import allowed_document_roots, run_sandboxed_command
from kakarot_daemon.scrubber import sanitize_text


SAFE_READ_ONLY_COMMANDS = frozenset({"ls", "cat", "pwd", "df", "whoami", "echo"})
READ_ONLY_COMMAND_TIMEOUT_SECONDS = 5
APPROVED_COMMAND_TIMEOUT_SECONDS = 15
MAX_COMMAND_LENGTH = 1000
DESKTOP_TOOL_TIMEOUT_SECONDS = 10
AUDIO_ACTIONS = frozenset({"mute", "unmute", "set_volume", "status"})
NETWORK_ACTIONS = frozenset({"status", "toggle_wifi"})
DESKTOP_SETTINGS = frozenset({"dark_mode", "light_mode"})
# Commands that can destroy data or a device never reach the approval gate:
# approving them cannot make them safe, so the model gets an immediate refusal
# instead of a prompt the user might click through.
DESTRUCTIVE_COMMAND_BLOCKLIST = re.compile(
    r"(?:"
    r"\brm\s+(?:-[a-z]*r|--recursive)"  # rm -r, rm -rf, rm -fr, rm --recursive
    r"|\bmkfs"                         # mkfs and mkfs.*
    r"|\bdd\s+if="                     # raw device writes via dd
    r"|>\s*/dev/[hs]d"                 # redirecting output onto a disk device
    r"|\bchown\s+-R"                   # recursive ownership changes
    r"|\bchmod\s+-R"                   # recursive permission changes
    r")",
    re.IGNORECASE,
)
# One command's output is data for one answer, not an archive. Keep the head
# (headers, first rows) and the tail (exit errors, final rows); the middle is
# usually the bulk that would otherwise dominate the next model request.
OS_OUTPUT_LIMIT = 2000
OS_OUTPUT_HEAD = 1000
OS_OUTPUT_TAIL = 1000
OS_OUTPUT_TRUNCATION_MARKER = (
    "\n...[TRUNCATED: output exceeded memory/token safety limits]...\n"
)
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

# OpenCode delegation is a deliberately privileged path: it receives one
# writable repository mount and a transient systemd scope, but its output still
# travels through the same bounded stream reader as every other subprocess.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OPENCODE_DELEGATION_TIMEOUT_SECONDS = 300
OPENCODE_MODEL = os.environ.get(
    "KAKAROT_OPENCODE_MODEL", "opencode/deepseek-v4.1-flash-free"
).strip()
OPENCODE_TASK_MAX_CHARS = 1000
OPENCODE_MAX_TARGET_FILES = 20
OPENCODE_TARGET_PATH_MAX_CHARS = 300
OPENCODE_PROMPT_MAX_CHARS = 1200
# /tmp is the sandbox's writable tmpfs; /usr is a read-only bind and cannot
# receive a new executable mount point.
OPENCODE_SANDBOX_BINARY = "/tmp/opencode/bin/opencode"
OPENCODE_SANDBOX_NODE_BIN = "/tmp/opencode/node-bin"

# Auto-Medic diagnostics and self-restart.  The diagnostic commands are
# read-only and bounded; the restart is deliberately delayed so the model's
# turn can finish and its response can reach the UI before systemd stops this
# process.
DAEMON_SERVICE_UNIT = "kakarot-core.service"
DAEMON_RESTART_DELAY_SECONDS = 2.0
SELF_DIAGNOSTICS_TIMEOUT_SECONDS = 120
SELF_DIAGNOSTICS_LOG_TIMEOUT_SECONDS = 20
SELF_DIAGNOSTICS_PYTEST_COMMAND = (
    "venv/bin/python3 -m pytest -q tests/ -p no:cacheprovider"
)
SELF_DIAGNOSTICS_LOG_COMMAND = (
    f"journalctl --user -u {DAEMON_SERVICE_UNIT} -n 40 --no-pager"
)
DAEMON_RESTART_COMMAND = f"systemctl --user restart {DAEMON_SERVICE_UNIT}"


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
    if tool_name in {"execute_command", "execute_system_command"}:
        command = sanitize_text(str(arguments.get("command", "")))[:1500]
        return f"\n```bash\n{command}\n```\n"
    if tool_name == "manage_audio":
        action = sanitize_text(str(arguments.get("action", "")))[:40]
        volume = arguments.get("volume_percent")
        return f"action={action}; volume_percent={volume}"
    if tool_name == "manage_network":
        action = sanitize_text(str(arguments.get("action", "")))[:40]
        state = arguments.get("state")
        return f"action={action}; state={state}"
    if tool_name == "manage_desktop_settings":
        setting = sanitize_text(str(arguments.get("setting", "")))[:40]
        return f"setting={setting}"
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
    if tool_name == "delegate_to_opencode":
        task = sanitize_text(str(arguments.get("task_description", "")))[:500]
        files = sanitize_text(
            ", ".join(str(item) for item in arguments.get("target_files", []))
        )[:1000]
        return f"task={task}; files={files} (delegated to OpenCode)"
    if tool_name == "run_self_diagnostics":
        return "pytest and recent kakarot-core.service journal"
    if tool_name == "restart_daemon":
        return DAEMON_RESTART_COMMAND
    return sanitize_text(str(arguments))[:1000]


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    """Remove terminal colour/control sequences before they reach Gemini."""
    return _ANSI_ESCAPE_RE.sub("", text)


def _truncate_middle(text: str, limit: int = OS_OUTPUT_LIMIT) -> str:
    """Keep both ends of OS output within a strict character budget.

    A command can print thousands of lines (``ps``, ``cat``, logs). The model
    needs the first rows to understand the shape and the last rows to see the
    failure, but not the repeated middle. The marker is intentionally explicit
    so the model knows the data is incomplete.
    """
    if len(text) <= limit:
        return text
    head = min(OS_OUTPUT_HEAD, limit // 2)
    tail = min(OS_OUTPUT_TAIL, limit - head)
    return text[:head] + OS_OUTPUT_TRUNCATION_MARKER + text[-tail:]


def _run_sandboxed_command(
    command: str, timeout: int, **kwargs: Any
) -> dict[str, Any]:
    """Run through the one Bubblewrap policy shared with runtime and tests.

    The runner is synchronous because ``_execute_command`` already offloads it
    with ``asyncio.to_thread``. It streams both pipes through the bounded ring
    buffer in :mod:`kakarot_daemon.sandbox`; no full payload is retained.
    """
    return run_sandboxed_command(command, timeout=timeout, **kwargs)


def _diagnostics_journal_mounts() -> tuple[tuple[Path, str, bool], ...]:
    """Expose only the journal files journalctl needs, read-only."""
    candidates = (
        Path("/var/log/journal"),
        Path("/run/log/journal"),
        Path("/etc/machine-id"),
    )
    return tuple(
        (path, str(path), False) for path in candidates if path.exists()
    )


def _diagnostics_pyqt_mounts() -> tuple[tuple[Path, str, bool], ...]:
    """Expose the system PyQt6 package the venv intentionally omits.

    The daemon venv is Qt-free; the desktop overlay uses the system PyQt6. The
    GUI tests need that package, so mount only PyQt6 into an isolated path
    rather than leaking the whole system site-packages tree into the venv.
    """
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = (
        Path(f"/usr/lib64/{version}/site-packages/PyQt6"),
        Path(f"/usr/lib/{version}/site-packages/PyQt6"),
        Path(f"/usr/lib/{version}/dist-packages/PyQt6"),
        Path("/usr/lib/python3/dist-packages/PyQt6"),
    )
    for candidate in candidates:
        if candidate.is_dir():
            return (
                (candidate, "/tmp/kakarot-diagnostics/PyQt6", False),
            )
    return ()


def _diagnostics_pytest_environment() -> tuple[tuple[str, str], ...]:
    """Keep test collection from trying to write inside the read-only repo."""
    environment = [
        ("HOME", "/tmp"),
        ("PYTHONDONTWRITEBYTECODE", "1"),
        ("KAKAROT_LOG_DIR", "/tmp/kakarot-diagnostics/logs"),
        ("QT_QPA_PLATFORM", "offscreen"),
    ]
    if _diagnostics_pyqt_mounts():
        environment.append(("PYTHONPATH", "/tmp/kakarot-diagnostics"))
    return tuple(environment)


def _diagnostic_section(name: str, result: dict[str, Any]) -> str:
    """Format one bounded command result for the model."""
    stdout = _truncate_middle(_strip_ansi(str(result.get("stdout") or "")))
    stderr = _truncate_middle(_strip_ansi(str(result.get("stderr") or "")))
    lines = [
        f"=== {name} ===",
        f"exit_code={result.get('returncode', '?')}",
    ]
    if result.get("error"):
        lines.append(f"error={result['error']}")
    if stdout:
        lines.append(stdout.rstrip())
    if stderr:
        lines.append("[stderr]")
        lines.append(stderr.rstrip())
    return "\n".join(lines)


def _system_command_output(result: Any) -> str:
    """Render a bounded command result as the single string the model sees.

    ``run_sandboxed_command`` already caps each stream with its ring buffer;
    this second pass strips ANSI colour and keeps a predictable head/tail shape
    if a test double or a future runner returns something larger.
    """
    if not isinstance(result, dict):
        return "Command failed: the runner returned an invalid result."
    stdout = _truncate_middle(_strip_ansi(str(result.get("stdout") or "")))
    stderr = _truncate_middle(_strip_ansi(str(result.get("stderr") or "")))
    lines = [f"exit_code: {result.get('returncode', '?')}"]
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    if stdout:
        lines.append(stdout.rstrip())
    if stderr:
        lines.append("[stderr]")
        lines.append(stderr.rstrip())
    if len(lines) == 1:
        lines.append("(no output)")
    return "\n".join(lines)


def _find_opencode_binary() -> Path | None:
    """Locate the host OpenCode CLI, including an nvm-managed install."""
    found = shutil.which("opencode")
    if found:
        return Path(found)

    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "opencode",
        home / ".bun" / "bin" / "opencode",
        home / ".npm-global" / "bin" / "opencode",
        Path("/usr/local/bin/opencode"),
        Path("/usr/bin/opencode"),
    ]
    versions = home / ".nvm" / "versions" / "node"
    if versions.is_dir():
        candidates.extend(sorted(versions.glob("*/bin/opencode"), reverse=True))
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _opencode_mounts(binary: Path) -> tuple[tuple[Path, str, bool], ...]:
    """Return the minimum host configuration needed for a delegation run."""
    home = Path.home()
    mounts: list[tuple[Path, str, bool]] = [
        (binary, OPENCODE_SANDBOX_BINARY, False),
        (binary.parent, OPENCODE_SANDBOX_NODE_BIN, False),
    ]
    for relative, writable in (
        (".config/opencode", False),
        (".config/git", False),
        (".local/share/opencode", True),
        (".local/state/opencode", True),
        (".cache/opencode", True),
        (".gitconfig", False),
    ):
        source = home / relative
        if source.exists():
            mounts.append((source, str(source), writable))
    for source in (
        Path("/etc/passwd"),
        Path("/etc/group"),
        Path("/etc/hosts"),
    ):
        if source.exists():
            mounts.append((source, str(source), False))
    return tuple(mounts)


def _normalize_opencode_files(raw_files: Any) -> list[str]:
    """Validate target paths and return repository-relative POSIX paths."""
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("target_files must be a non-empty list of paths")
    if len(raw_files) > OPENCODE_MAX_TARGET_FILES:
        raise ValueError(
            f"target_files must contain at most {OPENCODE_MAX_TARGET_FILES} paths"
        )

    root = PROJECT_ROOT.resolve()
    normalized: list[str] = []
    for raw in raw_files:
        if not isinstance(raw, str):
            raise ValueError("target_files must contain only strings")
        text = raw.strip()
        if not text or len(text) > OPENCODE_TARGET_PATH_MAX_CHARS:
            raise ValueError(
                "each target file must be 1 to "
                f"{OPENCODE_TARGET_PATH_MAX_CHARS} characters"
            )
        if "\x00" in text:
            raise ValueError("target file paths cannot contain null bytes")
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
            relative = resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                "target_files must stay inside the Kakarot project"
            ) from exc
        if not relative.parts:
            raise ValueError("target_files cannot refer to the project root")
        if resolved.exists() and not resolved.is_file():
            raise ValueError("target_files must refer to regular files")
        relative_text = relative.as_posix()
        if relative_text not in normalized:
            normalized.append(relative_text)
    return normalized


def _opencode_prompt(task_description: str, target_files: list[str]) -> str:
    prompt = f"Task: {task_description}\nFiles: {', '.join(target_files)}"
    if len(prompt) > OPENCODE_PROMPT_MAX_CHARS:
        raise ValueError(
            f"delegation prompt must be {OPENCODE_PROMPT_MAX_CHARS} characters or fewer"
        )
    return prompt


def _build_opencode_command(task_description: str, target_files: list[str]) -> str:
    """Build a shell-safe OpenCode argv for the read-only/working mount."""
    message = f"Task: {task_description}\nFiles: {', '.join(target_files)}"
    args = [
        OPENCODE_SANDBOX_BINARY,
        "run",
        "--standalone",
        "--auto",
        "--format",
        "default",
    ]
    if OPENCODE_MODEL:
        args.extend(["--model", OPENCODE_MODEL])
    root = PROJECT_ROOT.resolve()
    for relative in target_files:
        if (root / relative).is_file():
            args.extend(["--file", relative])
    args.append(message)
    return shlex.join(args)


def _stop_opencode_scope(unit: str) -> None:
    """Best-effort cleanup after a timed-out or output-capped delegation."""
    systemctl = shutil.which("systemctl")
    if not systemctl or not unit:
        return
    try:
        subprocess.run(
            [systemctl, "--user", "stop", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


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
            "description": "Schedule a local reminder for relative_days at 24h time_str.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "relative_days": {"type": "integer"},
                    "time_str": {"type": "string"}
                },
                "required": ["task"],
            },
        },
        {
            "name": "write_file",
            "description": "Create a UTF-8 text file in the home folder without overwriting.",
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
            "description": "Move top-level files in a home folder into extension folders.",
            "parameters": {
                "type": "object",
                "properties": {"target_directory": {"type": "string"}},
                "required": ["target_directory"],
            },
        },
        {
            "name": "get_system_status",
            "description": "Return CPU, RAM, disk, load, and uptime diagnostics.",
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
            "description": "Preview file grouping by extension without moving files.",
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
            "name": "execute_command",
            "description": "Run a sandboxed command; non-read-only commands require approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "maxLength": 1000}
                },
                "required": ["command"],
            },
        },
        {
            "name": "execute_system_command",
            "description": "Executes a shell command on the host OS to manage files, networks, processes, or system settings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "maxLength": MAX_COMMAND_LENGTH,
                    },
                    "explanation": {"type": "string", "maxLength": 500},
                },
                "required": ["command", "explanation"],
            },
        },
        {
            "name": "manage_audio",
            "description": "Controls system audio volume and mute state via PipeWire.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["mute", "unmute", "set_volume", "status"],
                    },
                    "volume_percent": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "manage_network",
            "description": "Checks network status and toggles Wi-Fi via NetworkManager.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "toggle_wifi"],
                    },
                    "state": {"type": "boolean"},
                },
                "required": ["action"],
            },
        },
        {
            "name": "manage_desktop_settings",
            "description": "Toggles Wayland desktop settings like dark mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "setting": {
                        "type": "string",
                        "enum": ["dark_mode", "light_mode"],
                    }
                },
                "required": ["setting"],
            },
        },
        {
            "name": "run_self_diagnostics",
            "description": "Runs unit tests and fetches recent daemon logs to diagnose system health.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "restart_daemon",
            "description": "Restarts the Kakarot daemon to apply code changes.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "delegate_to_opencode",
            "description": "Delegates a coding task or self-repair to the local OpenCode DeepSeek agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_description": {
                        "type": "string",
                        "maxLength": OPENCODE_TASK_MAX_CHARS,
                    },
                    "target_files": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "maxLength": OPENCODE_TARGET_PATH_MAX_CHARS,
                        },
                        "maxItems": OPENCODE_MAX_TARGET_FILES,
                    },
                },
                "required": ["task_description", "target_files"],
            },
        },
        {
            "name": "request_human_intervention",
            "description": "Ask the user for missing input, a decision, or a manual step.",
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
            "description": "Extract readable text from a URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
        {
            "name": "play_media",
            "description": "Play a media URL via mpv or the browser.",
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
            "description": "List recently modified indexed files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"}
                }
            },
        },
        {
            "name": "remember_fact",
            "description": "Save a fact to persistent memory.",
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
            "description": "Search persistent memory for saved facts.",
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
        "get_system_status",
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
        "execute_system_command",
        "manage_audio",
        "manage_network",
        "manage_desktop_settings",
        "run_self_diagnostics",
        "restart_daemon",
        "delegate_to_opencode",
        "request_human_intervention",
        "fetch_web_content",
        "play_media",
    )

    def __init__(self, reminders: ReminderStore) -> None:
        from kakarot_daemon.repo_bridge import RepositoryBridge

        self.repository = RepositoryBridge()
        self._reminders = reminders
        self._authorize_command: Callable[..., Awaitable[bool]] | None = None
        self._human_intervention_handler: (
            Callable[[str, str, str, str, list[str]], Awaitable[str | None]] | None
        ) = None
        self._restart_task: asyncio.Task[None] | None = None
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "schedule_reminder": self._schedule_reminder,
            "write_file": lambda args: write_file(**args),
            "organize_directory": lambda args: organize_directory(**args),
            "get_system_status": self._get_system_status,
            "find_files": self._find_files,
            "plan_file_organization": self._plan_file_organization,
            "list_due_reminders": self._list_due_reminders,
            "execute_command": self._execute_command,
            "execute_system_command": self._execute_system_command,
            "manage_audio": self._manage_audio,
            "manage_network": self._manage_network,
            "manage_desktop_settings": self._manage_desktop_settings,
            "run_self_diagnostics": self._run_self_diagnostics,
            "restart_daemon": self._restart_daemon,
            "delegate_to_opencode": self._delegate_to_opencode,
            "request_human_intervention": self._request_human_intervention,
            "fetch_web_content": self._fetch_web_content,
            "play_media": self._play_media,
            "read_screen_context": self._read_screen_context,
            "get_recent_files": self._get_recent_files,
            "remember_fact": self._remember_fact,
            "search_memory": self._search_memory,
        }

    def set_authorization_handler(
        self, handler: Callable[..., Awaitable[bool]]
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
            elif isinstance(result, str) and result.startswith("Command rejected:"):
                status = "BLOCKED BY SAFETY POLICY"
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

    def _get_system_status(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        import psutil

        disk = shutil.disk_usage(Path.home())
        memory = psutil.virtual_memory()
        load = os.getloadavg()
        return {
            "ok": True,
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "ram_percent": memory.percent,
            "ram_free_bytes": memory.available,
            "ram_total_bytes": memory.total,
            "disk": {"free_bytes": disk.free, "total_bytes": disk.total},
            "disk_free_bytes": disk.free,
            "disk_total_bytes": disk.total,
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "disk_total_gb": round(disk.total / (1024**3), 2),
            "load_1m": load[0],
            "load_5m": load[1],
            "load_15m": load[2],
            "uptime_seconds": max(0, int(time.time() - psutil.boot_time())),
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

    async def _execute_system_command(self, arguments: dict[str, Any]) -> str:
        """Run one generalized host command after an explicit approval gate.

        This is the Voice-to-Action path: unlike ``execute_command``, every
        command is treated as mutating, so there is no read-only shortcut. The
        destructive blocklist is checked before the UI is touched.
        """
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Command is required")
        command = command.strip()
        if len(command) > MAX_COMMAND_LENGTH:
            raise ValueError(
                f"Command must be {MAX_COMMAND_LENGTH} characters or fewer"
            )

        if DESTRUCTIVE_COMMAND_BLOCKLIST.search(command):
            return (
                "Command rejected: destructive command blocked by the safety "
                "policy."
            )

        explanation = arguments.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("Explanation is required")
        explanation = explanation.strip()
        if len(explanation) > 500:
            raise ValueError("Explanation must be 500 characters or fewer")

        # ``sudo`` already carries its own privilege escalation, so it gets the
        # red critical tier; ordinary mutating commands use the violet action
        # tier. Both go through the shared authorization handler.
        level = (
            "critical"
            if re.search(r"\bsudo\b", command, re.IGNORECASE)
            else "action"
        )
        if not await self._authorize_action(
            command,
            level=level,
            label="System Command Execution",
            reason=explanation,
            prompt=command,
            options=["✓ Execute", "✕ Cancel"],
        ):
            return "User denied execution"

        result = await asyncio.to_thread(
            _run_sandboxed_command, command, APPROVED_COMMAND_TIMEOUT_SECONDS
        )
        return _system_command_output(result)

    async def _authorize_desktop_action(
        self, command: str, *, label: str, reason: str
    ) -> bool:
        """Approve one fixed desktop wrapper at the violet action tier."""
        return await self._authorize_action(
            command,
            level="action",
            label=label,
            reason=reason,
            prompt=command,
            options=["✓ Apply", "✕ Cancel"],
        )

    async def _run_desktop_tool(self, command: str) -> dict[str, Any]:
        """Run a fixed desktop utility on the host through the bounded runner."""
        result = await asyncio.to_thread(
            _run_sandboxed_command,
            command,
            DESKTOP_TOOL_TIMEOUT_SECONDS,
            host=True,
        )
        if isinstance(result, dict):
            return result
        return {"ok": False, "error": "desktop utility returned an invalid result"}

    async def _manage_audio(self, arguments: dict[str, Any]) -> str:
        """Read or mutate PipeWire's default sink with ``wpctl``."""
        action = str(arguments.get("action") or "").strip().lower()
        if action not in AUDIO_ACTIONS:
            raise ValueError(
                "action must be one of mute, unmute, set_volume, or status"
            )

        if action == "status":
            command = "wpctl get-volume @DEFAULT_AUDIO_SINK@"
        elif action == "set_volume":
            volume = arguments.get("volume_percent")
            if type(volume) is not int or not 0 <= volume <= 100:
                raise ValueError(
                    "volume_percent must be an integer between 0 and 100"
                )
            command = f"wpctl set-volume @DEFAULT_AUDIO_SINK@ {volume}%"
            if not await self._authorize_desktop_action(
                command,
                label="Audio Control",
                reason=f"Set system volume to {volume}%.",
            ):
                return "User denied execution"
        else:
            muted = "1" if action == "mute" else "0"
            command = f"wpctl set-mute @DEFAULT_AUDIO_SINK@ {muted}"
            reason = (
                "Mute the system audio output."
                if action == "mute"
                else "Unmute the system audio output."
            )
            if not await self._authorize_desktop_action(
                command,
                label="Audio Control",
                reason=reason,
            ):
                return "User denied execution"

        return _system_command_output(await self._run_desktop_tool(command))

    async def _manage_network(self, arguments: dict[str, Any]) -> str:
        """Read NetworkManager status or switch the Wi-Fi radio with ``nmcli``."""
        action = str(arguments.get("action") or "").strip().lower()
        if action not in NETWORK_ACTIONS:
            raise ValueError("action must be one of status or toggle_wifi")

        if action == "status":
            command = "nmcli general status"
            return _system_command_output(await self._run_desktop_tool(command))

        state = arguments.get("state")
        if state is None:
            probe = await self._run_desktop_tool("nmcli radio wifi")
            current = str(probe.get("stdout") or "").strip().lower()
            if "enabled" in current:
                state = False
            elif "disabled" in current:
                state = True
            else:
                return "Could not determine Wi-Fi state.\n" + _system_command_output(
                    probe
                )
        if not isinstance(state, bool):
            raise ValueError("state must be true or false")

        command = f"nmcli radio wifi {'on' if state else 'off'}"
        if not await self._authorize_desktop_action(
            command,
            label="Network Control",
            reason=f"Turn the Wi-Fi radio {'on' if state else 'off'}.",
        ):
            return "User denied execution"
        return _system_command_output(await self._run_desktop_tool(command))

    async def _manage_desktop_settings(self, arguments: dict[str, Any]) -> str:
        """Switch the GNOME color scheme between dark and light with gsettings."""
        setting = str(arguments.get("setting") or "").strip().lower()
        if setting not in DESKTOP_SETTINGS:
            raise ValueError("setting must be one of dark_mode or light_mode")

        scheme = "prefer-dark" if setting == "dark_mode" else "default"
        command = (
            "gsettings set org.gnome.desktop.interface "
            f"color-scheme '{scheme}'"
        )
        if not await self._authorize_desktop_action(
            command,
            label="Desktop Settings",
            reason=f"Switch the desktop to {setting.replace('_', ' ')}.",
        ):
            return "User denied execution"
        return _system_command_output(await self._run_desktop_tool(command))

    async def _authorize_action(
        self,
        command: str,
        *,
        label: str,
        reason: str,
        prompt: str,
        options: list[str],
        level: str | None = None,
    ) -> bool:
        """Trigger the approval gate with an optional custom agency payload."""
        handler = self._authorize_command
        if handler is None:
            return False

        payload: dict[str, Any] = {
            "label": label,
            "reason": reason,
            "prompt": prompt,
            "options": options,
        }
        if level is not None:
            payload["level"] = level
        supports_payload = False
        try:
            parameters = inspect.signature(handler).parameters
            supports_payload = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ) or all(name in parameters for name in payload)
        except (TypeError, ValueError):
            supports_payload = False

        try:
            if supports_payload:
                approved = await handler(command, **payload)
            else:
                # Backward-compatible one-argument authorization handlers still
                # gate the action, even though they cannot render the custom
                # label/options.
                approved = await handler(prompt)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return False
        return approved is True

    async def _authorize_opencode(self, prompt: str) -> bool:
        """Trigger the decision gate with the delegation-specific UI payload."""
        return await self._authorize_action(
            "delegate_to_opencode",
            label="OpenCode Delegation",
            reason=(
                "Kakarot is offloading a code modification to the local "
                "DeepSeek agent."
            ),
            prompt=prompt,
            options=["✓ Approve", "✕ Reject"],
        )

    async def _authorize_daemon_restart(self) -> bool:
        """Ask for the action-tier approval required to restart the daemon."""
        return await self._authorize_action(
            "restart_daemon",
            level="action",
            label="Daemon Restart Required",
            reason=(
                "Kakarot needs to reboot to apply new code patches or clear "
                "its state."
            ),
            prompt=DAEMON_RESTART_COMMAND,
            options=["✓ Restart", "✕ Cancel"],
        )

    async def _run_self_diagnostics(
        self, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the test suite and read recent daemon logs without approval."""
        project_root = PROJECT_ROOT.resolve()
        # Pytest can allocate far more than the daemon's 192 MiB cgroup. Put it
        # in a transient scope, just like OpenCode delegation, while keeping the
        # small journal read in the ordinary sandbox.
        scope_unit = f"kakarot-diagnostics-{uuid.uuid4().hex[:12]}.scope"
        pytest_result = await asyncio.to_thread(
            _run_sandboxed_command,
            SELF_DIAGNOSTICS_PYTEST_COMMAND,
            SELF_DIAGNOSTICS_TIMEOUT_SECONDS,
            cwd="/workspace",
            extra_mounts=(
                (project_root, "/workspace", False),
                *_diagnostics_pyqt_mounts(),
            ),
            extra_env=_diagnostics_pytest_environment(),
            scope=True,
            scope_unit=scope_unit,
        )
        logs_result = await asyncio.to_thread(
            _run_sandboxed_command,
            SELF_DIAGNOSTICS_LOG_COMMAND,
            SELF_DIAGNOSTICS_LOG_TIMEOUT_SECONDS,
            extra_mounts=_diagnostics_journal_mounts(),
        )
        pytest_error = str(pytest_result.get("error") or "")
        if "timed out" in pytest_error or "2 MB" in pytest_error:
            # Killing systemd-run does not always stop the scope it created.
            await asyncio.to_thread(_stop_opencode_scope, scope_unit)

        combined = "\n\n".join(
            (
                _diagnostic_section("pytest", pytest_result),
                _diagnostic_section("journalctl", logs_result),
            )
        )
        pytest_ok = bool(pytest_result.get("ok"))
        logs_ok = bool(logs_result.get("ok"))
        return {
            # ``ok`` means the diagnostic collection ran; ``healthy`` reports
            # whether both commands succeeded. A failing test suite is useful
            # evidence, not a tool execution failure.
            "ok": True,
            "healthy": pytest_ok and logs_ok,
            "pytest_ok": pytest_ok,
            "logs_ok": logs_ok,
            "pytest": pytest_result,
            "logs": logs_result,
            "output": combined,
        }

    async def _restart_daemon(
        self, _arguments: dict[str, Any]
    ) -> str:
        """Schedule a restart only after an action-tier human approval."""
        if not await self._authorize_daemon_restart():
            return "User denied execution"

        # Never run systemctl inline: stopping this process would drop the
        # HTTP/SSE response before the model can finish its turn.  The delayed
        # task lets the answer leave first; systemd then restarts the service.
        if self._restart_task is None or self._restart_task.done():
            self._restart_task = asyncio.create_task(self._delayed_restart())
        return "Restart initiated. Kakarot will reconnect in a few seconds."

    async def _delayed_restart(self) -> None:
        """Wait for the turn to finish, then ask systemd to restart us."""
        await asyncio.sleep(DAEMON_RESTART_DELAY_SECONDS)
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "--user",
                "restart",
                DAEMON_SERVICE_UNIT,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError, TypeError):
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=15.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    async def _delegate_to_opencode(self, arguments: dict[str, Any]) -> Any:
        """Hand a bounded repair task to the local OpenCode agent."""
        task = arguments.get("task_description")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task_description is required")
        task = task.strip()
        if len(task) > OPENCODE_TASK_MAX_CHARS:
            raise ValueError(
                f"task_description must be {OPENCODE_TASK_MAX_CHARS} characters "
                "or fewer"
            )
        if "\x00" in task:
            raise ValueError("task_description cannot contain null bytes")

        target_files = _normalize_opencode_files(arguments.get("target_files"))
        prompt = _opencode_prompt(task, target_files)

        binary = _find_opencode_binary()
        if binary is None:
            return {
                "ok": False,
                "error": "OpenCode CLI is not installed or is not executable.",
            }

        if not await self._authorize_opencode(prompt):
            return "User denied execution"

        command = _build_opencode_command(task, target_files)
        scope_unit = f"kakarot-opencode-{uuid.uuid4().hex[:12]}.scope"
        result = await asyncio.to_thread(
            run_sandboxed_command,
            command,
            timeout=OPENCODE_DELEGATION_TIMEOUT_SECONDS,
            allow_network=True,
            cwd="/workspace",
            writable_workspace=PROJECT_ROOT.resolve(),
            extra_mounts=_opencode_mounts(binary),
            extra_env=(
                (
                    "PATH",
                    f"{OPENCODE_SANDBOX_NODE_BIN}:/usr/local/bin:/usr/bin:/bin",
                ),
                ("NO_COLOR", "1"),
                ("GIT_TERMINAL_PROMPT", "0"),
                ("XDG_RUNTIME_DIR", "/tmp/opencode-runtime"),
            ),
            scope=True,
            scope_unit=scope_unit,
        )
        if not isinstance(result, dict):
            return {"ok": False, "error": "OpenCode returned an invalid result"}
        if not result.get("ok"):
            # Killing systemd-run does not necessarily stop the scope it
            # created, so explicitly reap a timed-out or output-capped run.
            await asyncio.to_thread(_stop_opencode_scope, scope_unit)
            result.setdefault(
                "error",
                f"OpenCode exited with code {result.get('returncode', -1)}",
            )
        for stream in ("stdout", "stderr"):
            if isinstance(result.get(stream), str):
                result[stream] = _strip_ansi(result[stream])
        result.setdefault("delegated_to", "opencode")
        result.setdefault("files", target_files)
        if OPENCODE_MODEL:
            result.setdefault("model", OPENCODE_MODEL)
        return result

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
