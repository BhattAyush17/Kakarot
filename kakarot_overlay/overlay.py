"""Transparent, movable desktop avatar with a bounded-memory voice bridge."""

import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import signal
import time
import sys

from kakarot_overlay.qt import QtCore as C, QtGui as G, QtNetwork as N, QtWidgets as W
from kakarot_overlay.assets import avatar_pixmap, tray_icon
from kakarot_overlay.config import load_preferences, save_preferences
from kakarot_overlay.service_bridge import ServiceBridge
from kakarot_overlay.settings_dialog import SettingsDialog

# The daemon's coordinator derives the phase; the overlay only renders it.
# Each phase maps to what the avatar shows, with the tooltip that explains it.
PHASES = {
    "IDLE_MUTED": ("IDLE_MUTED", "Muted"),
    "DISCONNECTED": ("DISCONNECTED", "Connecting…"),
    "LISTENING": ("LISTENING", "Listening"),
    "PROCESSING_TOOL": ("PROCESSING_TOOL", "Executing task…"),
    "SPEAKING": ("SPEAKING", "Speaking"),
    "ERROR": ("ERROR", ""),
}


class Overlay(W.QWidget):
    def __init__(self):
        flags = (
            C.Qt.WindowType.FramelessWindowHint
            | C.Qt.WindowType.WindowStaysOnTopHint
            | C.Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.setAttribute(C.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Kakarot")
        self.setWindowIcon(tray_icon())
        self.setFixedSize(220, 265)
        self.face = avatar_pixmap(190)
        self.face_pos = C.QPoint((220 - self.face.width()) // 2, 8)
        self.state = "IDLE_MUTED"
        self.base_state = "IDLE_MUTED"
        self.muted = True
        self.output_muted = False
        self.connected = False
        self.alert_until = 0.0
        self.alert_text = ""
        self.seen_reminders = set()
        self.settings_request = 0
        self.pending = False
        self.phase = 0.0
        self.drag_offset = None
        self.quitting = False
        self.bridge = ServiceBridge(self)
        self.bridge.status_changed.connect(self.status_received)
        self.bridge.failed.connect(self.failure)
        self.bridge.command_finished.connect(self.command_done)
        self.bridge.command_failed.connect(self.command_error)
        self.buttons = []
        for x, text, tip, action in (
            (28, "●", "Toggle microphone", self.toggle_listening),
            (88, "♪", "Mute speaker", self.toggle_speaker),
            (148, "⚙", "Settings", self.open_settings),
        ):
            button = W.QPushButton(text, self)
            button.setGeometry(x, 185, 44, 44)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setCursor(C.Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton{color:#dce7ee;background:#333b4c;border:1px solid #525c70;border-radius:22px;font-size:22px;} QPushButton:hover{background:#465773;}"
            )
            button.clicked.connect(action)
            self.buttons.append(button)
        self.settings = SettingsDialog(self)
        self.settings.key_saved.connect(lambda: self.bridge.command("reload"))
        self.animation = C.QTimer(self)
        self.animation.setInterval(16)
        self.animation.timeout.connect(self.tick)
        self.persist_timer = C.QTimer(self)
        self.persist_timer.setSingleShot(True)
        self.persist_timer.setInterval(300)
        self.persist_timer.timeout.connect(self.persist)
        self.menu = W.QMenu()
        self.show_action = self.menu.addAction("Hide Overlay", self.toggle_visible)
        self.menu.addAction("Toggle Microphone", self.toggle_listening)
        self.menu.addAction("Quit", self.force_quit)
        self.tray = None
        if W.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = W.QSystemTrayIcon(tray_icon(), self)
            self.tray.setToolTip("Kakarot")
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(
                lambda reason: (
                    self.toggle_visible()
                    if reason == W.QSystemTrayIcon.ActivationReason.Trigger
                    else None
                )
            )
            self.tray.show()
        self.preferences = load_preferences()
        available = W.QApplication.primaryScreen().availableGeometry()
        x, y = self.preferences.get("x"), self.preferences.get("y")
        self.move(
            x if type(x) is int else available.right() - 240,
            y if type(y) is int else available.bottom() - 285,
        )
        self.clamp()
        self.update_mask()
        W.QApplication.instance().screenAdded.connect(self.screen_changed)
        W.QApplication.instance().screenRemoved.connect(self.screen_changed)
        for screen in W.QApplication.screens():
            screen.availableGeometryChanged.connect(self.screen_changed)
            screen.logicalDotsPerInchChanged.connect(self.screen_changed)

    def screen_changed(self, *_):
        self.clamp()
        self.update_mask()

    def update_mask(self):
        region = G.QRegion(self.face.mask()).translated(self.face_pos)
        path = G.QPainterPath()
        path.addRoundedRect(C.QRectF(16, 177, 188, 60), 30, 30)
        region |= G.QRegion(path.toFillPolygon().toPolygon())
        region |= G.QRegion(38, 240, 144, 24)
        self.setMask(region)

    def paintEvent(self, event):
        painter = G.QPainter(self)
        painter.drawPixmap(self.face_pos, self.face)
        if self.state in ("reminder", "PROCESSING_TOOL"):
            painter.setCompositionMode(
                G.QPainter.CompositionMode.CompositionMode_SourceAtop
            )
            # Yellow aura / thinking spinner effect
            painter.fillRect(
                self.rect(), G.QColor(255, 205, 55, int(40 + 30 * math.sin(self.phase)))
            )
            painter.setCompositionMode(
                G.QPainter.CompositionMode.CompositionMode_SourceOver
            )
            
        if self.state == "LISTENING":
            # Mic button glow/pulse
            r = 24 + 4 * math.sin(self.phase * 2)
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(G.QColor(87, 226, 187, 80))
            painter.drawEllipse(C.QPointF(50, 207), r, r)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.setBrush(G.QColor(18, 23, 34, 220))
        painter.setPen(G.QColor(255, 255, 255, 28))
        painter.drawRoundedRect(C.QRectF(16, 177, 188, 60), 30, 30)
        color = {
            "IDLE_MUTED": "#718397",
            "DISCONNECTED": "#718397",
            "LISTENING": "#57e2bb",
            "SPEAKING": "#b499ff",
            "PROCESSING_TOOL": "#edbd6d",
            "ERROR": "#ff737f",
            "reminder": "#ffcf4c",
        }.get(self.state, "#718397")
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(G.QColor(color))
        if self.state == "ERROR":
            painter.drawEllipse(C.QPointF(181, 169), 5, 5)
            
        # Gemini Connection Indicator (Green/Red dot)
        painter.setBrush(G.QColor("#57e2bb" if self.connected else "#ff737f"))
        painter.drawEllipse(C.QPointF(180, 40), 4, 4)
        for index in range(17):
            if self.state == "SPEAKING":
                height = 3 + abs(math.sin(self.phase + index * 0.71)) * 18
            elif self.state == "LISTENING":
                height = 3 + abs(math.cos(self.phase * 1.5 + index * 0.5)) * 10
            else:
                height = 3
            painter.drawRoundedRect(
                C.QRectF(44 + index * 8, 252 - height / 2, 4, height), 2, 2
            )

    def set_state(self, state, detail=""):
        if state != "reminder":
            self.base_state = state
        if time.monotonic() < self.alert_until:
            state, detail = "reminder", self.alert_text
        self.state = state
        self.setToolTip(detail or state.capitalize())
        self.buttons[0].setText("●" if self.muted else "■")
        self.buttons[1].setText("×" if self.output_muted else "♪")
        if self.isVisible() and state in (
            "LISTENING",
            "SPEAKING",
            "PROCESSING_TOOL",
            "reminder",
        ):
            self.animation.start()
        else:
            self.animation.stop()
        self.update()

    def tick(self):
        self.phase += 0.12
        if self.state == "reminder":
            if time.monotonic() >= self.alert_until:
                self.set_state(self.base_state)
            self.update()
        else:
            self.update(C.QRect(38, 240, 144, 24))

    def status_received(self, status):
        request = status.get("settings_request", 0)
        if request != self.settings_request:
            self.settings_request = request
            if request:
                self.open_settings()
        alerts = status.get("reminders", [])
        new_alerts = [item for item in alerts if item["id"] not in self.seen_reminders]
        if new_alerts:
            self.alert_text = "Reminder: " + "; ".join(
                item["title"] for item in new_alerts
            )
            self.alert_until = time.monotonic() + 10
            self.restore()
            self.play_notification()
            if self.tray:
                self.tray.showMessage("Kakarot reminder", self.alert_text)
        for item in alerts:
            self.seen_reminders.add(item["id"])
            self.bridge.command(f"ack_reminder:{item['id']}")
        # Retain only current pending IDs; acknowledgements remove them in SQLite.
        self.seen_reminders.intersection_update(item["id"] for item in alerts)
        self.muted = bool(status.get("muted", True))
        self.output_muted = bool(status.get("output_muted", False))
        self.connected = bool(status.get("connected", False))
        error = status.get("error", "")
        phase = status.get("phase")
        if not isinstance(phase, str) or phase not in PHASES:
            # A daemon that predates the coordinator: derive it here so the
            # overlay still works against an older socket.
            if error:
                phase = "ERROR"
            elif status.get("processing"):
                phase = "PROCESSING_TOOL"
            elif self.muted:
                phase = "IDLE_MUTED"
            elif not status.get("connected"):
                phase = "DISCONNECTED"
            else:
                phase = "SPEAKING" if status.get("speaking") else "LISTENING"
        state, detail = PHASES[phase]
        self.set_state(state, error or detail)

    def play_notification(self):
        try:
            if not hasattr(self, "notification_sound"):
                if os.environ.get("KAKAROT_QT_API", "pyside6").lower() == "pyqt6":
                    from PyQt6.QtMultimedia import QSoundEffect
                else:
                    from PySide6.QtMultimedia import QSoundEffect
                from kakarot_overlay.assets import resolve

                self.notification_sound = QSoundEffect(self)
                self.notification_sound.setSource(
                    C.QUrl.fromLocalFile(str(resolve("notification.wav")))
                )
                self.notification_sound.setVolume(0.5)
            self.notification_sound.play()
        except (ImportError, RuntimeError):
            W.QApplication.beep()

    def failure(self, message):
        self.set_state("ERROR", message)

    def command_done(self, command, response):
        if command.startswith("ack_reminder:"):
            return
        self.pending = False
        if command == "stop" and self.quitting:
            W.QApplication.quit()

    def command_error(self, command, message):
        if command.startswith("ack_reminder:"):
            return
        if command in ("mute", "unmute"):
            self.muted = command == "unmute"
        self.pending = False
        self.failure(message)

    def toggle_listening(self):
        if self.pending:
            return
        self.pending = True
        old = self.muted
        self.muted = not old
        self.set_state("IDLE_MUTED" if self.muted else "LISTENING")
        self.bridge.toggle_listening(old)

    def toggle_speaker(self):
        if not self.pending:
            self.pending = True
            self.bridge.command("speaker_on" if self.output_muted else "speaker_off")

    def open_settings(self):
        self.settings.open_anchored()

    def clamp(self):
        screen = (
            W.QApplication.screenAt(self.frameGeometry().center())
            or W.QApplication.primaryScreen()
        )
        rect = screen.availableGeometry()
        self.move(
            max(rect.left(), min(self.x(), rect.right() - self.width() + 1)),
            max(rect.top(), min(self.y(), rect.bottom() - self.height() + 1)),
        )

    def restore(self):
        self.clamp()
        self.show()
        self.raise_()
        if self.windowHandle():
            self.windowHandle().requestActivate()
        self.show_action.setText("Hide Overlay")
        self.set_state(self.state)
        self.persist()

    def toggle_visible(self):
        if self.isVisible():
            self.hide()
            self.animation.stop()
            self.show_action.setText("Show Overlay")
            self.persist()
        else:
            self.restore()

    def persist(self):
        try:
            save_preferences(self.x(), self.y(), not self.isVisible())
        except OSError:
            logging.getLogger(__name__).warning("Unable to save overlay position")

    def moveEvent(self, event):
        if hasattr(self, "persist_timer"):
            self.persist_timer.start()

    def mousePressEvent(self, event):
        if event.button() == C.Qt.MouseButton.RightButton:
            self.menu.popup(event.globalPosition().toPoint())
        elif (
            event.button() == C.Qt.MouseButton.LeftButton and event.position().y() < 240
        ):
            if self.windowHandle() and self.windowHandle().startSystemMove():
                return
            self.drag_offset = event.globalPosition().toPoint() - self.pos()
            self.grabMouse()
            self.setCursor(C.Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self.drag_offset)

    def mouseReleaseEvent(self, event):
        if self.drag_offset is not None:
            self.releaseMouse()
            self.drag_offset = None
            self.unsetCursor()
        self.clamp()

    def force_quit(self):
        self.persist()
        self.quitting = True
        self.bridge.command("stop")
        C.QTimer.singleShot(1600, W.QApplication.quit)

    def cleanup(self):
        self.persist()
        self.animation.stop()
        self.bridge.close()
        self.settings.hide()
        if self.tray:
            self.tray.hide()

    def closeEvent(self, event):
        event.ignore()
        self.toggle_visible()


def main():
    W.QApplication.setHighDpiScaleFactorRoundingPolicy(
        C.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = W.QApplication(sys.argv)
    app.setApplicationName("Kakarot")
    app.setDesktopFileName("kakarot")
    app.setQuitOnLastWindowClosed(False)
    runtime = (
        Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
        / f"kakarot-overlay-{os.getuid()}"
    )
    runtime.mkdir(mode=0o700, exist_ok=True)
    if runtime.is_symlink() or runtime.stat().st_uid != os.getuid():
        raise RuntimeError("Unsafe runtime directory")
    runtime.chmod(0o700)
    name = str(runtime / "instance.sock")
    lock = C.QLockFile(str(runtime / "instance.lock"))
    if not lock.tryLock(0):
        client = N.QLocalSocket()
        client.connectToServer(name)
        if client.waitForConnected(1000):
            client.write(b"show\n")
            client.waitForBytesWritten(1000)
        return 0
    N.QLocalServer.removeServer(name)
    server = N.QLocalServer()
    server.setSocketOptions(N.QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        return 1
    logdir = (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "kakarot"
    )
    logdir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        logdir / "overlay.log", maxBytes=262144, backupCount=2
    )
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    overlay = Overlay()

    def show_existing():
        client = server.nextPendingConnection()
        overlay.restore()
        client.close()
        client.deleteLater()

    server.newConnection.connect(show_existing)
    app.aboutToQuit.connect(overlay.cleanup)
    app.aboutToQuit.connect(server.close)
    app.aboutToQuit.connect(lock.unlock)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    pulse = C.QTimer()
    pulse.timeout.connect(lambda: None)
    pulse.start(250)
    if not overlay.preferences.get("hidden") or overlay.tray is None:
        overlay.show()
    if "--smoke-test" in sys.argv:
        C.QTimer.singleShot(500, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
