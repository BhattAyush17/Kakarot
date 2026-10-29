#!/usr/bin/env python3
"""Floating Kakarot assistant widget (PySide6, Linux).

A frameless, always-on-top, draggable glassmorphism widget: a circular avatar
with a pulsing state glow, plus a pill dock with mic / speaker / key / power
buttons. Voice and backend calls are placeholders wired for the Kakarot backend.

Run (from the repository root, needs a display)::

    .venv-light/bin/python scripts/assistant_widget.py

Use ``--daemon-pid <PID>`` so the power button also terminates the backend::

    .venv-light/bin/python scripts/assistant_widget.py --daemon-pid $(cat /tmp/kakarot-backend.pid)

Custom artwork (all optional — procedural fallbacks are generated at runtime):

* ``scripts/avatar.png`` (or ``scripts/assets/avatar.png``): the AI "face".
  Shown inside a circular mask. Recommended: 512x512 PNG with transparency.
* ``scripts/assets/icons/{mic,speaker,gear,power}.png``: button icons, drawn
  on transparency. Recommended: 96x96 PNG, light-colored glyph.
  Omit any file to use the built-in drawn glyph instead.

Linux notes:

* ``Qt.WindowStaysOnTopHint`` maps to ``_NET_WM_STATE_ABOVE`` on X11, which
  most compositors honor. True cross-workspace stickiness is *not* exposed by
  Qt; if ``wmctrl`` is installed this script best-effort requests
  ``sticky,above`` for its window, otherwise print a hint (fail-open).
* On Wayland/GNOME, "always on top" works for regular windows, but a true
  overlay-layer widget needs the layer-shell protocol (out of scope here).
* ``Qt.Tool`` keeps the widget out of the taskbar/pager on most window
  managers. Drop the flag if your WM hides ``Tool`` windows aggressively.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import traceback
import urllib.request
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QConicalGradient,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

SCRIPT_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# State palette
# --------------------------------------------------------------------------

IDLE_RING = QColor("#35E0FF")
LISTEN_RING = QColor("#2BFF88")
SPEAK_RING = QColor("#B388FF")
ICON_IDLE = QColor("#E8ECF1")
ICON_DIM = QColor("#8A93A6")
TEXT_DIM = "#9AA3B5"

AVATAR_SIZE = 132
BUTTON_SIZE = 46
ICON_PIXELS = 96  # render resolution for procedural glyphs


# --------------------------------------------------------------------------
# Procedural artwork (used when no local PNG is provided)
# --------------------------------------------------------------------------

def _icon_search_paths(name: str) -> list[Path]:
    return [
        SCRIPT_DIR / "assets" / "icons" / f"{name}.png",
        SCRIPT_DIR / f"{name}.png",
    ]


def _load_local_icon(name: str) -> QIcon | None:
    for path in _icon_search_paths(name):
        if path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return QIcon(pixmap)
    return None


def _drawn_glyph(kind: str, color: QColor) -> QPixmap:
    """Minimal single-color glyph painted on transparency."""
    pixmap = QPixmap(ICON_PIXELS, ICON_PIXELS)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(color, 7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    s = ICON_PIXELS
    cx, cy = s / 2, s / 2

    if kind == "mic":
        painter.drawRoundedRect(int(cx - 11), int(s * 0.14), 22, 36, 11, 11)
        painter.drawArc(int(cx - 22), int(s * 0.36), 44, 40, 200 * 16, 140 * 16)
        painter.drawLine(int(cx), int(s * 0.78), int(cx), int(s * 0.88))
        painter.drawLine(int(cx - 12), int(s * 0.88), int(cx + 12), int(s * 0.88))
    elif kind == "speaker":
        painter.drawPolyline(
            [QPoint(int(s * 0.22), int(s * 0.42)), QPoint(int(s * 0.38), int(s * 0.42))]
        )
        painter.drawPolygon(
            [
                QPoint(int(s * 0.38), int(s * 0.42)),
                QPoint(int(s * 0.38), int(s * 0.58)),
                QPoint(int(s * 0.22), int(s * 0.58)),
                QPoint(int(s * 0.22), int(s * 0.42)),
            ]
        )
        painter.drawLine(int(s * 0.38), int(s * 0.30), int(s * 0.56), int(s * 0.16))
        painter.drawLine(int(s * 0.38), int(s * 0.70), int(s * 0.56), int(s * 0.84))
        painter.drawLine(int(s * 0.56), int(s * 0.16), int(s * 0.56), int(s * 0.84))
        painter.drawArc(int(s * 0.58), int(s * 0.32), 22, 36, -60 * 16, 120 * 16)
    elif kind == "gear":
        painter.save()
        painter.translate(cx, cy)
        for angle in range(0, 360, 45):
            painter.save()
            painter.rotate(angle)
            painter.drawLine(0, -34, 0, -22)
            painter.restore()
        painter.restore()
        painter.drawEllipse(int(cx - 24), int(cy - 24), 48, 48)
        painter.drawEllipse(int(cx - 9), int(cy - 9), 18, 18)
    elif kind == "power":
        painter.drawArc(int(cx - 24), int(cy - 20), 48, 48, 60 * 16, 240 * 16)
        painter.drawLine(int(cx), int(cy - 34), int(cx), int(cy - 6))
    painter.end()
    return pixmap


def make_icon(kind: str, color: QColor) -> QIcon:
    """Local PNG wins; otherwise use the drawn glyph tinted ``color``."""
    local = _load_local_icon(kind)
    if local is not None:
        return local
    return QIcon(_drawn_glyph(kind, color))


def _load_avatar_face() -> QPixmap:
    """Local avatar.png wins; otherwise generate a gradient orb."""
    for candidate in (SCRIPT_DIR / "avatar.png", SCRIPT_DIR / "assets" / "avatar.png"):
        if candidate.is_file():
            pixmap = QPixmap(str(candidate))
            if not pixmap.isNull():
                return pixmap.scaled(
                    256, 256, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
    size = 256
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    gradient = QRadialGradient(size * 0.38, size * 0.34, size * 0.62)
    gradient.setColorAt(0.0, QColor("#BFF6FF"))
    gradient.setColorAt(0.35, QColor("#3FD2FF"))
    gradient.setColorAt(0.7, QColor("#2743B8"))
    gradient.setColorAt(1.0, QColor("#0B1030"))
    painter.setBrush(gradient)
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(8, 8, size - 16, size - 16)
    sheen = QRadialGradient(size * 0.34, size * 0.28, size * 0.30)
    sheen.setColorAt(0.0, QColor(255, 255, 255, 190))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.setBrush(sheen)
    painter.drawEllipse(0, 0, size, size)
    ring = QConicalGradient(size / 2, size / 2, 0)
    ring.setColorAt(0.0, QColor("#35E0FF"))
    ring.setColorAt(0.5, QColor("#B388FF"))
    ring.setColorAt(1.0, QColor("#35E0FF"))
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(ring, 7, Qt.SolidLine, Qt.RoundCap))
    painter.drawEllipse(12, 12, size - 24, size - 24)
    painter.end()
    return pixmap


# --------------------------------------------------------------------------
# Circular avatar label
# --------------------------------------------------------------------------

class CircularAvatar(QLabel):
    """QLabel that paints its pixmap inside a circle with a state ring."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
        self._face = _load_avatar_face()
        self._ring = QColor(IDLE_RING)

    def set_ring_color(self, color: QColor) -> None:
        self._ring = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:
        side = min(self.width(), self.height())
        pixmap = self._face.scaled(
            side, side, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addEllipse(4, 4, side - 8, side - 8)
        painter.setClipPath(path)
        x = (self.width() - pixmap.width()) // 2
        y = (self.height() - pixmap.height()) // 2
        painter.drawPixmap(x, y, pixmap)
        painter.setClipping(False)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(self._ring, 4, Qt.SolidLine, Qt.RoundCap))
        painter.drawEllipse(4, 4, side - 8, side - 8)
        painter.end()


