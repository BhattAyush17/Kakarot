"""Single Bubblewrap sandbox builder and runner for Kakarot.

Every command path (the agent's ``execute_command`` tool, front-end job
execution, and the standalone sandbox diagnostics) must use this module so a
test cannot pass against a different policy than the daemon runs.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import shlex
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

# Paths needed to execute binaries and resolve TLS certificates.  The host
# ``/etc`` tree is deliberately never bound wholesale: it contains shadow,
# keyring, NetworkManager, SSH, and other secrets.
SAFE_SYSTEM_ROOTS = ("/usr", "/bin", "/lib", "/lib64")
SAFE_SYSTEM_PATHS = (
    "/etc/alternatives",
    "/etc/resolv.conf",
    "/etc/ssl",
    "/etc/ca-certificates",
)
# The process may emit an unbounded amount of text. These are hard safety
# limits, not formatting preferences: the runner kills the process at the byte
# ceiling, and each stream keeps only a small head, a rolling line tail, and a
# short exact copy for genuinely small outputs.
MAX_STREAM_BYTES = 2 * 1024 * 1024
STREAM_CHUNK_BYTES = 8192
SMALL_OUTPUT_LIMIT = 2000
HEAD_CHAR_LIMIT = 1000
TAIL_LINE_LIMIT = 25
TAIL_CHAR_LIMIT = 1000
MAX_LINE_CHARS = 2000
OUTPUT_TRUNCATION_MARKER = (
    "\n...[TRUNCATED: output exceeded memory/token safety limits]...\n"
)


class _StreamCapture:
    """Bounded per-stream text accumulator.

    ``small`` is only retained until the stream proves it is not small. The
    head is the first 1,000 characters, and ``tail`` is a 25-line rolling
    window. The tail is also character-capped so one pathological line cannot
    re-inflate the payload.
    """

    def __init__(self) -> None:
        self._head_parts: list[str] = []
        self._head_len = 0
        self._small_parts: list[str] = []
        self._small_len = 0
        self._truncated = False
        self._tail: deque[str] = deque(maxlen=TAIL_LINE_LIMIT)
        self._tail_chars = 0
        self._line_parts: list[str] = []
        self._line_len = 0

    def feed(self, text: str) -> None:
        """Consume one decoded chunk without retaining the middle."""
        if not text:
            return

        if self._head_len < HEAD_CHAR_LIMIT:
            take = min(HEAD_CHAR_LIMIT - self._head_len, len(text))
            self._head_parts.append(text[:take])
            self._head_len += take

        if not self._truncated:
            if self._small_len + len(text) <= SMALL_OUTPUT_LIMIT:
                self._small_parts.append(text)
                self._small_len += len(text)
            else:
                # We now know this stream is not small. Drop the exact copy;
                # the head/tail ring already has what the model needs.
                self._truncated = True
                self._small_parts.clear()
                self._small_len = 0

        self._feed_lines(text)

    def finish(self) -> None:
        """Flush a trailing partial line after EOF or a kill."""
        if self._line_parts:
            self._flush_line()

    def render(self) -> str:
        """Return exact small output or the bounded head/marker/tail form."""
        if not self._truncated:
            return "".join(self._small_parts)
        head = "".join(self._head_parts)
        tail = "".join(self._tail)
        return head + OUTPUT_TRUNCATION_MARKER + tail

    @property
    def truncated(self) -> bool:
        return self._truncated

    def _feed_lines(self, text: str) -> None:
        start = 0
        while start < len(text):
            newline = text.find("\n", start)
            if newline < 0:
                self._append_line_piece(text[start:])
                break
            self._append_line_piece(text[start : newline + 1])
            self._flush_line()
            start = newline + 1

    def _append_line_piece(self, piece: str) -> None:
        remaining = piece
        while remaining:
            space = MAX_LINE_CHARS - self._line_len
            if space <= 0:
                self._flush_line(long_line=True)
                continue
            take = remaining[:space]
            self._line_parts.append(take)
            self._line_len += len(take)
            remaining = remaining[space:]
            if remaining and self._line_len >= MAX_LINE_CHARS:
                self._flush_line(long_line=True)

    def _flush_line(self, *, long_line: bool = False) -> None:
        line = "".join(self._line_parts)
        self._line_parts.clear()
        self._line_len = 0
        if not line:
            return
        if long_line or len(line) > TAIL_CHAR_LIMIT:
            # The head already holds the beginning; the tail only needs the
            # most recent end of a pathological line.
            line = line[-TAIL_CHAR_LIMIT:]
        if self._tail.maxlen is not None and len(self._tail) == self._tail.maxlen:
            self._tail_chars -= len(self._tail.popleft())
        self._tail.append(line)
        self._tail_chars += len(line)
        while self._tail_chars > TAIL_CHAR_LIMIT and len(self._tail) > 1:
            self._tail_chars -= len(self._tail.popleft())
        if self._tail_chars > TAIL_CHAR_LIMIT and self._tail:
            line = self._tail.pop()[-TAIL_CHAR_LIMIT:]
            self._tail.append(line)
            self._tail_chars = len(line)


def _truncate_output(text: str, limit: int = SMALL_OUTPUT_LIMIT) -> str:
    """Keep the command header and the final error, not the repeated middle."""
    if len(text) <= limit:
        return text
    head = min(HEAD_CHAR_LIMIT, limit // 2)
    tail = min(TAIL_CHAR_LIMIT, limit - head)
    return text[:head] + OUTPUT_TRUNCATION_MARKER + text[-tail:]


def allowed_document_roots() -> tuple[Path, ...]:
    """Return the only host document folders exposed read-only to commands."""
    home = Path.home()
    return tuple(
        path
        for path in (home / "Documents", home / "Downloads", home / "Desktop")
        if path.exists()
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _add_readonly_path(invocation: list[str], path: str) -> None:
    source = Path(path)
    if source.exists():
        invocation.extend(["--ro-bind", path, path])


def _add_parent_dirs(invocation: list[str], destination: Path) -> None:
    """Create destination parents inside the otherwise empty sandbox root."""
    parents: list[Path] = []
    current = destination.parent
    while current != current.parent:
        parents.append(current)
        current = current.parent
    for parent in reversed(parents):
        invocation.extend(["--dir", str(parent)])


def _add_extra_mount(
    invocation: list[str],
    source: Path,
    destination: str,
    writable: bool,
) -> None:
    """Bind an explicitly approved host path into the sandbox."""
    resolved_source = Path(source).resolve()
    destination_path = Path(destination)
    if not resolved_source.exists() or not destination_path.is_absolute():
        return
    _add_parent_dirs(invocation, destination_path)
    mode = "--bind" if writable else "--ro-bind"
    invocation.extend([mode, str(resolved_source), str(destination_path)])


def build_bwrap_invocation(
    command: str,
    *,
    allow_network: bool = False,
    cwd: str = "/",
    allowed_roots: tuple[Path, ...] | None = None,
    runtime_dir: str | None = None,
    writable_workspace: Path | None = None,
    extra_mounts: tuple[tuple[Path, str, bool], ...] = (),
    extra_env: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    """Build the exact Bubblewrap argv used by both tests and runtime.

    The sandbox starts with an empty root.  Only the approved system paths are
    bound read-only, the entire home directory is replaced by a tmpfs, and the
    three safe document folders are re-exposed read-only.  ``/run/user`` and a
    custom ``$XDG_RUNTIME_DIR`` are masked so a command cannot read daemon
    sockets, keyring files, or other session secrets.

    ``writable_workspace`` and ``extra_mounts`` are reserved for the explicitly
    approved OpenCode delegation path.  They are never used by ordinary shell
    commands, so the default policy remains read-only.
    """
    bwrap = shutil.which("bwrap") or "bwrap"
    home = Path.home()
    roots = allowed_document_roots() if allowed_roots is None else tuple(allowed_roots)
    invocation = [bwrap, "--die-with-parent"]

    for path in SAFE_SYSTEM_ROOTS:
        _add_readonly_path(invocation, path)
    for path in SAFE_SYSTEM_PATHS:
        if Path(path).exists():
            _add_readonly_path(invocation, path)
        else:
            # Keep the explicit policy visible on distributions that use a
            # different CA bundle layout.  bwrap ignores absent --*-try paths.
            invocation.extend(["--ro-bind-try", path, path])

    invocation.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])

    # The home directory is always masked, regardless of whether it lives under
    # /home, /var/home, or another location.
    invocation.extend(["--tmpfs", str(home)])
    for root in roots:
        resolved_root = Path(root).resolve()
        if not resolved_root.exists() or not _is_within(resolved_root, home):
            continue
        invocation.extend(
            ["--dir", str(resolved_root), "--ro-bind", str(resolved_root), str(resolved_root)]
        )

    # The code-delegation path mounts the repository at /workspace.  This is the
    # only writable host tree any sandboxed command receives, and it is reached
    # only after a decision-tier approval in ToolRegistry.
    if writable_workspace is not None:
        workspace = Path(writable_workspace).resolve()
        if workspace.is_dir():
            invocation.extend(
                ["--dir", "/workspace", "--bind", str(workspace), "/workspace"]
            )

    for source, destination, writable in extra_mounts:
        _add_extra_mount(invocation, Path(source), str(destination), writable)

    # Session runtime state is never needed by a shell command.  Mask the whole
    # XDG runtime tree and, defensively, a custom XDG_RUNTIME_DIR outside it.
    if Path("/run/user").exists():
        invocation.extend(["--tmpfs", "/run/user"])
    custom_runtime = runtime_dir
    if custom_runtime is None:
        custom_runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if custom_runtime:
        runtime_path = Path(custom_runtime)
        if (
            runtime_path.is_absolute()
            and not _is_within(runtime_path, home)
            and not _is_within(runtime_path, Path("/run/user"))
            and not _is_within(runtime_path, Path("/tmp"))
        ):
            invocation.extend(["--tmpfs", str(runtime_path)])

    # Preserve the old unshare-all isolation by default.  The network-enabled
    # diagnostic path still gets a private user, PID, IPC, and UTS namespace.
    if allow_network:
        invocation.extend(
            ["--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
        )
    else:
        invocation.append("--unshare-all")

    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    environment.update({str(name): str(value) for name, value in extra_env})

    invocation.extend(
        [
            "--uid",
            str(os.getuid()),
            "--gid",
            str(os.getgid()),
            "--cap-drop",
            "ALL",
            "--clearenv",
        ]
    )
    for name, value in environment.items():
        invocation.extend(["--setenv", name, value])
    invocation.extend(["--chdir", cwd, "--", "bash", "-c", command])
    return invocation


def _host_desktop_environment() -> dict[str, str]:
    """Return the minimum session environment desktop utilities need.

    These tools are not general shell escapes: they reach the user's own
    PipeWire, NetworkManager, and dconf services. The environment is rebuilt
    from a small allowlist so daemon secrets never leak into a child.
    """
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        environment["XDG_RUNTIME_DIR"] = runtime_dir
    session_bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not session_bus and runtime_dir:
        session_bus = f"unix:path={runtime_dir}/bus"
    if session_bus:
        environment["DBUS_SESSION_BUS_ADDRESS"] = session_bus
    for name in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_TYPE",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _run_bounded_process(
    invocation: Sequence[str],
    timeout: int,
    *,
    environment: dict[str, str],
    cwd: str = "/",
) -> dict[str, Any]:
    """Run one argv and drain both pipes through the bounded ring buffers."""
    try:
        process = subprocess.Popen(
            list(invocation),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=environment,
            cwd=cwd,
        )
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:200]}

    stdout_capture = _StreamCapture()
    stderr_capture = _StreamCapture()
    total_bytes = 0
    bytes_lock = threading.Lock()
    stop = threading.Event()
    limit_exceeded = False

    def pump(stream: Any, capture: _StreamCapture) -> None:
        nonlocal total_bytes, limit_exceeded
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while not stop.is_set():
                try:
                    chunk = stream.read(STREAM_CHUNK_BYTES)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                with bytes_lock:
                    total_bytes += len(chunk)
                    over_limit = total_bytes > MAX_STREAM_BYTES
                text = decoder.decode(chunk)
                if text:
                    capture.feed(text)
                if over_limit:
                    with bytes_lock:
                        limit_exceeded = True
                    stop.set()
                    with suppress(ProcessLookupError, OSError):
                        process.kill()
                    break
        finally:
            trailing = decoder.decode(b"", final=True)
            if trailing:
                capture.feed(trailing)
            capture.finish()
            with suppress(OSError):
                stream.close()

    readers = [
        threading.Thread(
            target=pump,
            args=(process.stdout, stdout_capture),
            name="kakarot-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=pump,
            args=(process.stderr, stderr_capture),
            name="kakarot-stderr",
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill first, but let the readers drain whatever the process already
        # wrote before the pipe reaches EOF. Setting ``stop`` here would throw
        # away that partial output.
        with suppress(ProcessLookupError, OSError):
            process.kill()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    # On a normal exit let the readers drain the pipe to EOF. On a kill the
    # stop event is already set, so this join is only cleanup.
    for reader in readers:
        reader.join(timeout=2.0)
    if any(reader.is_alive() for reader in readers):
        stop.set()
        with suppress(ProcessLookupError, OSError):
            process.kill()
        for reader in readers:
            reader.join(timeout=1.0)

    stdout = stdout_capture.render()
    stderr = stderr_capture.render()
    if timed_out:
        return {
            "ok": False,
            "error": f"Command timed out after {timeout} seconds.",
            "stdout": stdout,
            "stderr": stderr,
            "returncode": -1,
        }
    if limit_exceeded:
        return {
            "ok": False,
            "error": "Command output exceeded the 2 MB safety limit.",
            "stdout": stdout,
            "stderr": stderr,
            "returncode": -1,
        }
    return {
        "ok": process.returncode == 0,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": process.returncode,
    }


def run_host_command(
    command: str,
    timeout: int = 10,
) -> dict[str, Any]:
    """Run one validated desktop utility on the host with bounded output.

    The argument is parsed into argv and never passed through a shell. This is
    intentionally narrow: callers are the fixed ``wpctl``, ``nmcli``, and
    ``gsettings`` wrappers, which need the user's live session services that
    the Bubblewrap sandbox deliberately masks.
    """
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return {"ok": False, "error": f"Invalid desktop command: {exc}"}
    if not argv:
        return {"ok": False, "error": "Command is required."}
    return _run_bounded_process(
        argv,
        timeout,
        environment=_host_desktop_environment(),
        cwd=str(Path.home()),
    )


def run_sandboxed_command(
    command: str,
    timeout: int = 15,
    *,
    allow_network: bool = False,
    cwd: str = "/",
    allowed_roots: tuple[Path, ...] | None = None,
    runtime_dir: str | None = None,
    writable_workspace: Path | None = None,
    extra_mounts: tuple[tuple[Path, str, bool], ...] = (),
    extra_env: tuple[tuple[str, str], ...] = (),
    scope: bool = False,
    scope_unit: str | None = None,
    host: bool = False,
) -> dict[str, Any]:
    """Execute one command through the shared sandbox builder.

    Both pipes are drained concurrently by reader threads into bounded
    per-stream captures. A command that emits more than ``MAX_STREAM_BYTES``
    of combined output is killed, and no full payload is ever retained.

    ``scope`` runs Bubblewrap in a transient user scope.  That keeps a child
    such as the OpenCode CLI outside the daemon's 192 MiB cgroup while the
    bounded reader threads still own its output.

    ``host`` is reserved for the fixed desktop-control wrappers. Those
    utilities must reach the user's PipeWire, NetworkManager, and dconf
    services, which the default sandbox intentionally masks. The command is
    split into argv and never passed through a shell.
    """
    if host:
        return run_host_command(command, timeout=timeout)
    if shutil.which("bwrap") is None:
        return {
            "ok": False,
            "error": "Bubblewrap (bwrap) is not installed.",
        }

    invocation = build_bwrap_invocation(
        command,
        allow_network=allow_network,
        cwd=cwd,
        allowed_roots=allowed_roots,
        runtime_dir=runtime_dir,
        writable_workspace=writable_workspace,
        extra_mounts=extra_mounts,
        extra_env=extra_env,
    )
    if scope:
        systemd_run = shutil.which("systemd-run")
        if systemd_run is None:
            return {
                "ok": False,
                "error": "systemd-run is required for memory-isolated delegation.",
            }
        scope_command = [
            systemd_run,
            "--user",
            "--scope",
            "--collect",
            "--quiet",
        ]
        if scope_unit:
            scope_command.append(f"--unit={scope_unit}")
        invocation = scope_command + invocation

    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    if scope:
        # systemd-run itself talks to the user manager before Bubblewrap starts.
        host_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if host_runtime_dir:
            environment["XDG_RUNTIME_DIR"] = host_runtime_dir
        session_bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        if not session_bus and host_runtime_dir:
            session_bus = f"unix:path={host_runtime_dir}/bus"
        if session_bus:
            environment["DBUS_SESSION_BUS_ADDRESS"] = session_bus
    return _run_bounded_process(invocation, timeout, environment=environment, cwd="/")


async def execute_sandboxed(
    command: str,
    allow_network: bool = False,
    workdir: Path | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Async compatibility wrapper around the shared synchronous runner."""
    cwd = "/"
    if workdir is not None:
        requested = Path(workdir).resolve()
        if not any(_is_within(requested, root.resolve()) for root in allowed_document_roots()):
            return {"ok": False, "error": "workdir is outside the permitted user folders"}
        if not requested.is_dir():
            return {"ok": False, "error": "workdir does not exist"}
        cwd = str(requested)
    return await asyncio.to_thread(
        run_sandboxed_command,
        command,
        timeout,
        allow_network=allow_network,
        cwd=cwd,
    )
