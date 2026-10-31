"""Tightly sized PyQt6 desktop mascot for Kakarot."""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import os
import sys
import tempfile
from PyQt6.QtCore import (
    QByteArray,
    QEvent,
    QLockFile,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

_INSTANCE_LOCK: QLockFile | None = None
_INSTANCE_SERVER_NAME = "kakarot_mascot_activate"
_ICON_SVGS = {
    "mic": '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/>',
    "mic-off": '<path d="M9 9v5a3 3 0 0 0 5.1 2.1M15 9V5a3 3 0 0 0-5.8-1M5 10v2a7 7 0 0 0 11.9 5M19 10v2a7 7 0 0 1-.5 2.6M12 19v3m-4 0h8M2 2l20 20"/>',
    "volume-2": '<path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7m3-10a9 9 0 0 1 0 13"/>',
    "volume-x": '<path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="m17 9 5 6m0-6-5 6"/>',
    "settings": '<path d="M4 21v-7m0-4V3m8 18v-9m0-4V3m8 18v-5m0-4V3"/><path d="M2 14h4m4-6h4m4 8h4"/>',
}
_ICON_CACHE: dict[str, QIcon] = {}


class AudioWaveformWidget(QWidget):
    """Compact waveform driven by microphone bands with a quiet decay on silence."""

    decayed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(32, 20)
        self.max_height = 20
        self.current_heights = [4.0] * 4
        self.target_heights = [4.0] * 4
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._interpolate)

    def set_state(self, state: str):
        if state not in {"listening", "speaking"}:
            self.update_levels([], active=False)

    def update_levels(self, bands: list[float], *, active: bool = True):
        """Set target bar heights from four or five normalized frequency bands."""
        values = []
        if active and isinstance(bands, (list, tuple)):
            for value in bands[:5]:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    number = 0.0
                values.append(
                    min(1.0, max(0.0, number))
                    if math.isfinite(number)
                    else 0.0
                )
        if not values:
            values = [0.0] * len(self.current_heights)
        if values and len(values) == 5:
            values = [
                values[0],
                (values[1] + values[2]) / 2,
                (values[2] + values[3]) / 2,
                values[4],
            ]
        elif values and len(values) < 4:
            values = (values + [0.0] * 4)[:4]
        elif values:
            values = values[:4]
        self.target_heights = [
            max(4.0, value * self.max_height) for value in values
        ]
        if any(abs(c - t) > 0.25 for c, t in zip(self.current_heights, self.target_heights)):
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            if all(height <= 4.25 for height in self.current_heights):
                self.decayed.emit()

    def _interpolate(self):
        settled = True
        for index, target in enumerate(self.target_heights):
            current = self.current_heights[index]
            if abs(target - current) <= 0.25:
                self.current_heights[index] = target
            else:
                self.current_heights[index] = current + 0.35 * (target - current)
                settled = False
        self.update()
        if settled:
            self._timer.stop()
            if all(height <= 4.25 for height in self.current_heights):
                self.decayed.emit()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.white)
        bar_width, gap = 3.0, 3.0
        count = len(self.current_heights)
        total_width = count * bar_width + (count - 1) * gap
        start_x = (self.width() - total_width) / 2
        center_y = self.height() / 2
        for index, bar_height in enumerate(self.current_heights):
            rect = QRectF(
                start_x + index * (bar_width + gap),
                center_y - bar_height / 2,
                bar_width,
                bar_height,
            )
            painter.drawRoundedRect(rect, bar_width / 2, bar_width / 2)
        painter.end()


def _avatar_path() -> str | None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(root, "avatar.png"), os.path.join(root, "assets", "avatar.png")):
        if os.path.isfile(path):
            return path
    return None


