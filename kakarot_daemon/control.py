"""Private NDJSON control and state-event socket for the daemon."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from kakarot_daemon.commands import CommandRouter, ExecuteJobs

MAX_LINE_BYTES = 8192
HEARTBEAT_SECONDS = 5.0
AUTH_RESPONSE_TIMEOUT_SECONDS = 60.0
_LOG = logging.getLogger(__name__)


def resolve_socket_path(path: Path | None = None) -> Path:
    """Resolve the per-user daemon socket, honoring the XDG runtime directory."""
    if path is not None:
        return Path(path)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    base = Path(runtime_dir) if runtime_dir else Path("/run/user/1000")
    return base / "kakarot" / "kakarot.sock"


class ControlServer:
    """Serve one-shot commands and long-lived ``subscribe`` clients."""

    def __init__(
        self,
        path: Path,
        *,
        mute: Callable[[bool], None],
        status: Callable[[], dict[str, Any]],
        stop: Callable[[], None],
        jobs: ExecuteJobs | None = None,
        extra: Callable[[str], None] | None = None,
        manual_input: Callable[[str], Awaitable[str | None]] | None = None,
        force_listen: Callable[[], Any] | None = None,
        sleep_mic: Callable[[], Any] | None = None,
        audio_control: Callable[[bool], Any] | None = None,
    ) -> None:
        """
        :param mute: Called with the new microphone gate, ``True`` to mute.
        :param status: Returns the current snapshot; owned by the coordinator.
        :param stop: Requests daemon shutdown.
        :param jobs: Enables ``execute``/``result``; absence rejects them.
        :param extra: Handles ``reload``/``speaker_on``/``speaker_off``/
            ``ack_reminder:<id>``; return ``None`` to reject them.
        """
        self._path = resolve_socket_path(path)
        self._router = CommandRouter(
            mute=mute, status=status, stop=stop, jobs=jobs, extra=extra
        )
        self._status = status
        self._manual_input = manual_input
        self._force_listen = force_listen
        self._sleep_mic = sleep_mic
        self._audio_control = audio_control
        self._server: asyncio.AbstractServer | None = None
        self._subscribers: set[asyncio.StreamWriter] = set()
        self._clients: set[asyncio.StreamWriter] = set()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._authorization_waiters: dict[str, asyncio.Future[str | None]] = {}

    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._path.parent.chmod(0o700)
        if self._path.exists():
            try:
                if not stat.S_ISSOCK(self._path.lstat().st_mode):
                    raise RuntimeError(f"Refusing to replace non-socket path: {self._path}")
                _reader, writer = await asyncio.open_unix_connection(str(self._path))
            except (ConnectionRefusedError, FileNotFoundError):
                self._path.unlink(missing_ok=True)
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError("Voice service is already running")
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self._path), limit=MAX_LINE_BYTES
        )
        self._path.chmod(0o600)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def close(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        for waiter in tuple(self._authorization_waiters.values()):
            if not waiter.done():
                waiter.set_result(None)
        self._authorization_waiters.clear()
        if self._server:
            self._server.close()
        # Aborting the transports makes every handler see EOF, so the 3.12+
        # wait_closed() below cannot hang on a client that never sent EOF.
        for writer in (*self._clients, *self._subscribers):
            with contextlib.suppress(Exception):
                writer.close()
        self._subscribers.clear()
        self._clients.clear()
        if self._server:
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        self._path.unlink(missing_ok=True)

    def state_changed(self, snapshot: dict[str, Any]) -> None:
        """Push a new snapshot to subscribers.

        Called by the state coordinator, which only ever publishes from the
        event-loop thread, so ``create_task`` is safe here.  Reporters that run
        elsewhere (the PortAudio callback) must be marshalled by the coordinator
        first rather than calling this directly.
        """
        if self._subscribers:
            asyncio.create_task(self._broadcast({"type": "state", "state": snapshot}))

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if self._subscribers:
                await self._broadcast(
                    {
                        "type": "heartbeat",
                        "state_version": self._status().get("state_version", 0),
                    }
                )

    async def _broadcast(self, event: dict[str, Any]) -> None:
        payload = self._encode(event)
        failed: list[asyncio.StreamWriter] = []
        for writer in tuple(self._subscribers):
            try:
                writer.write(payload)
                # Bound the drain so a stalled subscriber cannot wedge the loop
                await asyncio.wait_for(writer.drain(), timeout=2.0)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                failed.append(writer)
        for writer in failed:
            self._subscribers.discard(writer)
            writer.close()

    async def broadcast(self, msg_type: str, payload: dict[str, Any]) -> None:
        """Send one core-originated event to every connected subscriber."""
        await self._broadcast(
            {"sender": "core", "type": msg_type, "payload": payload}
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Track every connection, not just subscribers: Python 3.12+ waits for
        # all handlers in wait_closed(), so an untracked client that never sent
        # EOF would hang server shutdown.
        self._clients.add(writer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ConnectionError, OSError, ValueError):
                    break
                if not line:
                    break
                try:
                    decoded = line.decode("utf-8", errors="strict").strip()
                except UnicodeDecodeError:
                    decoded = ""
                if not decoded:
                    continue
                try:
                    request = json.loads(decoded)
                    if not isinstance(request, dict):
                        request = None
                except json.JSONDecodeError:
                    request = None
                if request is None:
                    # A malformed line must still be ANSWERED: a one-shot client
                    # waiting for a reply would otherwise hang on its read. The
                    # connection stays open so later NDJSON lines remain usable.
                    writer.write(
                        self._encode(
                            {"type": "response", "ok": False, "error": "invalid request"}
                        )
                    )
                    with contextlib.suppress(ConnectionError, OSError):
                        await writer.drain()
                    continue
                _LOG.info(
                    "UDS NDJSON event sender=%s type=%s",
                    request.get("sender", "legacy"),
                    request.get("type", request.get("command", "unknown")),
                )
                if "command" in request and "type" not in request:
                    command = str(request.get("command") or "")
                    if command == "subscribe":
                        await self._subscribe(reader, writer)
                        return
                response = await self.dispatch_message(request)
                writer.write(self._encode({"type": "response", **response}))
                with contextlib.suppress(ConnectionError, OSError):
                    await writer.drain()
        except (UnicodeDecodeError, ConnectionError, OSError):
            # Malformed or disconnected clients cannot take down the server.
            pass
        finally:
            self._clients.discard(writer)
            # ``_subscribe`` may have already closed the writer; touching an
            # aborted transport twice surfaces BrokenPipeError in the loop log.
            if not writer.is_closing():
                with contextlib.suppress(ConnectionError, OSError):
                    writer.close()
                    await writer.wait_closed()

    async def dispatch_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Dispatch legacy commands and typed client-to-core NDJSON events."""
        if "command" in message and "type" not in message:
            return self._command(str(message.get("command") or ""), message)

        sender = message.get("sender")
        msg_type = message.get("type")
        payload = message.get("payload")
        if not isinstance(sender, str) or not isinstance(payload, dict):
            return {"ok": False, "error": "invalid event envelope"}

        if msg_type in {"auth_response", "human_response"}:
            return self._handle_auth_response(payload)

        if msg_type == "audio_control":
            speaker_muted = payload.get("speaker_muted")
            if not isinstance(speaker_muted, bool):
                return {"ok": False, "error": "invalid audio control state"}
            if self._audio_control is None:
                return {"ok": False, "error": "speaker control is unavailable"}
            result = self._audio_control(speaker_muted)
            if hasattr(result, "__await__"):
                await result
            return {"ok": True, **self._status()}

        if msg_type == "manual_input":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                return {"ok": False, "error": "invalid manual input"}
            if self._manual_input is None:
                return {"ok": False, "error": "manual input is unavailable"}
            await self.broadcast("ui_say", {"text": "Working on that…", "status": "busy"})
            try:
                answer = await self._manual_input(text.strip())
            except Exception as exc:
                message_text = str(exc)[:200] or "Manual input failed."
                await self._ui_say(message_text, "error")
                return {"ok": False, "error": message_text}
            if answer:
                await self._ui_say(str(answer), "idle")
            return {"ok": True}

        if msg_type == "force_listen":
            if self._force_listen is None:
                return {"ok": False, "error": "audio wake is unavailable"}
            try:
                result = self._force_listen()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                message_text = str(exc)[:200] or "Unable to start listening."
                await self._ui_say(message_text, "error")
                return {"ok": False, "error": message_text}
            return {"ok": True}

        if msg_type == "system":
            action = payload.get("action")
            if action != "sleep_mic":
                return {"ok": False, "error": "unsupported system action"}
            if self._sleep_mic is None:
                return {"ok": False, "error": "microphone control is unavailable"}
            try:
                result = self._sleep_mic()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                _LOG.warning("Could not release microphone on UI shutdown: %s", exc)
                return {"ok": False, "error": "could not release microphone"}
            return {"ok": True}

        # Preserve the existing command vocabulary while clients migrate to
        # typed NDJSON events.
        command = str(message.get("command") or "")
        if command == "subscribe":
            return {"ok": False, "error": "subscribe requires a persistent connection"}
        return self._command(command, message)

    async def request_human_intervention(
        self,
        level: str,
        label: str,
        reason: str,
        prompt: str,
        options: list[str],
    ) -> str | None:
        """Broadcast a 3-tier agency prompt and await the selected option."""
        level = str(level or "").strip().lower()
        if level not in {"input", "decision", "action", "critical"}:
            return None
        reason = str(reason or "").strip()
        prompt = str(prompt or "").strip()
        if not reason or not prompt or len(reason) > 500 or len(prompt) > 1200:
            return None
        label = str(label or "").strip()[:80]
        if not label:
            return None
        if not isinstance(options, list):
            return None
        normalized_options = [
            str(option).strip()[:80]
            for option in options
            if isinstance(option, str) and str(option).strip()
        ][:8]
        if not normalized_options:
            return None
        if not self._subscribers:
            return None

        request_id = str(uuid.uuid4())
        waiter: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._authorization_waiters[request_id] = waiter
        try:
            await self.broadcast(
                "human_required",
                {
                    "request_id": request_id,
                    "level": level,
                    "label": label,
                    "reason": reason,
                    "prompt": prompt,
                    "options": normalized_options,
                },
            )
            try:
                return await asyncio.wait_for(
                    waiter, timeout=AUTH_RESPONSE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                return None
        except (ConnectionError, OSError):
            return None
        finally:
            self._authorization_waiters.pop(request_id, None)
            if not waiter.done():
                waiter.cancel()

    async def request_authorization(
        self,
        command: str,
        *,
        label: str = "Approval required",
        reason: str = "This command modifies system state and cannot run automatically.",
        prompt: str | None = None,
        options: list[str] | None = None,
        level: str = "decision",
    ) -> bool:
        """Ask the UI to approve an action through the shared agency schema."""
        if not isinstance(command, str) or not command.strip() or len(command) > 1000:
            return False
        approval_options = options or ["Approve", "Reject"]
        if not isinstance(approval_options, list) or not approval_options:
            return False
        choice = await self.request_human_intervention(
            level,
            label,
            reason,
            prompt if prompt is not None else command,
            approval_options,
        )
        # The first option is the affirmative choice in the agency contract.
        return choice == approval_options[0] or choice == "Approve"

    def _handle_auth_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return {"ok": False, "error": "invalid authorization response"}

        approved = payload.get("approved")
        option = payload.get("option", payload.get("response"))
        if isinstance(option, str) and option.strip():
            choice = option.strip()[:80]
        elif isinstance(approved, bool):
            # Backward compatibility for clients that only send a boolean.
            choice = "Approve" if approved else "Reject"
        else:
            return {"ok": False, "error": "invalid authorization response"}

        waiter = self._authorization_waiters.get(request_id)
        if waiter is None or waiter.done():
            return {"ok": False, "error": "authorization request expired or invalid"}
        waiter.set_result(choice)
        return {"ok": True}

    async def _ui_say(self, text: str, status: str) -> None:
        from kakarot_daemon.scrubber import sanitize_text

        await self.broadcast(
            "ui_say", {"text": sanitize_text(text)[:1200], "status": status}
        )

    def _command(self, command: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one command and answer with the snapshot it produced."""
        return self._router.handle(command, request)

    def _encode(self, event: dict[str, Any]) -> bytes:
        return json.dumps(event, separators=(",", ":")).encode() + b"\n"

    async def _subscribe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._subscribers.add(writer)
        try:
            writer.write(
                self._encode({"type": "state", "state": self._status()})
            )
            with contextlib.suppress(ConnectionError, OSError):
                await writer.drain()
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="strict").strip()
                    if not decoded:
                        continue
                    try:
                        message = json.loads(decoded)
                        if not isinstance(message, dict):
                            continue
                        response = await self.dispatch_message(message)
                    except json.JSONDecodeError:
                        continue
                    writer.write(self._encode({"type": "response", **response}))
                    await asyncio.wait_for(writer.drain(), timeout=2.0)
            except (ConnectionError, OSError, ValueError):
                # Aborted transports deliver the pipe error to the reader
                # waiter, not just to writes; either way the peer is gone.
                pass
        finally:
            self._subscribers.discard(writer)
            # Abort discards the send buffer; a graceful close() on a dead
            # peer raises BrokenPipeError while flushing. Suppress the whole
            # teardown — the peer is gone either way.
            try:
                if not writer.is_closing():
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (ConnectionError, OSError, asyncio.TimeoutError, ValueError):
                pass


async def send_command(path: Path, command: str) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(json.dumps({"command": command}).encode() + b"\n")
    await writer.drain()
    response = json.loads(
        (await asyncio.wait_for(reader.readline(), 1.5)).decode("utf-8")
    )
    writer.close()
    await writer.wait_closed()
    if not isinstance(response, dict):
        raise ValueError("invalid daemon response")
    return response
