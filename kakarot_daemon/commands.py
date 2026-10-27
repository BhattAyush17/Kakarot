"""Transport-agnostic command dispatch for the daemon control plane.

The command vocabulary is shared by every transport: the private NDJSON
UNIX socket (:mod:`kakarot_daemon.control`) and the session D-Bus interface
``io.github.kakarot.Daemon`` (:mod:`kakarot_daemon.dbus_service`).  Keeping the
dispatcher here means a new transport cannot grow its own, subtly different
set of commands.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

LOGGER = logging.getLogger(__name__)


class ExecuteJobs:
    """Bounded job table for sandboxed command execution.

    ``CommandRouter.handle`` is synchronous and the D-Bus transport dispatches
    on the bus thread, so awaiting a subprocess there would stall every other
    D-Bus caller for the length of the run.  ``execute`` therefore schedules the
    work on the daemon's own loop and answers with a job id; the client then
    polls ``result``.

    The work itself is delegated to ``ToolRegistry.execute("execute_command")``
    so the AST gate (:mod:`kakarot_daemon.security`), the Bubblewrap sandbox
    (:mod:`kakarot_daemon.sandbox`) and the audit trail all stay in the daemon.
    A front end can therefore never widen its own permissions.
    """

    MAX_JOBS = 8
    MAX_OUTPUT = 4000

    def __init__(
        self,
        runner: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """
        :param runner: ``(tool_name, arguments) -> awaitable result``.
        :param loop: The daemon's running loop, used to schedule from any thread.
        """
        self._runner = runner
        self._loop = loop
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._counter = 0

    def start(self, shell: str) -> dict[str, Any]:
        """Schedule ``shell`` and return the job ticket to poll."""
        self._counter += 1
        job = f"exec-{self._counter}"
        entry: dict[str, Any] = {"job": job, "command": shell, "done": False}
        self._jobs[job] = entry
        while len(self._jobs) > self.MAX_JOBS:
            self._jobs.popitem(last=False)
        # run_coroutine_threadsafe is the only schedule path safe from the
        # D-Bus bus thread, which has no running loop of its own.
        asyncio.run_coroutine_threadsafe(self._run(job, shell), self._loop)
        return {"job": job}

    def poll(self, job: str) -> dict[str, Any] | None:
        entry = self._jobs.get(job)
        return dict(entry) if entry else None

    async def _run(self, job: str, shell: str) -> None:
        from kakarot_daemon.scrubber import sanitize_text

        try:
            result = await self._runner("execute_command", {"command": shell})
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the loop
            result = {"ok": False, "error": str(exc)[:200]}
        if not isinstance(result, dict):
            result = {"ok": False, "error": "malformed result"}

        entry = self._jobs.get(job)
        if entry is None:
            return  # evicted; the client will get "unknown or expired job"
        awaiting = result.get("error") == "AWAITING_CONFIRMATION"
        # Command output can contain anything on the host, so it leaves through
        # the same privacy filter as every other outbound string.
        stdout = sanitize_text(str(result.get("stdout") or ""))
        stderr = sanitize_text(str(result.get("stderr") or ""))
        self._jobs[job] = {
            **entry,
            "done": True,
            "ok": bool(result.get("ok")),
            "requires_confirmation": awaiting,
            "reason": str(result.get("reason") or result.get("error") or ""),
            "stdout": stdout[: self.MAX_OUTPUT],
            "stderr": stderr[: self.MAX_OUTPUT],
            "returncode": result.get("returncode"),
        }


class CommandRouter:
    """Validate and run one control command, returning the daemon snapshot.

    :param mute: Called with the new microphone gate, ``True`` to mute.
    :param status: Returns the current snapshot; owned by the coordinator.
    :param stop: Requests daemon shutdown.
    :param jobs: Enables ``execute``/``result``; absence rejects them.
    :param extra: Handles ``reload``/``speaker_on``/``speaker_off``/
        ``ack_reminder:<id>``; absence rejects them.
    """

    def __init__(
        self,
        *,
        mute: Callable[[bool], None],
        status: Callable[[], dict[str, Any]],
        stop: Callable[[], None],
        jobs: ExecuteJobs | None = None,
        extra: Callable[[str], None] | None = None,
    ) -> None:
        self._mute = mute
        self._status = status
        self._stop = stop
        self._jobs = jobs
        self._extra = extra

    def handle(
        self, command: str, request: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run one command and answer with the snapshot it produced."""
        extra: dict[str, Any] = {}
        if command == "mute":
            self._mute(True)
        elif command == "unmute":
            self._mute(False)
        elif command == "status":
            # Snapshot already carries ``phase`` (e.g. IDLE_MUTED); also
            # echo it as ``state`` so callers using either key find it.
            snap = self._status()
            extra["state"] = snap.get("phase", "UNKNOWN")
        elif command in ("request_confirmation", "trigger_hitl"):
            # HITL audit probe: acknowledge the request and report the
            # consent-gate state without actually blocking the loop.
            tool = (request or {}).get("tool", "unknown")
            extra["state"] = "AWAITING_CONFIRMATION"
            extra["tool"] = tool
        elif command == "resolve_action":
            approved = (request or {}).get("approved", False)
            if not approved:
                LOGGER.info("HITL action denied by user.")
            extra["state"] = self._status().get("phase", "IDLE_MUTED")
        elif command == "history":
            from services.chat_bus import get_history

            messages = get_history()[-20:]
            while messages and len(json_bytes(messages)) > 48000:
                messages.pop(0)
            extra["messages"] = messages
        elif command == "clear_history":
            from services.chat_bus import clear_history

            clear_history()
        elif command == "stop":
            self._stop()
            extra["stopping"] = True
        elif command == "execute":
            extra.update(self._start_execute(request or {}))
        elif command == "result":
            extra.update(self._poll_result(request or {}))
        elif command in ("reload", "speaker_on", "speaker_off"):
            if self._extra is None:
                return {"ok": False, "error": "unknown command"}
            self._extra(command)
        elif command.startswith("ack_reminder:"):
            if self._extra is None:
                return {"ok": False, "error": "unknown command"}
            self._extra(command)
        else:
            return {"ok": False, "error": "unknown command"}
        return {"ok": True, **extra, **self._status()}

    def _start_execute(self, request: dict[str, Any]) -> dict[str, Any]:
        """Hand a shell command to the sandbox and return its job ticket."""
        if self._jobs is None:
            return {"error": "command execution is unavailable"}
        # ``command`` is the router verb in the legacy protocol, so only an
        # explicit ``shell`` field may carry executable text.
        shell = str(request.get("shell") or "").strip()
        if not shell:
            return {"error": "missing shell command"}
        return {"state": "EXECUTING", **self._jobs.start(shell)}

    def _poll_result(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the finished sandbox result for one ``execute`` job."""
        if self._jobs is None:
            return {"error": "command execution is unavailable"}
        entry = self._jobs.poll(str(request.get("job") or ""))
        if entry is None:
            return {"error": "unknown or expired job"}
        return entry


def json_bytes(value: Any) -> bytes:
    """Byte length of ``value`` encoded the way every transport sends it."""
    import json

    return json.dumps(value).encode()