def _socket_path() -> str:
    """Resolve the compatibility socket without importing daemon code."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip() or "/run/user/1000"
    return os.path.join(runtime_dir, "kakarot", "kakarot.sock")


def _hud_layout_path() -> str:
    """Where the mascot remembers its last dragged position."""
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = config_home or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "kakarot", "hud_layout.json")


def _load_hud_layout() -> tuple[int, int] | None:
    """Read the saved window origin, ignoring anything malformed."""
    try:
        with open(_hud_layout_path(), encoding="utf-8") as stream:
            data = json.load(stream)
        x, y = data.get("x"), data.get("y")
    except (OSError, ValueError, AttributeError):
        return None
    # ``bool`` is an ``int`` subclass; a True coordinate would be nonsense.
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, int) or not isinstance(y, int):
        return None
    return x, y


def _save_hud_layout(x: int, y: int) -> None:
    """Atomically persist the mascot origin, owner-readable only."""
    path = _hud_layout_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".hud-layout-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"x": int(x), "y": int(y)}, stream)
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _position_is_visible(x: int, y: int, size: QSize) -> bool:
    """Reject saved coordinates that would leave the HUD effectively hidden."""
    rect = QRect(x, y, max(1, size.width()), max(1, size.height()))
    for screen in QApplication.screens():
        overlap = screen.availableGeometry().intersected(rect)
        if overlap.width() >= 80 and overlap.height() >= 80:
            return True
    return False


def _mascot_window_flags():
    return (
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
    )


def _configure_application(app: QApplication) -> None:
    app.setApplicationName("Kakarot")
    app.setApplicationDisplayName("Kakarot")
    app.setDesktopFileName("kakarot-mascot")
    avatar_path = _avatar_path()
    if avatar_path is not None:
        app.setWindowIcon(QIcon(str(avatar_path)))


def _vector_icon(name: str) -> QIcon:
    """Render a small SVG path at 2x resolution for crisp toolbar icons."""
    icon = _ICON_CACHE.get(name)
    if icon is not None:
        return icon
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
        'viewBox="0 0 24 24" fill="none" stroke="#d4d4d8" stroke-width="1.75" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f"{_ICON_SVGS[name]}</svg>"
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    icon = QIcon(pixmap)
    _ICON_CACHE[name] = icon
    return icon


def _acquire_instance_lock() -> bool:
    """Hold one process-wide lock before Qt creates any application windows."""
    global _INSTANCE_LOCK
    lock = QLockFile(os.path.join(tempfile.gettempdir(), "kakarot_mascot.lock"))
    if lock.tryLock(100):
        _INSTANCE_LOCK = lock
        return True
    if lock.removeStaleLockFile() and lock.tryLock(100):
        _INSTANCE_LOCK = lock
        return True
    _notify_existing_instance()
    print("Mascot instance already active or lock held", file=sys.stderr)
    return False


def _notify_existing_instance() -> None:
    """Ask the lock owner to raise its window when the app icon is clicked again."""
    socket = QLocalSocket()
    socket.connectToServer(_INSTANCE_SERVER_NAME)
    if socket.waitForConnected(150):
        socket.write(b"raise\n")
        socket.flush()
        socket.waitForBytesWritten(150)
    socket.abort()


def _set_x11_sticky(widget: QWidget) -> None:
    """Mark an X11/XWayland window for every desktop when libX11 is available."""
    if QApplication.platformName().lower() not in {"xcb", "x11"}:
        return
    library = ctypes.util.find_library("X11")
    if not library:
        return
    display = None
    try:
        x11 = ctypes.CDLL(library)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XChangeProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
        ]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        class _ClientMessageData(ctypes.Union):
            _fields_ = [
                ("b", ctypes.c_char * 20),
                ("s", ctypes.c_short * 10),
                ("l", ctypes.c_long * 5),
            ]

        class _ClientMessage(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_int),
                ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong),
                ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int),
                ("data", _ClientMessageData),
            ]

        class _XEvent(ctypes.Union):
            _fields_ = [("type", ctypes.c_int), ("xclient", _ClientMessage), ("pad", ctypes.c_long * 24)]

        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XSendEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
            ctypes.POINTER(_XEvent),
        ]
        x11.XSendEvent.restype = ctypes.c_int
        display = x11.XOpenDisplay(None)
        if not display:
            return
        window = int(widget.winId())
        desktop_atom = x11.XInternAtom(display, b"_NET_WM_DESKTOP", 0)
        cardinal_atom = x11.XInternAtom(display, b"CARDINAL", 0)
        all_desktops = ctypes.c_ulong(0xFFFFFFFF)
        x11.XChangeProperty(
            display,
            window,
            desktop_atom,
            cardinal_atom,
            32,
            0,
            ctypes.byref(all_desktops),
            1,
        )
        sticky_atom = x11.XInternAtom(display, b"_NET_WM_STATE_STICKY", 0)
        state_atom = x11.XInternAtom(display, b"_NET_WM_STATE", 0)
        event = _XEvent()
        event.xclient.type = 33  # ClientMessage
        event.xclient.serial = 0
        event.xclient.send_event = 1
        event.xclient.display = display
        event.xclient.window = window
        event.xclient.message_type = state_atom
        event.xclient.format = 32
        event.xclient.data.l[0] = 1  # _NET_WM_STATE_ADD
        event.xclient.data.l[1] = sticky_atom
        event.xclient.data.l[2] = 0
        event.xclient.data.l[3] = 1  # application request
        root = x11.XDefaultRootWindow(display)
        x11.XSendEvent(display, root, 0, (1 << 20) | (1 << 19), ctypes.byref(event))
        x11.XFlush(display)
    except Exception:
        # The Qt Tool/StayOnTop hints remain in place if the WM lacks X11 support.
        return
    finally:
        if display:
            try:
                x11.XCloseDisplay(display)
            except Exception:
                pass


_AGENCY_LEVELS = frozenset({"input", "decision", "action", "critical"})
_AGENCY_LABELS = {
    "input": "Need your input",
    "decision": "Your decision",
    "action": "Action required",
    "critical": "Immediate action",
}
_AGENCY_DEFAULT_OPTIONS = {
    "input": ["Continue"],
    "decision": ["Approve", "Reject"],
    "action": ["I'm done"],
    "critical": ["Acknowledge"],
}


class FloatingAgencyStrip(QWidget):
    """Compact non-modal 3-tier agency prompt shown beneath the mic dock.

    The daemon pauses the active turn while this strip waits for a chip click.
    It never opens a modal dialog, so the event loop, hotkeys, and the mic
    toggle stay responsive.
    """

    response_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("agencyStrip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumWidth(228)
        self.setMaximumWidth(320)
        self.setVisible(False)
        self._request_id = ""
        self._options: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(6)
        self.header = QLabel(self)
        self.header.setObjectName("agencyHeader")
        self.header.setWordWrap(True)
        self.header.setAccessibleName("Agency request label and reason")
        self.prompt = QLabel(self)
        self.prompt.setObjectName("agencyPrompt")
        self.prompt.setWordWrap(True)
        self.prompt.setTextFormat(Qt.TextFormat.PlainText)
        self.prompt.setAccessibleName("Agency request prompt")
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(mono)
        self.options_widget = QWidget(self)
        self.options_layout = QHBoxLayout(self.options_widget)
        self.options_layout.setContentsMargins(0, 0, 0, 0)
        self.options_layout.setSpacing(6)
        layout.addWidget(self.header)
        layout.addWidget(self.prompt)
        layout.addWidget(self.options_widget)
        self.buttons: list[QPushButton] = []
        self._apply_style()

    @property
    def request_id(self) -> str:
        """The daemon request currently rendered by the strip."""
        return self._request_id

    def _apply_style(self) -> None:
        """Strict monochrome terminal aesthetic: sharp borders, mono font.

        The tier system lives on in labels and default options only; the
        interface itself never changes color.
        """
        self.setStyleSheet(
            "#agencyStrip { background-color: rgba(0, 0, 0, 0.85); "
            "color: #ffffff; border-radius: 0px; border: 1px solid #ffffff; "
            "font-family: monospace; }"
            "#agencyHeader { color: #ffffff; background: transparent; "
            "border: 0; font-family: monospace; font-weight: 600; "
            "font-size: 9pt; }"
            "#agencyPrompt { color: #ffffff; background: transparent; "
            "border: 0; font-family: monospace; font-size: 9pt; }"
            "#agencyChip { background-color: rgba(0, 0, 0, 0.5); "
            "color: #ffffff; border: 1px solid #ffffff; border-radius: 0px; "
            "padding: 4px 12px; font-family: monospace; font-weight: bold; }"
            "#agencyChip:hover { background-color: #ffffff; color: #000000; }"
            "#agencyChip:pressed { background-color: #ffffff; color: #000000; }"
        )

    def _clear_options(self) -> None:
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.buttons.clear()

    def show_request(self, payload: dict, event_type: str = "human_required") -> bool:
        """Render a human_required payload, including legacy approval events."""
        if not isinstance(payload, dict):
            return False
        if event_type in {"auth_request", "hitl_prompt"}:
            request_id = payload.get("request_id") or payload.get("token")
            prompt = payload.get("command") or payload.get("prompt")
            level = str(payload.get("level") or "decision").strip().lower()
            if level not in _AGENCY_LEVELS:
                level = "decision"
            label = str(payload.get("label") or "Approval required").strip()
            reason = str(
                payload.get("reason") or "This action can change your system."
            ).strip()
            options = payload.get("options")
        else:
            request_id = payload.get("request_id")
            prompt = payload.get("prompt") or payload.get("command")
            level = str(payload.get("level") or "decision").strip().lower()
            if level not in _AGENCY_LEVELS:
                level = "decision"
            reason = str(payload.get("reason") or "").strip()
            label = str(
                payload.get("label") or _AGENCY_LABELS.get(level, "Your decision")
            ).strip()
            options = payload.get("options")
        if not isinstance(options, list):
            options = _AGENCY_DEFAULT_OPTIONS.get(level, ["Approve", "Reject"])
        options = [
            str(option).strip()[:80]
            for option in options
            if str(option).strip()
        ]
        if not options:
            options = _AGENCY_DEFAULT_OPTIONS.get(level, ["Approve", "Reject"])
        if not isinstance(request_id, str) or not request_id:
            return False
        prompt_text = str(prompt or "(no prompt provided)")[:1200]
        self._request_id = request_id
        self._options = list(options[:8])
        self.header.setText(f"👤 {label} · {reason}" if reason else f"👤 {label}")
        self.prompt.setText(prompt_text)
        self._clear_options()
        for option in self._options:
            chip = QPushButton(option, self.options_widget)
            chip.setObjectName("agencyChip")
            chip.setFlat(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            chip.setAccessibleName(f"Respond: {option}")
            chip.clicked.connect(
                lambda _checked=False, choice=option: self._choose(choice)
            )
            self.options_layout.addWidget(chip)
            self.buttons.append(chip)
        self.options_layout.addStretch(1)
        self.show()
        self.adjustSize()
        self.raise_()
        return True

    def hide_request(self) -> None:
        self._request_id = ""
        self._options = []
        self._clear_options()
        self.hide()

    def _choose(self, option: str) -> None:
        if not self._request_id:
            return
        choice = str(option).strip()[:80]
        if choice:
            self.response_selected.emit(choice)


class KakarotMascot(QWidget):
    LED_GREEN = "#10b981"
    LED_RED = "#ef4444"
    LED_AMBER = "#f59e0b"

    # Emitted with the reminder title; the handler renders a passive terminal
    # block that stays on screen until the user clicks its ``[x] Ack`` chip.
    reminder_received = pyqtSignal(str)

    def __init__(self):
        super().__init__(None, _mascot_window_flags())
        self._app_icon = QIcon(str(_avatar_path())) if _avatar_path() else QIcon()
        if not self._app_icon.isNull():
            self.setWindowIcon(self._app_icon)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        always_tool = getattr(Qt.WidgetAttribute, "WA_AlwaysShowToolWindow", None)
        if always_tool is not None:
            self.setAttribute(always_tool, True)
        self.setWindowTitle("Kakarot")
        self.setObjectName("KakarotMascot")
        self.setStyleSheet("#KakarotMascot { background: transparent; }")

        self.socket_path = _socket_path()
        self.socket = QLocalSocket(self)
        self.socket.connected.connect(self._connected)
        self.socket.readyRead.connect(self._read)
        self.socket.errorOccurred.connect(self._socket_error)
        self.events_socket = QLocalSocket(self)
        self.events_socket.connected.connect(self._events_connected)
        self.events_socket.readyRead.connect(self._read_events)
        self.events_socket.disconnected.connect(self._schedule_events_reconnect)
        self.events_socket.errorOccurred.connect(self._event_socket_error)
        self._events_buffer = bytearray()
        self.events_retry = QTimer(self)
        self.events_retry.setInterval(2000)
        self.events_retry.timeout.connect(self._connect_events)
        self._request = None
        self._buffer = bytearray()
        self._job = None
        self._drag_offset: QPoint | None = None
        self._daemon_connected = False
        self._daemon_error = ""
        self._api_connected = False
        self._last_error = ""
        self._unstable = False
        # True while the bubble shows a phase message (THINKING/TOOL_CALL), so
        # the next phase can replace or clear it even if the audio mode does
        # not change.
        self._phase_shown = False
        self._mic_active = False
        self._speaker_active = True
        self._pending_speaker_muted: bool | None = None
        self._pending_auth: tuple[str, dict] | None = None
        self._settings_request = 0
        self._settings_open = False
        self._audio_mode = "idle"
        self._allow_close = False
        self._closing = False
        # Outbound typed events and reminder acks wait for the single in-flight
        # request instead of being dropped while a status poll is outstanding.
        self._pending_events: list[tuple[str, dict]] = []
        self._pending_acks: list[str] = []
        self._known_reminders: set[str] = set()
        self._reminder_queue: list[tuple[str, str]] = []
        self._active_reminder_id = ""

        # A repeated desktop launch should focus the existing mascot instead of
        # appearing to do nothing after the single-instance guard exits.
        QLocalServer.removeServer(_INSTANCE_SERVER_NAME)
        self._instance_server = QLocalServer(self)
        self._instance_server.setSocketOptions(
            QLocalServer.SocketOption.UserAccessOption
        )
        self._instance_server.newConnection.connect(self._handle_activation)
        self._instance_server.listen(_INSTANCE_SERVER_NAME)

        self.avatar = QLabel(self)
        self.avatar.setObjectName("avatar")
        self.avatar.setFixedSize(140, 140)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        path = _avatar_path()
        pixmap = QPixmap(str(path)) if path else QPixmap()
        if pixmap.isNull():
            raise FileNotFoundError("Kakarot avatar.png was not found")
        self.avatar.setPixmap(
            pixmap.scaled(
                140,
                140,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.avatar.installEventFilter(self)

        self.dock = QWidget(self)
        self.dock.setObjectName("floatingDock")
        self.dock.setFixedHeight(34)
        self.dock.setStyleSheet(
            "#floatingDock { background-color:rgba(18,18,22,0.88); "
            "border:1px solid rgba(255,255,255,0.10); border-radius:17px; }"
        )
        toolbar = QHBoxLayout(self.dock)
        toolbar.setContentsMargins(8, 3, 8, 3)
        toolbar.setSpacing(3)
        toolbar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.toolbar_layout = toolbar

        self.status_led = QLabel(self.dock)
        self.status_led.setFixedSize(8, 8)
        self.status_led.setAccessibleName("Kakarot daemon health")
        self.status_led.setStyleSheet("background:#71717a; border-radius:4px;")
        toolbar.addWidget(self.status_led, 0, Qt.AlignmentFlag.AlignVCenter)

        self.mic_button = self._make_button("mic", "Toggle microphone")
        self.mic_button.clicked.connect(self.toggle_microphone)
        toolbar.addWidget(self.mic_button)

        self.composer_button = QPushButton("[T]", self.dock)
        self.composer_button.setObjectName("terminalToggle")
        self.composer_button.setFixedSize(26, 26)
        self.composer_button.setToolTip("Type a message (Ctrl+Space)")
        self.composer_button.setAccessibleName("Type a message to Kakarot")
        self.composer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.composer_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.composer_button.setStyleSheet(
            "#terminalToggle { background: transparent; color: #ffffff; "
            "border: 1px solid #ffffff; border-radius: 0px; "
            "font-family: monospace; font-size: 8pt; font-weight: bold; "
            "padding: 0; }"
            "#terminalToggle:hover { background: #ffffff; color: #000000; }"
            "#terminalToggle:pressed { background: #ffffff; color: #000000; }"
        )
        self.composer_button.clicked.connect(lambda: self.toggle_composer())
        toolbar.insertWidget(toolbar.indexOf(self.mic_button) + 1, self.composer_button)

        self.speaker_button = self._make_button("volume-2", "Toggle spoken responses")
        self.speaker_button.clicked.connect(self.toggle_speaker)
        toolbar.addWidget(self.speaker_button)

        self.settings_button = self._make_button("settings", "Gemini API settings")
        self.settings_button.clicked.connect(self.open_settings)
        toolbar.addWidget(self.settings_button)

        self.composer = QLineEdit(self)
        self.composer.setObjectName("terminalComposer")
        self.composer.setPlaceholderText("[type a message — Enter sends]")
        self.composer.setMaxLength(4000)
        self.composer.setMinimumWidth(228)
        self.composer.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.composer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.composer.setStyleSheet(
            "#terminalComposer { background-color: rgba(0, 0, 0, 0.85); "
            "color: #ffffff; border: 1px solid #ffffff; border-radius: 0px; "
            "font-family: monospace; padding: 4px; outline: none; "
            "selection-background-color: #ffffff; selection-color: #000000; }"
        )
        self.composer.returnPressed.connect(self._submit_composer_text)
        self.composer.installEventFilter(self)
        self.composer.hide()
        self.composer_shortcut = QShortcut(QKeySequence("Ctrl+Space"), self)
        self.composer_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.composer_shortcut.activated.connect(self.toggle_composer)

        self.reminder = QWidget(self)
        self.reminder.setObjectName("reminderBubble")
        self.reminder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.reminder.setMaximumWidth(340)
        reminder_row = QHBoxLayout(self.reminder)
        reminder_row.setContentsMargins(8, 6, 8, 6)
        reminder_row.setSpacing(8)
        self.reminder_label = QLabel(self.reminder)
        self.reminder_label.setObjectName("reminderText")
        self.reminder_label.setWordWrap(True)
        self.reminder_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reminder_label.setAccessibleName("Kakarot reminder")
        self.reminder_ack = QPushButton("[x] Ack", self.reminder)
        self.reminder_ack.setObjectName("reminderAck")
        self.reminder_ack.setFlat(True)
        self.reminder_ack.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reminder_ack.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reminder_ack.setAccessibleName("Acknowledge reminder")
        self.reminder_ack.clicked.connect(lambda: self._ack_reminder())
        reminder_row.addWidget(self.reminder_label, 1)
        reminder_row.addWidget(self.reminder_ack, 0, Qt.AlignmentFlag.AlignVCenter)
        self.reminder.setStyleSheet(
            "#reminderBubble { background-color: rgba(0, 0, 0, 0.85); "
            "color: #ffffff; border: 1px solid #ffffff; border-radius: 0px; "
            "font-family: monospace; padding: 6px 8px; }"
            "#reminderText { color: #ffffff; background: transparent; "
            "border: 0; font-family: monospace; font-size: 9pt; padding: 0; }"
            "#reminderAck { background-color: rgba(0, 0, 0, 0.5); "
            "color: #ffffff; border: 1px solid #ffffff; border-radius: 0px; "
            "padding: 3px 10px; font-family: monospace; font-weight: bold; }"
            "#reminderAck:hover { background-color: #ffffff; color: #000000; }"
            "#reminderAck:pressed { background-color: #ffffff; color: #000000; }"
        )
        self.reminder_received.connect(self._on_reminder_received)
        self.reminder.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(4)
        root.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(self.dock, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(self.composer, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(self.reminder, 0, Qt.AlignmentFlag.AlignHCenter)
        self.agency_strip = FloatingAgencyStrip()
        self.agency_strip.response_selected.connect(self._on_agency_response)
        root.addWidget(self.agency_strip, 0, Qt.AlignmentFlag.AlignHCenter)
        self.agency_strip.hide()
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.adjustSize()

        self.bubble = QWidget(self)
        self.bubble.setObjectName("speechBubble")
        self.bubble.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.bubble_layout = QHBoxLayout(self.bubble)
        self.bubble_layout.setContentsMargins(8, 5, 8, 5)
        self.bubble_layout.setSpacing(7)
        self.bubble.setStyleSheet(
            "#speechBubble { background-color: rgba(0, 0, 0, 0.85); "
            "color: #ffffff; border: 1px solid #ffffff; border-radius: 0px; "
            "font-family: monospace; padding: 4px 8px; }"
            "#speechBubble QLabel { color:#ffffff; background:transparent; "
            "border:0; padding:0; font-family: monospace; font-size:9pt; }"
        )
        self.bubble_waveform = AudioWaveformWidget(self.bubble)
        self.bubble_waveform.decayed.connect(self._on_waveform_decayed)
        self.bubble_waveform.hide()
        self.bubble_label = QLabel(self.bubble)
        self.bubble_label.setWordWrap(True)
        self.bubble_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bubble_layout.addWidget(self.bubble_waveform, 0, Qt.AlignmentFlag.AlignVCenter)
        self.bubble_layout.addWidget(self.bubble_label, 1)
        self.bubble.hide()
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.bubble.hide)

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._finish_close)

        self.poll = QTimer(self)
        self.poll.timeout.connect(lambda: self._send("status"))
        QTimer.singleShot(250, lambda: _set_x11_sticky(self))
        self._restore_position()
        tray_icon = self._app_icon
        if tray_icon.isNull():
            tray_icon = QApplication.style().standardIcon(
                QStyle.StandardPixmap.SP_ComputerIcon
            )
        self.tray = QSystemTrayIcon(tray_icon, self)
        self.tray.setToolTip("Kakarot")
        self.tray_menu = QMenu(self)
        self.tray_menu.setStyleSheet(
            "QMenu { background-color: rgba(0, 0, 0, 0.95); color: #ffffff; "
            "border: 1px solid #ffffff; border-radius: 0px; "
            "font-family: monospace; padding: 4px; }"
            "QMenu::item { background: transparent; padding: 6px 18px; }"
            "QMenu::item:selected { background-color: #ffffff; color: #000000; }"
        )
        toggle_action = self.tray_menu.addAction("Toggle HUD")
        toggle_action.triggered.connect(self.toggle_hud)
        quit_action = self.tray_menu.addAction("Quit Kakarot")
        quit_action.triggered.connect(self.close)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print(
                "Kakarot tray is unavailable; GNOME requires AppIndicator/KStatusNotifierItem support.",
                file=sys.stderr,
            )
        # Paint the offline LED immediately; only the socket work is deferred.
        self._update_health()
        self._post_init_started = False
        QTimer.singleShot(100, self._post_init)

    def _restore_position(self) -> None:
        """Apply the last dragged position, or the bottom-right default."""
        saved = _load_hud_layout()
        if saved is not None and _position_is_visible(*saved, self.size()):
            self.move(*saved)
            return
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(
                area.right() - self.width() - 20,
                area.bottom() - self.height() - 30,
            )

    def _persist_position(self) -> None:
        if not self.isVisible():
            return
        try:
            _save_hud_layout(self.x(), self.y())
        except OSError:
            print("Kakarot could not save its HUD position.", file=sys.stderr)

    def _post_init(self):
        """Start IPC only after Qt has had a chance to render the mascot."""
        if self._post_init_started or self._closing:
            return
        self._post_init_started = True
        self._update_health()
        self.poll.start(1500)
        self._connect_events()
        # Never open the microphone just because the overlay started. Listening
        # begins only through an explicit mic click, Force Listen, or unmute.

    def show_mascot(self):
        if self.isMinimized():
            self.showNormal()
        elif not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_mascot()

    def toggle_hud(self):
        """Hide or restore the HUD without stopping the daemon or tray."""
        if self.isVisible():
            self._persist_position()
            self.hide()
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _make_button(self, icon_name: str, tooltip: str) -> QPushButton:
        button = QPushButton(self.dock)
        button.setFixedSize(26, 26)
        button.setIcon(_vector_icon(icon_name))
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setStyleSheet(
            "QPushButton { background:transparent; color:#d4d4d8; padding:2px; "
            "border:0; border-radius:13px; }"
            "QPushButton:hover { background:rgba(255,255,255,0.12); }"
        )
        return button

    def _health(self) -> tuple[str, str]:
        """Return the LED colour and hover text for daemon connection health.

        Muting the microphone is a user choice, not a fault, so it never changes
        the colour of this indicator.  Only the daemon link or the Gemini
        session can make Kakarot unhealthy.
        """
        if not self._daemon_connected:
            detail = self._daemon_error.strip()
            reason = f": {detail}" if detail else ""
            return (
                self.LED_RED,
                f"Kakarot daemon is unreachable{reason}. "
                "Check kakarot-core.service.",
            )
        if self._last_error.strip():
            return self.LED_RED, f"Kakarot is unhealthy: {self._last_error.strip()}"
        if self._unstable:
            return (
                self.LED_AMBER,
                "Gemini connection is unstable; reconnecting.",
            )
        if not self._api_connected:
            return (
                self.LED_RED,
                "Gemini session is disconnected. Check the API key and network.",
            )
        return (
            self.LED_GREEN,
            "Kakarot is healthy: daemon and Gemini session are connected.",
        )

    def _update_health(self):
        color, tooltip = self._health()
        self.status_led.setStyleSheet(f"background:{color}; border-radius:4px;")
        self.status_led.setToolTip(tooltip)
        self.status_led.setAccessibleDescription(tooltip)
        self._update_controls()

    def _update_controls(self):
        self.mic_button.setIcon(_vector_icon("mic" if self._mic_active else "mic-off"))
        mic_tooltip = "Mute microphone" if self._mic_active else "Enable microphone"
        self.mic_button.setToolTip(mic_tooltip)
        self.mic_button.setAccessibleName(mic_tooltip)
        self._update_speaker_button()

    def _update_speaker_button(self):
        self.speaker_button.setIcon(
            _vector_icon("volume-2" if self._speaker_active else "volume-x")
        )
        self.speaker_button.setToolTip(
            "Voice active; click to deactivate" if self._speaker_active
            else "Voice inactive; click to activate"
        )

    def open_settings(self):
        if self._settings_open or self._closing:
            return
        self._settings_open = True
        try:
            # Load keyring/config modules only when the user opens settings.
            from kakarot_overlay.settings_dialog import SettingsDialog

            dialog = SettingsDialog(self)
            dialog.setModal(True)
            dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
            if not self._app_icon.isNull():
                dialog.setWindowIcon(self._app_icon)
            dialog.key_saved.connect(lambda: self._send("reload"))
            dialog.exec()
            dialog.deleteLater()
        finally:
            self._settings_open = False

    def toggle_microphone(self):
        self._send("mute" if self._mic_active else "unmute")

    def toggle_speaker(self):
        if self._closing:
            return
        muted = self._speaker_active
        self._speaker_active = not muted
        self._pending_speaker_muted = muted
        self._update_speaker_button()
        self.show_message("Voice Unactivated" if muted else "Voice Activated")
        self._flush_pending_audio_control()

    def _flush_pending_audio_control(self):
        if self._closing or self._request is not None or self._pending_speaker_muted is None:
            return
        muted = self._pending_speaker_muted
        self._pending_speaker_muted = None
        self._send_event("audio_control", {"speaker_muted": muted})

    def show_message(self, text, *, persistent=False):
        text = str(text or "").strip()
        if not text:
            return
        self.bubble_label.setText(text[:400])
        self.bubble.setFixedWidth(max(120, self.width()))
        self.bubble.adjustSize()
        self.bubble.move(0, 0)
        self.bubble.show()
        self.bubble.raise_()
        if persistent:
            self.hide_timer.stop()
        else:
            self.hide_timer.start(5000)

    # ------------------------------------------------------- composer -- #

    def toggle_composer(self):
        """Show or hide the terminal-style typed-message composer."""
        self._set_composer_visible(not self.composer.isVisible())

    def _set_composer_visible(self, visible: bool):
        if self._closing or visible == self.composer.isVisible():
            if visible:
                self.composer.setFocus(Qt.FocusReason.ShortcutFocusReason)
            return
        anchor = self.geometry()
        self.composer.setVisible(visible)
        if visible:
            self.composer.clear()
            self._resize_anchored(anchor)
            self.raise_()
            self.composer.setFocus(Qt.FocusReason.ShortcutFocusReason)
        else:
            self._resize_anchored(anchor)

    def _submit_composer_text(self):
        """Send the typed turn, then clear and dismiss the composer."""
        text = self.composer.text().strip()
        self.composer.clear()
        self._set_composer_visible(False)
        if not text or self._closing:
            return
        self._queue_event("manual_input", {"text": text})

    def _queue_event(self, event_type: str, payload: dict):
        """Queue a typed event until the single in-flight request completes."""
        if self._closing:
            return
        self._pending_events.append((event_type, dict(payload)))
        self._flush_pending_events()

    def _flush_pending_events(self):
        if self._closing or not self._pending_events:
            return
        if self._request is not None:
            QTimer.singleShot(150, self._flush_pending_events)
            return
        event_type, payload = self._pending_events.pop(0)
        self._send_event(event_type, payload)
        if self._pending_events:
            QTimer.singleShot(150, self._flush_pending_events)

    # ------------------------------------------------------- reminders -- #

    def _remember_reminders(self, reminders) -> None:
        """Queue newly due reminders and keep the current block in sync."""
        if not isinstance(reminders, (list, tuple)):
            return
        current: dict[str, str] = {}
        for item in reminders:
            if not isinstance(item, dict):
                continue
            reminder_id = str(item.get("id") or "").strip()
            title = str(item.get("title") or "Reminder").strip()
            if reminder_id:
                current[reminder_id] = title or "Reminder"
        for reminder_id, title in current.items():
            if reminder_id not in self._known_reminders:
                self._known_reminders.add(reminder_id)
                self._reminder_queue.append((reminder_id, title))
        self._known_reminders.intersection_update(current)
        # Drop entries acknowledged elsewhere (the GNOME notification, another
        # HUD instance, or the model's cancel tool).
        self._reminder_queue = [
            entry for entry in self._reminder_queue if entry[0] in current
        ]
        if self._active_reminder_id and self._active_reminder_id not in current:
            self._active_reminder_id = ""
        if not self._active_reminder_id:
            self._show_next_reminder()

    def _show_next_reminder(self) -> None:
        if self._closing:
            return
        if not self._reminder_queue:
            if not self.reminder.isHidden():
                anchor = self.geometry()
                self.reminder.hide()
                self._resize_anchored(anchor)
            return
        reminder_id, title = self._reminder_queue[0]
        self._active_reminder_id = reminder_id
        self.reminder_received.emit(title)

    def _on_reminder_received(self, text: str) -> None:
        text = str(text or "").strip()
        if not text or self._closing:
            return
        anchor = self.geometry()
        self.reminder_label.setText(f"[!] {text}"[:400])
        self.reminder.show()
        self._resize_anchored(anchor)
        self.reminder.raise_()

    def _ack_reminder(self) -> None:
        reminder_id = self._active_reminder_id
        self._active_reminder_id = ""
        if reminder_id:
            self._pending_acks.append(reminder_id)
            self._flush_pending_acks()
        if self._reminder_queue:
            self._reminder_queue.pop(0)
        anchor = self.geometry()
        self.reminder.hide()
        self._resize_anchored(anchor)
        self._show_next_reminder()

    def _flush_pending_acks(self) -> None:
        if self._closing or not self._pending_acks:
            return
        if self._request is not None:
            QTimer.singleShot(250, self._flush_pending_acks)
            return
        reminder_id = self._pending_acks.pop(0)
        self._send(f"ack_reminder:{reminder_id}")
        if self._pending_acks:
            QTimer.singleShot(250, self._flush_pending_acks)

    def _set_audio_mode(self, mode, *, show=True, level=None):
        mode = mode if mode in {"listening", "speaking"} else "idle"
        self._audio_mode = mode
        self.bubble_waveform.set_state(mode)
        if mode == "idle":
            if any(height > 4 for height in self.bubble_waveform.current_heights):
                self.bubble_label.clear()
                self.bubble_waveform.show()
                self.bubble.adjustSize()
                self.bubble.show()
                self.hide_timer.stop()
            else:
                self._on_waveform_decayed()
            return
        self.bubble_waveform.show()
        if level is not None:
            self.bubble_waveform.update_levels([level] * 4)
        if show:
            label = "[+] Speaking..." if mode == "speaking" else "[*] Listening..."
            self.show_message(label, persistent=True)

    def _on_waveform_decayed(self):
        if self._audio_mode == "idle":
            self.bubble_waveform.hide()
            self.bubble.hide()

    def _send(self, command, **args):
        if self._closing or self._request is not None:
            return
        self._request = (command, args)
        self._buffer.clear()
        self.socket.abort()
        self.socket.connectToServer(self.socket_path)

    def _send_event(self, event_type, payload):
        if self._closing or self._request is not None:
            return
        self._request = ("_event", {"type": event_type, "payload": payload})
        self._buffer.clear()
        self.socket.abort()
        self.socket.connectToServer(self.socket_path)

    def _connect_events(self):
        if self._closing:
            return
        if self.events_socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            return
        self.events_socket.connectToServer(self.socket_path)

    def _events_connected(self):
        self.events_retry.stop()
        self.events_socket.write(b'{"command":"subscribe"}\n')
        self.events_socket.flush()

    def _schedule_events_reconnect(self):
        if not self._closing and not self.events_retry.isActive():
            self.events_retry.start()

    def _event_socket_error(self, _error):
        self._schedule_events_reconnect()

    def _read_events(self):
        self._events_buffer.extend(bytes(self.events_socket.readAll()))
        if len(self._events_buffer) > 65536:
            self.events_socket.abort()
            self._events_buffer.clear()
            self._schedule_events_reconnect()
            return
        while b"\n" in self._events_buffer:
            line, _, remainder = self._events_buffer.partition(b"\n")
            self._events_buffer = bytearray(remainder)
            try:
                event = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                self._handle_core_event(event)

    def _handle_core_event(self, event):
        event_type = event.get("type")
        if event_type == "state":
            state = event.get("state")
            if not isinstance(state, dict):
                return
            health_before = (
                self._daemon_connected,
                self._api_connected,
                self._last_error,
                self._unstable,
            )
            self._daemon_connected = True
            self._daemon_error = ""
            self._last_error = str(state.get("error") or "").strip()
            self._unstable = bool(state.get("unstable"))
            if "connected" in state:
                self._api_connected = bool(state["connected"])
            if "output_muted" in state:
                self._speaker_active = not bool(state["output_muted"])
            if state.get("audio_available") is False:
                self._mic_active = False
            elif "muted" in state:
                self._mic_active = not bool(state["muted"])
            elif "listening" in state:
                self._mic_active = bool(state["listening"])
            elif "mic_active" in state:
                self._mic_active = bool(state["mic_active"])
            if self._last_error or state.get("phase") == "ERROR":
                self._api_connected = False
            speaking = bool(state.get("speaking"))
            phase = str(state.get("phase") or "")
            tool_name = str(state.get("tool") or "").strip()
            next_mode = (
                "speaking" if speaking
                else "listening" if self._mic_active
                else "idle"
            )
            if phase == "TOOL_CALL" and tool_name:
                self.show_message(
                    f"[>] Running: {tool_name[:40]}...", persistent=True
                )
                self._phase_shown = True
            elif phase == "THINKING":
                self.show_message("[~] Processing...", persistent=True)
                self._phase_shown = True
            elif self._phase_shown or next_mode != self._audio_mode:
                self._phase_shown = False
                self._set_audio_mode(
                    next_mode,
                    level=state.get("audio_level", state.get("rms")),
                )
            health_after = (
                self._daemon_connected,
                self._api_connected,
                self._last_error,
                self._unstable,
            )
            if health_before != health_after:
                self._update_health()
            else:
                self._update_controls()
            try:
                settings_request = int(state.get("settings_request") or 0)
            except (TypeError, ValueError):
                settings_request = 0
            if settings_request == 0:
                # A fresh daemon starts its monotonic request counter at zero;
                # reset ours too, so the first request after a restart is not
                # mistaken for an old, already-handled one.
                self._settings_request = 0
            elif settings_request > self._settings_request:
                self._settings_request = settings_request
                QTimer.singleShot(0, self.open_settings)
            self._remember_reminders(state.get("reminders"))
            return
        if event_type == "ui_say":
            payload = event.get("payload")
            if not isinstance(payload, dict):
                return
            status = payload.get("status")
            if status == "speaking":
                self._set_audio_mode("speaking", level=payload.get("audio_level", payload.get("rms")))
            else:
                if self._audio_mode == "speaking":
                    self._set_audio_mode("listening" if self._mic_active else "idle", show=False)
                self.show_message(payload.get("text", ""))
            return
        if event_type in {"human_required", "auth_request", "hitl_prompt"}:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                return
            anchor = self.geometry()
            if self.agency_strip.show_request(payload, event_type):
                self._resize_for_agency(anchor)
            return
        if event_type == "audio_level":
            payload = event.get("payload")
            if not isinstance(payload, dict) or self._audio_mode == "idle":
                return
            bands = payload.get("bands")
            if isinstance(bands, list):
                self.bubble_waveform.update_levels(
                    bands, active=bool(payload.get("active", True))
                )
            elif "rms" in payload:
                self.bubble_waveform.update_levels(
                    [payload["rms"]] * 4,
                    active=bool(payload.get("active", True)),
                )

    def _resize_for_agency(self, anchor=None) -> None:
        """Resize around an agency prompt while keeping the window anchored.

        The mascot lives in the bottom-right corner, so growing downward would
        push the strip off-screen.  Keep the previous bottom-right corner fixed
        and let the window grow upward, then clamp it to the available screen.
        """
        self._resize_anchored(anchor)

    def _resize_anchored(self, anchor=None) -> None:
        """Grow or shrink the HUD around its bottom-right corner."""
        if self._closing:
            return
        if anchor is None:
            anchor = self.geometry()
        self.layout().activate()
        self.adjustSize()
        size = self.size()
        x = anchor.right() - size.width() + 1
        y = anchor.bottom() - size.height() + 1
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left(), min(x, area.right() - size.width() + 1))
            y = max(area.top(), min(y, area.bottom() - size.height() + 1))
        self.setGeometry(x, y, size.width(), size.height())

    def _on_agency_response(self, option: str) -> None:
        """Hide the strip, restore the dock geometry, then queue the answer."""
        anchor = self.geometry()
        request_id = self.agency_strip.request_id
        self.agency_strip.hide_request()
        self._resize_for_agency(anchor)
        if self._closing or not request_id:
            return
        choice = str(option).strip()[:80]
        if not choice:
            return
        approved = choice.startswith("✓") or choice in [
            "Approve",
            "Restart",
            "I'm done",
        ]
        self._pending_auth = (
            "human_response",
            {
                "request_id": request_id,
                "option": choice,
                "approved": approved,
            },
        )
        self._flush_pending_auth()

    def _flush_pending_auth(self):
        if self._closing or self._pending_auth is None:
            return
        if self._request is not None:
            QTimer.singleShot(250, self._flush_pending_auth)
            return
        event_type, payload = self._pending_auth
        self._pending_auth = None
        self._send_event(event_type, payload)
        if self._request is None:
            # The socket was unavailable; retry once it is back.
            self._pending_auth = (event_type, payload)
            QTimer.singleShot(500, self._flush_pending_auth)

    def _send_system_event(self, action):
        self._request = ("_system", {"action": action})
        self._buffer.clear()
        self.socket.abort()
        self.socket.connectToServer(self.socket_path)

    def _connected(self):
        if self._request is None:
            return
        command, args = self._request
        if command == "_system":
            message = {"sender": "mascot", "type": "system", "payload": args}
        elif command == "_event":
            message = {
                "sender": "mascot",
                "type": args["type"],
                "payload": args["payload"],
            }
        else:
            message = {"command": command, **args}
        self.socket.write(json.dumps(message).encode("utf-8") + b"\n")
        self.socket.flush()

    def _read(self):
        self._buffer.extend(bytes(self.socket.readAll()))
        if len(self._buffer) > 65536:
            self._finish(None)
        elif b"\n" in self._buffer:
            try:
                response = json.loads(self._buffer.split(b"\n", 1)[0])
            except (ValueError, UnicodeDecodeError):
                response = None
            self._finish(response if isinstance(response, dict) else None)

    def _socket_error(self, _error):
        self._daemon_connected = False
        self._daemon_error = self.socket.errorString() or "socket connection failed"
        self._update_health()
        if self._closing:
            self._finish_close()
            return
        if self._request and self._request[0] != "status":
            self.show_message("Kakarot daemon is unavailable.")
        self._request = None
        QTimer.singleShot(0, self._flush_pending_audio_control)
        QTimer.singleShot(300, self._flush_pending_auth)

    def _handle_activation(self):
        while self._instance_server.hasPendingConnections():
            client = self._instance_server.nextPendingConnection()
            client.readyRead.connect(lambda peer=client: self._read_activation(peer))
            if client.bytesAvailable():
                self._read_activation(client)

    def _read_activation(self, client):
        request = bytes(client.readAll()).strip()
        if request == b"raise":
            if self.isMinimized():
                self.showNormal()
            elif not self.isVisible():
                self.show()
            self.raise_()
            self.activateWindow()
        client.disconnectFromServer()
        client.deleteLater()

    def _finish(self, response):
        request, self._request = self._request, None
        self.socket.abort()
        if self._closing:
            # Older installed daemons may not know sleep_mic yet. A regular
            # mute command still stops capture on those versions.
            if (
                request
                and request[0] == "_system"
                and response
                and not response.get("ok")
            ):
                self._request = ("mute", {})
                self.socket.connectToServer(self.socket_path)
                return
            self._finish_close()
            return
        QTimer.singleShot(0, self._flush_pending_audio_control)
        QTimer.singleShot(0, self._flush_pending_auth)
        QTimer.singleShot(0, self._flush_pending_events)
        QTimer.singleShot(0, self._flush_pending_acks)
        health_before = (
            self._daemon_connected,
            self._api_connected,
            self._last_error,
        )
        if response is None:
            self._daemon_connected = False
            self._daemon_error = self.socket.errorString() or "socket connection failed"
        else:
            self._daemon_connected = True
            self._daemon_error = ""
            # Command failures do not carry a state snapshot.  Only a status
            # reply that contains the daemon snapshot may clear or set the
            # health error; otherwise a rejected mute would look like a dead
            # Gemini connection.
            if "connected" in response or "phase" in response:
                self._last_error = str(response.get("error") or "").strip()
            if "connected" in response:
                self._api_connected = bool(response["connected"])
            if self._last_error or response.get("phase") == "ERROR":
                self._api_connected = False
            # ``muted`` is the user's listening intent and outranks the
            # lower-level capture flag.  A race during playback could otherwise
            # report ``muted=True, mic_active=True``, leaving the button stuck
            # on "Mute" so every click sends another no-op mute.
            if response.get("audio_available") is False:
                self._mic_active = False
            elif "muted" in response:
                self._mic_active = not bool(response["muted"])
            elif "listening" in response:
                self._mic_active = bool(response["listening"])
            elif "mic_active" in response:
                self._mic_active = bool(response["mic_active"])
            if "output_muted" in response:
                self._speaker_active = not bool(response["output_muted"])
        speaker_event = (
            request is not None
            and request[0] == "_event"
            and request[1].get("type") == "audio_control"
        )
        health_after = (
            self._daemon_connected,
            self._api_connected,
            self._last_error,
        )
        if health_before != health_after:
            self._update_health()
        elif speaker_event:
            self._update_speaker_button()
        else:
            self._update_controls()
        if not response or not response.get("ok"):
            if request and request[0] != "status":
                self.show_message((response or {}).get("error", "Daemon did not reply."))
            return
        self._remember_reminders(response.get("reminders"))
        if "speaking" in response:
            mode = (
                "speaking" if response.get("speaking")
                else "listening" if self._mic_active
                else "idle"
            )
            if mode != self._audio_mode:
                self._set_audio_mode(mode, level=response.get("audio_level", response.get("rms")))
        command = request[0] if request else ""
        if command == "status":
            if response.get("error"):
                self.show_message(response["error"])
        elif command == "unmute":
            self._mic_active = bool(response.get("mic_active", response.get("listening", True)))
            self._update_health()
            if self._mic_active:
                self._set_audio_mode("listening")
            else:
                self._set_audio_mode("idle")
                self.show_message("Microphone unavailable.")
        elif command == "mute":
            self._mic_active = False
            self._update_health()
            self._set_audio_mode("idle")
            self.show_message("Microphone paused.")
        elif command == "speaker_on":
            self._speaker_active = True
            self._update_health()
            self.show_message("Voice Activated")
        elif command == "speaker_off":
            self._speaker_active = False
            self._update_health()
            self.show_message("Voice Unactivated")
        elif command == "execute":
            self._job = response.get("job")
            if self._job:
                self.show_message("Command sent through Kakarot’s audited sandbox.")
                QTimer.singleShot(400, self._poll_job)
        elif command == "result":
            if not response.get("done"):
                QTimer.singleShot(400, self._poll_job)
            elif response.get("requires_confirmation"):
                self.show_message("Kakarot requires your approval before this action.")
                self._job = None
            else:
                self.show_message(
                    response.get("stdout") or response.get("stderr")
                    or response.get("reason") or "Command finished."
                )
                self._job = None

    def _poll_job(self):
        if self._job:
            self._send("result", job=self._job)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#18181b;color:#f4f4f5;border:1px solid #3f3f46;padding:5px;}"
            "QMenu::item{padding:8px 24px;border-radius:6px;}"
            "QMenu::item:selected{background:#3f3f46;}"
        )
        listen = menu.addAction("Force Listen")
        execute = menu.addAction("Execute Command…")
        settings = menu.addAction("Settings / Keys…")
        menu.addSeparator()
        quit_action = menu.addAction("Sleep / Quit")
        chosen = menu.exec(event.globalPos())
        if chosen is listen:
            self._send("unmute")
        elif chosen is execute:
            dialog = QInputDialog(self)
            dialog.setWindowTitle("Kakarot command")
            dialog.setLabelText("Command (audited and sandboxed):")
            dialog.setInputMode(QInputDialog.InputMode.TextInput)
            dialog.setWindowModality(Qt.WindowModality.NonModal)
            dialog.finished.connect(lambda result: self._execute(dialog, result))
            dialog.open()
        elif chosen is settings:
            self.open_settings()
        elif chosen is quit_action:
            self.close()

    def _execute(self, dialog, result):
        if result == QInputDialog.DialogCode.Accepted and dialog.textValue().strip():
            self._send("execute", shell=dialog.textValue().strip())
        dialog.deleteLater()

    def eventFilter(self, watched, event):
        if watched is self.avatar:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.Type.MouseMove and self._drag_offset is not None:
                if event.buttons() & Qt.MouseButton.LeftButton:
                    self.move(event.globalPosition().toPoint() - self._drag_offset)
                    return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_offset = None
                self._persist_position()
        if watched is self.composer and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._set_composer_visible(False)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            self._persist_position()
            event.accept()

    def closeEvent(self, event):
        self._persist_position()
        if self._allow_close:
            self.poll.stop()
            self._close_timer.stop()
            self.events_retry.stop()
            self.socket.abort()
            self.events_socket.abort()
            event.accept()
            QTimer.singleShot(0, QApplication.quit)
            return
        if self._closing:
            event.ignore()
            return
        self._closing = True
        self.poll.stop()
        self.events_retry.stop()
        self.events_socket.abort()
        event.ignore()
        self._send_system_event("sleep_mic")
        self._close_timer.start(900)

    def _finish_close(self):
        if not self._closing:
            return
        self._close_timer.stop()
        self._allow_close = True
        self.close()


def main():
    if not _acquire_instance_lock():
        return 0
    app = QApplication(sys.argv)
    _configure_application(app)
    widget = KakarotMascot()
    widget.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
