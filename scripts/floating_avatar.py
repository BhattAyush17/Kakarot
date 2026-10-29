#!/usr/bin/env python3
"""Floating, always-on-top AI assistant avatar for Linux (PyQt6).

States
------
0  Settings  : Gemini API key entry + Save (first launch / no key on disk)
1  Active    : avatar image + Mic / Speaker / Power circular buttons

Run:
    python3 scripts/floating_avatar.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
AVATAR_PATH = BASE_DIR / "avatar.png"
CONFIG_PATH = Path.home() / ".config" / "floating_avatar" / "settings.json"

AVATAR_PX = 220          # on-screen avatar edge, in logical pixels
BUTTON_PX = 46           # circular control diameter
BUTTON_MARGIN = 6

GLOW = QColor("#2b7fff")
GLASS = QColor(24, 26, 34, 232)
INK = QColor("#e9ecf5")
MUTED = QColor("#98a0b3")
DANGER = QColor("#ff5f56")
OK = QColor("#3ddc84")


# --------------------------------------------------------------------------- #
# Small self-contained widgets
# --------------------------------------------------------------------------- #


class DragMixin:
    """Mixin: forward left-button drags on a child widget to the main window."""

    def _drag_owner(self):
        owner = self.window()
        return owner if isinstance(owner, FloatingAssistant) else None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        owner = self._drag_owner()
        if owner and event.button() == Qt.MouseButton.LeftButton:
            owner.begin_drag(event)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        owner = self._drag_owner()
        if owner and owner.dragging:
            owner.update_drag(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        owner = self._drag_owner()
        if owner and owner.dragging:
            owner.end_drag()
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class GlassPanel(DragMixin, QFrame):
    """Rounded translucent card used for the settings state. Also a drag handle."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setStyleSheet(
            """
            GlassPanel {
                background: rgba(24, 26, 34, 232);
                border: 1px solid rgba(255, 255, 255, 34);
                border-radius: 18px;
            }
            """
        )


