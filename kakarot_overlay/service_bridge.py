import datetime
"""Qt-native IPC: pushed state, one-shot commands, and a polling fallback.

The daemon publishes one revisioned snapshot and pushes it to any subscriber.
A duplicate revision on the push stream is pure noise and is dropped.  A
``status`` read is the fallback *and* the retry path -- a pending reminder whose
acknowledgement failed must be offered again -- so an equal revision is
re-delivered there.  Only a strictly older revision is ever rejected, which
keeps a slow poll from moving the UI backwards.
"""
import json

from kakarot_overlay.qt import QtCore as C, QtNetwork as N, Signal
from kakarot_daemon.config import KakarotDaemonConfig

MAX_BUFFER = 65536
CONNECT_TIMEOUT_MS = 1500
MAX_RECONNECT_MS = 5000
# Cadence of the confirmation read while the push stream is healthy.
CONFIRM_INTERVAL_MS = 5000


class ServiceBridge(C.QObject):
    status_changed = Signal(dict)
    failed = Signal(str)
    command_finished = Signal(str, dict)
    command_failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = str(
            KakarotDaemonConfig.from_environment().data_dir / "kakarot.sock"
        )
        self._requests = {}
        self._closed = False
        self._delay = 1000
        self._generation = 0
        self._version = -1
        self._stream = None
        self._stream_buffer = bytearray()
        self._reconnect_delay = 250

        self.timer = C.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(0)

        self.retry = C.QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.timeout.connect(self.open_stream)
        self.open_stream()

    # -- revision handling -------------------------------------------------

    def _accept(self, snapshot, dedupe):
        if not isinstance(snapshot, dict):
            return
        version = snapshot.get("state_version")
        if type(version) is int:
            if version < self._version:
                return
            if dedupe and version == self._version:
                return
            self._version = version
        self._delay = 1000
        self.status_changed.emit(snapshot)

    def _schedule_poll(self, ok):
        if self._closed:
            return
        if not ok:
            self.timer.start(self._delay)
        elif self._stream is not None:
            # The stream carries changes and heartbeat; stop polling completely
            self.timer.stop()
        else:
            self.timer.start(150)

    # -- one-shot commands and polling -------------------------------------

    def refresh(self):
        if self._closed:
            return
        if self._requests:
            self.timer.start(250)
            return
        self._submit("status")

    def toggle_listening(self, muted):
        command = "unmute" if muted else "mute"
        print(f"[UI TRACE] [{datetime.datetime.now()}] Mic button clicked. Intended command: {command}")
        self.command(command)

    def command(self, command):
        if self._closed:
            print(f"[UI TRACE] [{datetime.datetime.now()}] Socket closed, discarding command: {command}")
            return
        self._generation += 1
        print(f"[UI TRACE] [{datetime.datetime.now()}] Submitting IPC payload: {{'command': '{command}'}}")
        self._submit(command)

    def _submit(self, command):
        socket = N.QLocalSocket(self)
        timeout = C.QTimer(socket)
        timeout.setSingleShot(True)
        buffer = bytearray()
        generation = self._generation
        self._requests[socket] = command

        def finish(response=None):
            if socket not in self._requests:
                return
            del self._requests[socket]
            timeout.stop()
            try:
                socket.abort()
            except RuntimeError:
                pass
            socket.deleteLater()
            if self._closed:
                return
            if response and response.get("ok"):
                self._accept(response, dedupe=False)
                if command != "status":
                    self.command_finished.emit(command, response)
            else:
                message = (response or {}).get(
                    "error"
                ) or "Voice service unavailable. Start kakarot.service."
                self._delay = min(MAX_RECONNECT_MS, self._delay * 2)
                if generation == self._generation:
                    self.failed.emit(message)
                if command != "status":
                    self.command_failed.emit(command, message)
            self._schedule_poll(bool(response))

        def read():
            buffer.extend(bytes(socket.readAll()))
            if len(buffer) > MAX_BUFFER:
                finish()
            elif b"\n" in buffer:
                try:
                    response = json.loads(buffer.split(b"\n", 1)[0])
                    finish(response if isinstance(response, dict) else None)
                except (ValueError, UnicodeDecodeError):
                    finish()

        socket.connected.connect(
            lambda: socket.write(
                json.dumps({"command": command}).encode() + b"\n"
            )
        )
        socket.readyRead.connect(read)
        socket.errorOccurred.connect(lambda _error: finish())
        timeout.timeout.connect(finish)
        socket.connectToServer(self.path)
        timeout.start(CONNECT_TIMEOUT_MS)

    # -- push stream -------------------------------------------------------

    def open_stream(self):
        if self._closed or self._stream is not None:
            return
        socket = N.QLocalSocket(self)
        watchdog = C.QTimer(socket)
        watchdog.setSingleShot(True)
        self._stream = socket
        self._stream_buffer = bytearray()

        def forget():
            # disconnected and errorOccurred both fire; only the first counts.
            if self._stream is not socket:
                return
            self._stream = None
            self._stream_buffer = bytearray()
            watchdog.stop()
            # Clearing self._stream above is what stops re-entry; abort() can
            # deliver another signal to this same socket.
            try:
                socket.abort()
            except RuntimeError:
                # The C++ object is already gone during teardown.
                pass
            socket.deleteLater()
            if not self._closed:
                self._reconnect_delay = min(
                    MAX_RECONNECT_MS, self._reconnect_delay * 2
                )
                self.retry.start(self._reconnect_delay)

        def connected():
            watchdog.stop()
            self._reconnect_delay = 250
            socket.write(b'{"command":"subscribe"}\n')

        socket.connected.connect(connected)
        socket.readyRead.connect(self._read_stream)
        socket.disconnected.connect(forget)
        socket.errorOccurred.connect(lambda _error: forget())
        watchdog.timeout.connect(forget)
        socket.connectToServer(self.path)
        watchdog.start(CONNECT_TIMEOUT_MS)

    def _read_stream(self):
        socket = self._stream
        if socket is None:
            return
        self._stream_buffer.extend(bytes(socket.readAll()))
        if len(self._stream_buffer) > MAX_BUFFER:
            # Fell far enough behind that the buffer is meaningless; resync.
            self._stream_buffer = bytearray()
            self.refresh()
            return
        while b"\n" in self._stream_buffer:
            line, _, rest = self._stream_buffer.partition(b"\n")
            self._stream_buffer = bytearray(rest)
            self._consume(line)

    def _consume(self, line):
        if not line:
            return
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "state":
            self._accept(event.get("state"), dedupe=True)
        elif kind == "heartbeat":
            # A heartbeat carries only the revision.  One ahead of what we hold
            # means a push was lost, so read the full snapshot again.
            version = event.get("state_version")
            if type(version) is int and version > self._version:
                self.refresh()

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        self._closed = True
        self.timer.stop()
        self.retry.stop()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            except RuntimeError:
                pass
            stream.deleteLater()
        for socket in list(self._requests):
            try:
                socket.abort()
            except RuntimeError:
                pass
            socket.deleteLater()
        self._requests.clear()
