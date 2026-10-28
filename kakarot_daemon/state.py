"""Single owner of the daemon's observable state.

The reporters -- the Live service, the audio bridge, the reminder scheduler --
know only about their own concrete lifecycle events ("the socket connected",
"playback started", "a reminder became due").  None of them may decide what the
snapshot means.  :class:`DaemonState` is the coordinator: it is the only object
that derives a phase, the only one that stamps a revision, and the only one that
publishes.  Adding a field here is the single edit needed to make it observable.

Every snapshot carries a monotonic ``state_version``.  Consumers use it to
discard duplicates and to detect that they missed an event, so publication is
idempotent: reporting the same value twice must not bump the revision.

The audio bridge reports playback transitions from the PortAudio real-time
callback thread, so :meth:`DaemonState.apply` is safe to call from any thread and
marshals the work onto the loop recorded by :meth:`attach`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

Snapshot = dict[str, Any]

# Every reported field and its neutral default.  This is the authoritative
# contract: anything a reporter sends that is not listed here is a bug, and
# ``apply`` raises rather than silently dropping the event.
_FIELDS: dict[str, Any] = {
    # User intent: is the microphone supposed to be live?
    "muted": True,
    # Speaker output gate.
    "output_muted": False,
    # Live-session facts reported by KakarotLiveService.
    "connected": False,
    "processing": False,
    # Playback facts reported by AudioBridge.
    "speaking": False,
    # Startup failure; empty whenever the voice path is healthy.
    "error": "",
    # Reminder facts reported by ReminderScheduler.
    "reminders": (),
    # Repository integration facts reported by the Live service.
    "repository_error": "",
    "repository_tools": (),
    # Monotonic id of the last "open settings" request; never resets.
    "settings_request": 0,
    # Gemini request/token ledger for the last user turn and the whole process.
    "last_turn_usage": {},
    "session_usage": {},
}

_DICT_FIELDS = ("last_turn_usage", "session_usage")

# Fields the coordinator derives and reporters may never set directly.
_DERIVED = ("state_version", "phase", "listening")


def _in_loop_thread(loop: asyncio.AbstractEventLoop | None) -> bool:
    if loop is None:
        return False
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def derive_phase(fields: dict[str, Any]) -> str:
    """Reduce reported events to the one phase a client should render."""
    if fields["error"]:
        return "ERROR"
    if fields["muted"]:
        return "IDLE_MUTED"
    if not fields["connected"]:
        return "DISCONNECTED"
    if fields["processing"]:
        return "PROCESSING_TOOL"
    if fields["speaking"]:
        return "SPEAKING"
    return "LISTENING"


class DaemonState:
    """Own the fields, the revision, and the publication of daemon state."""

    def __init__(self, publish: Callable[[Snapshot], None] | None = None) -> None:
        # Sequence defaults are immutable in ``_FIELDS`` so the module constant
        # stays safe to share, but each instance holds a real list so "no
        # reminders" compares equal to a report of no reminders.
        self._fields: dict[str, Any] = {
            key: (
                list(value)
                if isinstance(value, tuple)
                else dict(value)
                if isinstance(value, dict)
                else value
            )
            for key, value in _FIELDS.items()
        }
        self._version = 0
        self._publish = publish
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending = False

    # -- wiring -----------------------------------------------------------

    def attach(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> None:
        """Adopt the loop that ``apply`` marshals foreign threads onto.

        Outside an event loop there is no other thread to marshal for, so the
        coordinator stays direct and reports apply in the calling thread.
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        self._loop = loop
        self._flush()

    def set_publisher(self, publish: Callable[[Snapshot], None] | None) -> None:
        self._publish = publish
        self._flush()

    @property
    def version(self) -> int:
        return self._version

    @property
    def listening(self) -> bool:
        """The user's intent: should the voice path be running at all?"""
        return not self._fields["muted"]

    # -- reporting --------------------------------------------------------

    def apply(self, **events: Any) -> None:
        """Record concrete lifecycle events.  Safe to call from any thread."""
        if not events:
            return
        for key in events:
            if key not in _FIELDS:
                raise KeyError(f"unknown state field {key!r}")
        loop = self._loop
        if loop is not None and not _in_loop_thread(loop):
            try:
                loop.call_soon_threadsafe(self._commit, events)
            except RuntimeError:
                # The loop is shutting down; the terminal state is published
                # by the daemon's own teardown path.
                return
            return
        self._commit(events)

    # -- publication ------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Return the current observable state.  Call on the loop thread."""
        fields = self._fields
        return {
            "state_version": self._version,
            "phase": derive_phase(fields),
            "listening": not fields["muted"],
            "muted": fields["muted"],
            "output_muted": fields["output_muted"],
            "connected": fields["connected"],
            "processing": fields["processing"],
            "speaking": fields["speaking"],
            "error": fields["error"],
            "reminders": list(fields["reminders"]),
            "repository_error": fields["repository_error"],
            "repository_tools": list(fields["repository_tools"]),
            "settings_request": fields["settings_request"],
            "last_turn_usage": dict(fields["last_turn_usage"]),
            "session_usage": dict(fields["session_usage"]),
        }

    # -- internals --------------------------------------------------------

    def _commit(self, events: dict[str, Any]) -> None:
        changed = False
        for key, value in events.items():
            if key == "reminders" or key == "repository_tools":
                # Normalise to a list so a reporter passing a tuple and the
                # coordinator's own default compare equal.
                value = list(value)
            elif key in _DICT_FIELDS:
                # The ledger is copied so a later in-place update by the
                # reporter cannot mutate an already-published snapshot.
                value = dict(value or {})
            if self._fields[key] != value:
                self._fields[key] = value
                changed = True
        if not changed:
            return
        self._version += 1
        self._flush(changed=True)

    def _flush(self, *, changed: bool = False) -> None:
        """Publish, but only for a real change or a change held while unwired.

        Wiring the loop and the publisher must not emit the initial snapshot:
        a subscriber would see a revision it can never attribute to an event.
        """
        if self._publish is None:
            self._pending = True
            return
        if not self._pending and not changed:
            return
        self._pending = False
        self._publish(self.snapshot())
