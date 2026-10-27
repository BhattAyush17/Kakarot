"""Single Bubblewrap sandbox builder and runner for Kakarot.

Every command path (the agent's ``execute_command`` tool, front-end job
execution, and the standalone sandbox diagnostics) must use this module so a
test cannot pass against a different policy than the daemon runs.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
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
MAX_OUTPUT = 4000


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


def build_bwrap_invocation(
    command: str,
    *,
    allow_network: bool = False,
    cwd: str = "/",
    allowed_roots: tuple[Path, ...] | None = None,
    runtime_dir: str | None = None,
) -> list[str]:
    """Build the exact Bubblewrap argv used by both tests and runtime.

    The sandbox starts with an empty root.  Only the approved system paths are
    bound read-only, the entire home directory is replaced by a tmpfs, and the
    three safe document folders are re-exposed read-only.  ``/run/user`` and a
    custom ``$XDG_RUNTIME_DIR`` are masked so a command cannot read daemon
    sockets, keyring files, or other session secrets.
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

    invocation.extend(
        [
            "--uid",
            str(os.getuid()),
            "--gid",
            str(os.getgid()),
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "HOME",
            str(home),
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "TERM",
            "dumb",
            "--chdir",
            cwd,
            "--",
            "bash",
            "-c",
            command,
        ]
    )
    return invocation


def _decoded_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def run_sandboxed_command(
    command: str,
    timeout: int = 15,
    *,
    allow_network: bool = False,
    cwd: str = "/",
    allowed_roots: tuple[Path, ...] | None = None,
    runtime_dir: str | None = None,
) -> dict[str, Any]:
    """Execute one command through the shared sandbox builder."""
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
    )
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    try:
        completed = subprocess.run(
            invocation,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
            cwd="/",
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": f"Command timed out after {timeout} seconds.",
            "stdout": _decoded_output(exc.stdout)[:2000],
            "stderr": _decoded_output(exc.stderr)[:2000],
            "returncode": -1,
        }
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:200]}

    stdout = _decoded_output(completed.stdout)
    stderr = _decoded_output(completed.stderr)
    if len(stdout) + len(stderr) > MAX_OUTPUT:
        if len(stdout) > 2000:
            stdout = stdout[:2000] + "\n[... truncated]"
        if len(stderr) > 2000:
            stderr = stderr[:2000] + "\n[... truncated]"
    return {
        "ok": completed.returncode == 0,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": completed.returncode,
    }


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