# --------------------------------------------------------------------------
# Gemini API key popup
# --------------------------------------------------------------------------

def _read_key_configured() -> bool:
    try:
        sys.path.insert(0, str(SCRIPT_DIR.parent))
        from core.config import is_gemini_api_key_configured

        return bool(is_gemini_api_key_configured())
    except Exception:
        return False
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR.parent))
        except ValueError:
            pass


def _store_key(api_key: str) -> None:
    """Persist via the repo secret store, else keyring directly."""
    try:
        sys.path.insert(0, str(SCRIPT_DIR.parent))
        from core.config import set_gemini_api_key

        set_gemini_api_key(api_key)
        return
    except Exception:
        pass
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR.parent))
        except ValueError:
            pass
    import keyring  # PyPI dependency, already in requirements-light.txt

    keyring.set_password("Kakarot", "gemini_api_key", api_key)


class KeyDialog(QDialog):
    """Frameless popup to paste the Gemini API key (never shown in clear)."""

    def __init__(self, parent: QWidget | None = None, backend_url: str = "") -> None:
        # NOTE: plain Dialog, not Popup — Popup windows light-dismiss on any
        # outside click and can open off-screen unnoticed on some WMs.
        super().__init__(
            parent, Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(320)
        self._backend_url = backend_url.rstrip("/")

        frame = QFrame(self)
        frame.setObjectName("keyCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Gemini API key")
        title.setObjectName("keyTitle")
        layout.addWidget(title)

        self._field = QLineEdit()
        self._field.setEchoMode(QLineEdit.Password)
        self._field.setPlaceholderText("Paste key — stored in OS keyring")
        layout.addWidget(self._field)

        self._status = QLabel()
        self._status.setObjectName("keyStatus")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        self._refresh_status()

    def _refresh_status(self) -> None:
        configured = _read_key_configured()
        backend = self._probe_backend()
        self._status.setText(
            f"Key stored: {'yes' if configured else 'not yet'}\nBackend: {backend}"
        )

    def _probe_backend(self) -> str:
        if not self._backend_url:
            return "not configured"
        try:
            with urllib.request.urlopen(
                f"{self._backend_url}/health/ready", timeout=2
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return str(payload.get("status", "unknown"))
        except Exception as exc:
            return f"offline ({type(exc).__name__})"

    def _save(self) -> None:
        api_key = self._field.text().strip()
        if not api_key:
            self._status.setText("Paste a key first — nothing was saved.")
            return
        try:
            _store_key(api_key)
        except Exception as exc:  # keyring backends vary per desktop
            self._status.setText(f"Could not store key: {exc}")
            return
        self._field.clear()
        self._refresh_status()
        self.accept()


# --------------------------------------------------------------------------
# Main widget
# --------------------------------------------------------------------------

WIDGET_QSS = """
AssistantWidget { background: transparent; }
#dock {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(30, 34, 48, 215), stop:1 rgba(13, 15, 24, 215));
    border: 1px solid rgba(255, 255, 255, 36);
    border-radius: 26px;
}
#statusLabel { color: %s; font-size: 11px; }
QPushButton#roundBtn {
    background: rgba(255, 255, 255, 16);
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 23px;
}
QPushButton#roundBtn:hover {
    background: rgba(53, 224, 255, 46);
    border: 1px solid rgba(53, 224, 255, 160);
}
QPushButton#roundBtn[active="true"] {
    background: rgba(43, 255, 136, 52);
    border: 1px solid rgba(43, 255, 136, 200);
}
QPushButton#roundBtn[active="true"]:hover {
    background: rgba(43, 255, 136, 80);
}
#keyCard {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(30, 34, 48, 235), stop:1 rgba(13, 15, 24, 235));
    border: 1px solid rgba(53, 224, 255, 90);
    border-radius: 16px;
}
#keyTitle { color: #EDF1F7; font-size: 14px; font-weight: bold; }
#keyStatus { color: %s; font-size: 11px; }
QLineEdit {
    background: rgba(0, 0, 0, 110);
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 8px;
    color: #EDF1F7;
    padding: 8px;
    selection-background-color: rgba(53, 224, 255, 90);
}
QDialogButtonBox QPushButton {
    background: rgba(255, 255, 255, 16);
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 8px;
    color: #EDF1F7;
    padding: 6px 14px;
}
QDialogButtonBox QPushButton:hover {
    border: 1px solid rgba(53, 224, 255, 160);
}
""" % (TEXT_DIM, TEXT_DIM)


class AssistantWidget(QWidget):
    """Frameless always-on-top widget with avatar, glow, and control dock."""

    def __init__(
        self, daemon_pid: int | None = None, backend_url: str = ""
    ) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("Kakarot")
        self.setStyleSheet(WIDGET_QSS)

        self._daemon_pid = daemon_pid
        self._backend_url = backend_url
        self._drag_offset: QPoint | None = None
        self._btn_press_pos: QPoint | None = None
        self._btn_dragging = False
        self._drag_buttons: list[QPushButton] = []
        self._listening = False
        self._speaking = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Avatar with animated glow.
        glow = QGraphicsDropShadowEffect(self)
        glow.setColor(IDLE_RING)
        glow.setOffset(0, 0)
        glow.setBlurRadius(26)
        self._glow = glow
        self._avatar = CircularAvatar(self)
        self._avatar.setGraphicsEffect(glow)
        layout.addWidget(self._avatar, alignment=Qt.AlignHCenter)

        self._pulse = QPropertyAnimation(glow, b"blurRadius", self)
        self._pulse.setStartValue(16)
        self._pulse.setEndValue(38)
        self._pulse.setDuration(1600)
        self._pulse.setEasingCurve(QEasingCurve.InOutSine)
        self._pulse.setLoopCount(-1)
        self._pulse.start()

        # Pill dock with the four control buttons.
        dock = QFrame(self)
        dock.setObjectName("dock")
        dock_layout = QHBoxLayout(dock)
        dock_layout.setContentsMargins(12, 8, 12, 8)
        dock_layout.setSpacing(10)

        self._mic_btn = self._make_button("mic", "Toggle listening", self.toggle_listening)
        self._play_btn = self._make_button("speaker", "Toggle playback", self.toggle_playback)
        self._key_btn = self._make_button("gear", "Gemini API key", self.open_settings)
        self._power_btn = self._make_button("power", "Quit", self.power_off)
        for button in (
            self._mic_btn,
            self._play_btn,
            self._key_btn,
            self._power_btn,
        ):
            dock_layout.addWidget(button)
        layout.addWidget(dock)

        self._status = QLabel("idle")
        self._status.setObjectName("statusLabel")
        self._status.setAlignment(Qt.AlignHCenter)
        layout.addWidget(self._status)

        self.setFixedSize(220, 300)
        self._ensure_sticky()

    # -- construction helpers -------------------------------------------

    def _make_button(self, kind: str, tooltip: str, slot) -> QPushButton:
        button = QPushButton(self)
        button.setObjectName("roundBtn")
        button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        button.setIconSize(QSize(24, 24))
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        button.setProperty("iconKind", kind)
        button.setIcon(make_icon(kind, ICON_IDLE if kind != "power" else ICON_DIM))
        button.clicked.connect(slot)
        button.installEventFilter(self)
        self._drag_buttons.append(button)
        return button

    def _dialog_position(self, dialog: QDialog) -> QPoint:
        """Place the popup below the widget, or above it when space is short.

        Clamped to the available screen geometry so it can never open
        off-screen (which previously looked like "nothing happens").
        """
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else dialog.geometry()
        width = dialog.width() or dialog.sizeHint().width()
        height = dialog.height() or dialog.sizeHint().height()
        x = self.x() + (self.width() - width) // 2
        x = min(max(x, area.left()), max(area.left(), area.right() - width))
        below = self.y() + self.height() + 6
        if below + height <= area.bottom():
            return QPoint(x, below)
        return QPoint(x, max(area.top(), self.y() - height - 6))

    def _set_button_state(
        self, button: QPushButton, active: bool, color: QColor
    ) -> None:
        kind = str(button.property("iconKind") or "")
        # Local PNG icons are used as-is (recoloring a PNG is out of scope);
        # drawn glyphs are regenerated in the state color.
        if _load_local_icon(kind) is None:
            button.setIcon(make_icon(kind, color))
        button.setProperty("active", active)
        button.style().unpolish(button)
        button.style().polish(button)

    def _ensure_sticky(self) -> None:
        """Best-effort: ask the WM to keep the widget above and on all workspaces."""
        if shutil.which("wmctrl") is None:
            print("[Widget] wmctrl not found; enable 'Always on Top' / 'Sticky' "
                  "via your WM, e.g.: wmctrl -r Kakarot -b add,above,sticky")
            return
        QTimer.singleShot(500, self._apply_wmctrl)

    def _apply_wmctrl(self) -> None:
        try:
            subprocess.run(
                ["wmctrl", "-r", "Kakarot", "-b", "add,above,sticky"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass

    # -- frameless dragging ----------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.raise_()
            self.activateWindow()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None

    def eventFilter(self, watched, event) -> bool:
        """Let drags start on the dock buttons too.

        A press that moves past the system drag distance becomes a window
        move (and the release is swallowed so the button does not click);
        a press without movement passes through untouched.
        """
        if watched in self._drag_buttons:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._btn_press_pos = event.globalPosition().toPoint()
                self._btn_dragging = False
            elif (
                event.type() == QEvent.MouseMove
                and self._btn_press_pos is not None
                and event.buttons() & Qt.LeftButton
            ):
                delta = event.globalPosition().toPoint() - self._btn_press_pos
                if not self._btn_dragging and delta.manhattanLength() >= QApplication.startDragDistance():
                    self._btn_dragging = True
                    self._drag_offset = self._btn_press_pos - self.frameGeometry().topLeft()
                if self._btn_dragging:
                    self.move(event.globalPosition().toPoint() - self._drag_offset)
                    return True
            elif event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                dragging = self._btn_dragging
                self._btn_dragging = False
                self._btn_press_pos = None
                if dragging:
                    return True  # suppress the click after a drag
        return super().eventFilter(watched, event)

    # -- state + placeholder slots ----------------------------------------

    def _apply_state(self) -> None:
        if self._listening:
            ring, text = LISTEN_RING, "listening…"
        elif self._speaking:
            ring, text = SPEAK_RING, "speaking…"
        else:
            ring, text = IDLE_RING, "idle"
        self._avatar.set_ring_color(ring)
        self._glow.setColor(ring)
        self._set_button_state(
            self._mic_btn, self._listening, LISTEN_RING if self._listening else ICON_IDLE
        )
        self._set_button_state(
            self._play_btn, self._speaking, SPEAK_RING if self._speaking else ICON_IDLE
        )
        self._status.setText(text)

    def toggle_listening(self) -> None:
        """Placeholder: hook microphone capture here (e.g. open the /session socket)."""
        self._listening = not self._listening
        if self._listening:
            self._speaking = False
        print(f"[Widget] listening={'on' if self._listening else 'off'}")
        self._apply_state()

    def toggle_playback(self) -> None:
        """Placeholder: hook audio playback state here."""
        self._speaking = not self._speaking
        if self._speaking:
            self._listening = False
        print(f"[Widget] playback={'on' if self._speaking else 'off'}")
        self._apply_state()

    def open_settings(self) -> None:
        """Popup to paste the Gemini API key (stored in the OS keyring)."""
        print("[Widget] opening key dialog…")
        try:
            dialog = KeyDialog(self, backend_url=self._backend_url)
            dialog.move(self._dialog_position(dialog))
            dialog.exec()
        except Exception:
            traceback.print_exc()

    def power_off(self) -> None:
        """Quit the UI; also terminate the backend daemon when --daemon-pid was given."""
        if self._daemon_pid:
            try:
                os.kill(self._daemon_pid, signal.SIGTERM)
                print(f"[Widget] sent SIGTERM to backend pid {self._daemon_pid}")
            except Exception as exc:
                print(f"[Widget] could not stop backend pid: {exc}")
        QApplication.instance().quit()


# --------------------------------------------------------------------------

def _run_smoke_test() -> int:
    """Headless self-test: construct the widget, flip states, grab a frame."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QAbstractAnimation

    app = QApplication([])
    widget = AssistantWidget(backend_url="")
    widget.show()
    checks: list[tuple[str, bool]] = [
        ("avatar face loaded", not widget._avatar._face.isNull()),
        ("glow pulse running", widget._pulse.state() == QAbstractAnimation.Running),
    ]
    widget.toggle_listening()
    checks.append(("listening state", widget._listening))
    widget.toggle_listening()
    widget.toggle_playback()
    checks.append(("speaking state", widget._speaking))
    widget.toggle_playback()
    checks.append(("back to idle", widget._status.text() == "idle"))
    dialog = KeyDialog(widget, backend_url="")
    checks.append(("key dialog builds", "Backend" in dialog._status.text()))
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    # Popup must stay on-screen even with the widget at the bottom edge.
    screen = widget.screen() or QApplication.primaryScreen()
    area = screen.availableGeometry()
    widget.move(60, area.bottom() - widget.height() + 1)
    position = widget._dialog_position(dialog)
    dialog_rect = position + QPoint(dialog.width(), dialog.sizeHint().height())
    checks.append(
        ("key dialog stays on-screen", area.contains(position) and area.contains(dialog_rect)),
    )
    # Drag starting on a button moves the window and suppresses the click.
    widget.move(60, 60)
    origin = widget.mapToGlobal(widget._mic_btn.geometry().center())

    def mouse_event(kind, where):
        return QMouseEvent(
            kind, QPointF(5, 5), QPointF(float(where.x()), float(where.y())),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )

    before = widget.pos()
    widget.eventFilter(widget._mic_btn, mouse_event(QEvent.MouseButtonPress, origin))
    moved = origin + QPoint(120, 80)
    widget.eventFilter(widget._mic_btn, mouse_event(QEvent.MouseMove, moved))
    checks.append(("drag from button moves window", widget.pos() != before))
    swallowed = widget.eventFilter(
        widget._mic_btn, mouse_event(QEvent.MouseButtonRelease, moved)
    )
    checks.append(("release after drag suppresses click", bool(swallowed)))
    # A plain click (no movement) still reaches the button.
    widget.eventFilter(widget._mic_btn, mouse_event(QEvent.MouseButtonPress, origin))
    passed_through = widget.eventFilter(
        widget._mic_btn, mouse_event(QEvent.MouseButtonRelease, origin)
    )
    checks.append(("plain click passes through", not passed_through))
    icons_ok = all(
        not make_icon(kind, ICON_IDLE).isNull()
        for kind in ("mic", "speaker", "gear", "power")
    )
    checks.append(("all button icons render", icons_ok))
    frame = widget.grab()
    frame_ok = not frame.isNull() and frame.width() > 0
    checks.append(("frame grab renders", frame_ok))
    if frame_ok:
        frame.save("/tmp/widget-smoke.png")
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"[Smoke] {'PASS' if ok else 'FAIL'}  {name}")
    if failed:
        print(f"[Smoke] {len(failed)} check(s) failed.")
        return 1
    print("[Smoke] all checks passed (frame: /tmp/widget-smoke.png).")
    app.quit()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Floating Kakarot assistant widget.")
    parser.add_argument("--daemon-pid", type=int, default=None,
                        help="Backend PID the power button should terminate.")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000",
                        help="Backend base URL shown in the key popup.")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run the headless self-test and exit.")
    args = parser.parse_args(argv)
    if args.smoke_test:
        return _run_smoke_test()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    widget = AssistantWidget(daemon_pid=args.daemon_pid, backend_url=args.backend_url)
    widget.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