class DragPanel(DragMixin, QWidget):
    """Transparent pane whose body area is a drag handle."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


class CircleButton(QToolButton):
    """Round icon button with hover + active glow."""

    def __init__(
        self,
        glyph: str,
        color: str = "#e9ecf5",
        checkable: bool = False,
        tooltip: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._glyph = glyph
        self._color = QColor(color)
        self.setCheckable(checkable)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # QToolButton defaults to ToolButtonIconOnly, which would hide the glyph.
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setFixedSize(BUTTON_PX, BUTTON_PX)
        self._apply(self.isChecked())

    def _apply(self, on: bool) -> None:
        fg = self._color.name() if not on else "#0d0f16"
        self.setText(self._glyph)
        self.setStyleSheet(
            f"""
            QToolButton {{
                background: {"#2b7fff" if on else "rgba(255, 255, 255, 26)"};
                color: {fg};
                border: 1px solid {"#2b7fff" if on else "rgba(255, 255, 255, 46)"};
                border-radius: {BUTTON_PX // 2}px;
                font-size: 15px;
                font-weight: 600;
            }}
            QToolButton:hover {{
                background: {"#1a5fd6" if on else "rgba(255, 255, 255, 48)"};
            }}
            QToolButton:pressed {{
                background: {"#124aae" if on else "rgba(255, 255, 255, 70)"};
            }}
            """
        )

    def setChecked(self, checked: bool) -> None:  # type: ignore[override]
        super().setChecked(checked)
        self._apply(checked)


class AvatarView(QWidget):
    """Transparent image holder. Click anywhere on the picture to drag."""

    def __init__(self, pixmap: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(AVATAR_PX, AVATAR_PX)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._scaled = self._fit(pixmap)
        self._ring = 0.0  # 0..1 pulse amount, set by the talk animation

    @staticmethod
    def _fit(src: QPixmap) -> QPixmap:
        if src.isNull():
            blank = QPixmap(AVATAR_PX, AVATAR_PX)
            blank.fill(Qt.GlobalColor.transparent)
            return blank
        return src.scaled(
            AVATAR_PX,
            AVATAR_PX,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def set_source(self, pixmap: QPixmap) -> None:
        self._scaled = self._fit(pixmap)
        self.update()

    def set_ring(self, amount: float) -> None:
        self._ring = max(0.0, min(1.0, amount))
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        if self._ring > 0.01:
            halo = QRectF(self.rect()).adjusted(-6, -6, 6, 6)
            c = QColor(GLOW)
            c.setAlphaF(0.55 * self._ring)
            p.setPen(c)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(halo)

        if not self._scaled.isNull():
            x = (self.width() - self._scaled.width()) // 2
            y = (self.height() - self._scaled.height()) // 2
            p.drawPixmap(QPoint(x, y), self._scaled)
        p.end()

    # -- the picture itself is the primary drag surface ------------------- #

    def mousePressEvent(self, event: QMouseEvent) -> None:
        owner = self.window()
        if isinstance(owner, FloatingAssistant) and event.button() == Qt.MouseButton.LeftButton:
            owner.begin_drag(event)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        owner = self.window()
        if isinstance(owner, FloatingAssistant) and owner.dragging:
            owner.update_drag(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        owner = self.window()
        if isinstance(owner, FloatingAssistant) and owner.dragging:
            owner.end_drag()
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ToggleButton(QPushButton):
    """Frameless drag handle that also toggles the top-most flag."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.setCheckable(True)
        self.setChecked(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedHeight(22)
        self.setStyleSheet(
            """
            ToggleButton {
                background: rgba(255, 255, 255, 14);
                color: #e9ecf5;
                border: 1px solid rgba(255, 255, 255, 30);
                border-radius: 11px;
                font-size: 11px;
                padding: 0 10px;
            }
            ToggleButton:hover { background: rgba(255, 255, 255, 30); }
            ToggleButton:checked { background: rgba(43, 127, 255, 90); }
            """
        )
        self.toggled.connect(self._pin_changed)

    def _pin_changed(self, pinned: bool) -> None:
        owner = self.window()
        if isinstance(owner, FloatingAssistant):
            owner.set_pinned(pinned)
            self.setText("Pinned" if pinned else "Unpinned")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            owner = self.window()
            if isinstance(owner, FloatingAssistant):
                owner.begin_drag(event)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                # Swallow the click so pressing the handle does not untoggle it.
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        owner = self.window()
        if isinstance(owner, FloatingAssistant) and owner.dragging:
            owner.update_drag(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        owner = self.window()
        if isinstance(owner, FloatingAssistant) and owner.dragging:
            owner.end_drag()
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------------- #
# Persisted state
# --------------------------------------------------------------------------- #


def load_settings() -> dict:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> bool:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #


class FloatingAssistant(QWidget):
    STATE_SETTINGS = 0
    STATE_ACTIVE = 1

    def __init__(self) -> None:
        super().__init__(None)

        self.setWindowTitle("Kakarot Floating Assistant")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setWindowIcon(self._load_avatar_pixmap(64))
        self.setMinimumSize(320, 260)

        # --- drag state -------------------------------------------------- #
        self._drag_active = False
        self._drag_origin: QPoint | None = None
        self._drag_offset: QPoint | None = None

        # --- persistence -------------------------------------------------- #
        self._settings = load_settings()
        self._api_key: str = str(self._settings.get("gemini_api_key", "") or "")

        # --- root layout -------------------------------------------------- #
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        self.stack = QStackedWidget(self)
        self.stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        root.addWidget(self.stack)

        self._build_settings_page()
        self._build_active_page()

        self.pin_toggle = ToggleButton("Pinned", self)
        root.addWidget(self.pin_toggle, 0, Qt.AlignmentFlag.AlignHCenter)

        self._pulse = QTimer(self)
        self._pulse.setInterval(60)
        self._pulse.timeout.connect(self._on_pulse)

        self.restore_geometry()
        self.stack.setCurrentIndex(
            self.STATE_ACTIVE if self._api_key else self.STATE_SETTINGS
        )
        self.resize(self.sizeHint())

    # ------------------------------------------------------------------ UI #

    def _build_settings_page(self) -> None:
        card = GlassPanel(self.stack)
        outer = QVBoxLayout(card)
        outer.setContentsMargins(20, 22, 20, 20)
        outer.setSpacing(12)

        title = QLabel("Gemini API key", card)
        title.setFont(QFont("Sans Serif", 15, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color: {INK.name()}; background: transparent;")
        outer.addWidget(title)

        hint = QLabel(
            "Paste the key from Google AI Studio. It is stored locally in "
            f"<code>{CONFIG_PATH}</code> with 0600 permissions.",
            card,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED.name()}; font-size: 11px; background: transparent;")
        outer.addWidget(hint)

        self.key_input = QLineEdit(card)
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("AIza…")
        self.key_input.setText(self._api_key)
        self.key_input.setClearButtonEnabled(True)
        self.key_input.setStyleSheet(
            """
            QLineEdit {
                background: rgba(0, 0, 0, 90);
                color: #e9ecf5;
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 10px;
                padding: 9px 11px;
                selection-background-color: #2b7fff;
            }
            QLineEdit:focus { border-color: #2b7fff; }
            """
        )
        self.key_input.returnPressed.connect(self.on_save_clicked)
        outer.addWidget(self.key_input)

        self.remember_box = QCheckBox("Remember key on this machine", card)
        self.remember_box.setChecked(bool(self._settings.get("remember", True)))
        self.remember_box.setStyleSheet(
            f"color: {INK.name()}; font-size: 11px; background: transparent;"
        )
        outer.addWidget(self.remember_box)

        self.save_button = QPushButton("Save", card)
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setDefault(True)
        self.save_button.setStyleSheet(
            """
            QPushButton {
                background: #2b7fff;
                color: #0d0f16;
                border: none;
                border-radius: 10px;
                padding: 10px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton:hover  { background: #1a5fd6; }
            QPushButton:pressed{ background: #124aae; }
            """
        )
        self.save_button.clicked.connect(self.on_save_clicked)
        outer.addWidget(self.save_button)

        self.status_label = QLabel("", card)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            f"color: {OK.name()}; font-size: 11px; background: transparent;"
        )
        outer.addWidget(self.status_label)

        self.stack.addWidget(card)

    def _build_active_page(self) -> None:
        holder = DragPanel(self.stack)

        col = QVBoxLayout(holder)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(BUTTON_MARGIN)
        col.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.avatar = AvatarView(self._load_avatar_pixmap(), holder)
        col.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(BUTTON_MARGIN + 2)
        row.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.mic_button = CircleButton("●", "#ff5f56", True, "Toggle listening", holder)
        self.speaker_button = CircleButton(
            "▶", "#3ddc84", True, "Talk-back indicator", holder
        )
        self.power_button = CircleButton("⏻", "#ff5f56", False, "Close assistant", holder)

        self.mic_button.toggled.connect(self.on_mic_toggled)
        self.speaker_button.toggled.connect(self.on_speaker_toggled)
        self.power_button.clicked.connect(self.on_power_clicked)

        for btn in (self.mic_button, self.speaker_button, self.power_button):
            row.addWidget(btn)
        col.addLayout(row)

        self.hint_label = QLabel("Drag the avatar anywhere · Alt+Tab to switch windows", holder)
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setStyleSheet(
            f"color: {MUTED.name()}; font-size: 10px; background: transparent;"
        )
        col.addWidget(self.hint_label)

        self.stack.addWidget(holder)

    # -------------------------------------------------------------- assets #

    def _load_avatar_pixmap(self, size: int | None = None):
        path = AVATAR_PATH
        if not path.is_file():
            base = Path(__file__).resolve().parent / "assets" / "avatar.png"
            if base.is_file():
                path = base
        pm = QPixmap(str(path)) if path.is_file() else QPixmap()
        if size and not pm.isNull():
            pm = pm.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return pm

    # -------------------------------------------------------------- window #

    def showEvent(self, event: QEvent) -> None:
        super().showEvent(event)
        self._clamp_to_screen()

    def set_pinned(self, pinned: bool) -> None:
        """Re-apply the window flags. Unpinning drops WindowStaysOnTopHint and
        switches to a Tool window so it no longer occupies a taskbar entry."""
        flags = Qt.WindowType.FramelessWindowHint
        if pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Window
        else:
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.show()
        self._clamp_to_screen()

    def save_geometry(self) -> None:
        self._settings["pos_x"] = self.x()
        self._settings["pos_y"] = self.y()
        self._settings["pinned"] = self.pin_toggle.isChecked()
        self._settings["api_key_saved"] = bool(self._api_key)
        save_settings(self._settings)

    def restore_geometry(self) -> None:
        pinned = self._settings.get("pinned", True)
        self.pin_toggle.blockSignals(True)
        self.pin_toggle.setChecked(bool(pinned))
        self.pin_toggle.blockSignals(False)
        self.pin_toggle.setText("Pinned" if pinned else "Unpinned")
        self.set_pinned(bool(pinned))
        if "pos_x" in self._settings and "pos_y" in self._settings:
            self.move(int(self._settings["pos_x"]), int(self._settings["pos_y"]))

    def _clamp_to_screen(self) -> None:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        geo = self.frameGeometry()
        x = min(max(geo.left(), area.left()), area.right() - geo.width() + 1)
        y = min(max(geo.top(), area.top()), area.bottom() - geo.height() + 1)
        self.move(x, y)

    def closeEvent(self, event) -> None:
        self.save_geometry()
        super().closeEvent(event)

    # ------------------------------------------------------------- dragging #

    def begin_drag(self, event: QMouseEvent) -> None:
        """Latch onto the press. No mouse grab: the pressed widget keeps it,
        so move/release events keep arriving at that widget, not the window."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_active = True
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def update_drag(self, event: QMouseEvent) -> None:
        if not self._drag_active or self._drag_offset is None:
            return
        # event.globalPosition().toPoint() stays correct across X11/Wayland
        # compositors and across fractional-DPI changes, unlike QCursor.pos().
        target = event.globalPosition().toPoint() - self._drag_offset
        self.move(self._clamp_point(target))

    def end_drag(self) -> None:
        self._drag_active = False
        self._drag_offset = None

    def _clamp_point(self, pt: QPoint) -> QPoint:
        screen = QApplication.screenAt(pt) or QApplication.primaryScreen()
        if screen is None:
            return pt
        area = screen.availableGeometry()
        # Allow partial off-screen travel so you can park the widget on an edge.
        x = min(max(pt.x(), area.left() - 40), area.right() - 40)
        y = min(max(pt.y(), area.top() - 20), area.bottom() - 20)
        return QPoint(x, y)

    # Fallbacks so the window itself stays draggable even if a child eats the
    # press (e.g. a stylesheet-backed frame edge).
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.begin_drag(event)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_active:
            self.update_drag(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_active:
            self.end_drag()
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    @property
    def dragging(self) -> bool:
        return self._drag_active

    # ------------------------------------------------------------- actions #

    def on_save_clicked(self) -> None:
        key = self.key_input.text().strip()
        if not key:
            self.status_label.setStyleSheet(
                f"color: {DANGER.name()}; font-size: 11px; background: transparent;"
            )
            self.status_label.setText("A key is required before the assistant can start.")
            return
        if len(key) < 20 or " " in key:
            self.status_label.setStyleSheet(
                f"color: {DANGER.name()}; font-size: 11px; background: transparent;"
            )
            self.status_label.setText("That does not look like a valid Gemini key.")
            return

        self._api_key = key
        remember = self.remember_box.isChecked()
        self._settings["remember"] = remember
        if remember:
            self._settings["gemini_api_key"] = key
        else:
            self._settings.pop("gemini_api_key", None)
        self._settings["api_key_saved"] = True
        self._settings["pos_x"] = self.x()
        self._settings["pos_y"] = self.y()

        if not save_settings(self._settings):
            QMessageBox.warning(
                self,
                "Save failed",
                f"Could not write {CONFIG_PATH}. The key was not persisted.",
            )
            return

        self.status_label.setStyleSheet(
            f"color: {OK.name()}; font-size: 11px; background: transparent;"
        )
        self.status_label.setText("Saved. Starting the assistant…")
        self.stack.setCurrentIndex(self.STATE_ACTIVE)
        self.hint_label.setText("Ready · drag me anywhere")
        self.resize(self.sizeHint())
        QTimer.singleShot(600, lambda: self.mic_button.setChecked(True))

    def on_mic_toggled(self, checked: bool) -> None:
        self.hint_label.setText(
            "Listening… (wire up your STT here)" if checked else "Microphone off"
        )
        if not self.speaker_button.isChecked():
            self.speaker_button.setChecked(False)

    def on_speaker_toggled(self, checked: bool) -> None:
        if checked:
            self._pulse.start()
            self.hint_label.setText("Speaking…")
        else:
            self._pulse.stop()
            self.avatar.set_ring(0.0)
            self.hint_label.setText(
                "Listening… (wire up your STT here)"
                if self.mic_button.isChecked()
                else "Idle"
            )

    def on_power_clicked(self) -> None:
        self._pulse.stop()
        self.save_geometry()
        self.close()
        QApplication.instance().quit()

    # ----------------------------------------------------------- animation #

    def _on_pulse(self) -> None:
        phase = (self._pulse.elapsed() % 1600) / 1600.0
        self.avatar.set_ring(0.5 + 0.5 * abs(1 - 2 * phase))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _high_dpi() -> None:
    """Enable fractional/high-DPI scaling before QApplication exists."""
    for name in ("AA_EnableHighDpiScaling", "AA_UseHighDpiPixmaps"):
        attr = getattr(Qt.ApplicationAttribute, name, None)
        if attr is not None:
            QApplication.setAttribute(attr, True)


def main() -> int:
    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    _high_dpi()
    app = QApplication(sys.argv)
    app.setApplicationName("FloatingAssistant")
    app.setApplicationDisplayName("Kakarot Assistant")
    app.setQuitOnLastWindowClosed(True)

    window = FloatingAssistant()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
