#!/usr/bin/env python3
"""Kakarot Floating Assistant — frameless, always-on-top desktop companion.

Two states inside a QStackedWidget:
  1. Settings : Gemini API key entry (first launch / no key on disk)
  2. Active   : hero image + Mic / Speaker controls

Designed to be frozen with PyInstaller into a single native binary. All
resource lookups go through resolve_path(), which understands both the
frozen (sys._MEIPASS) and the plain-source layouts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtNetwork import QLocalSocket
from PyQt6.QtDBus import QDBusConnection

#: One product, one name. The floating widget is an implementation detail of the
#: Kakarot front end, so neither the display name nor the ID advertises a second
#: "assistant" product alongside the GTK app.
APP_NAME = "Kakarot"
#: The floating frontend and launcher share the product's reverse-DNS identity.
APP_ID = "io.github.kakarot"
ORG_NAME = "Kakarot"

#: Config written by earlier builds, read once so an upgrade keeps the saved key
#: and window position instead of silently dropping back to the setup screen.
LEGACY_CONFIG_PATH = Path.home() / ".config" / "kakarot-floating-assistant" / "settings.json"

#: Primary hero image. First candidate wins if present. The preferred file is
#: looked up by exact name; the rest are fallbacks so the app still renders when
#: only a generic asset is on hand.
IMAGE_CANDIDATES = (
    "kakarot_cover.png",
    "assets/kakarot_cover.png",
    "watermarked_img_15057877357388561413.png",
    "watermarked_img_15057877357388561413@2x.png",
    "assets/watermarked_img_15057877357388561413.png",
    "scripts/watermarked_img_15057877357388561413.png",
    "avatar.png",
    "assets/avatar.png",
    "scripts/avatar.png",
)

CONFIG_DIR = Path.home() / ".config" / "kakarot"
CONFIG_PATH = CONFIG_DIR / "settings.json"
PREVIOUS_CONFIG_PATH = Path.home() / ".config" / "io.github.kakarot.mascot" / "settings.json"

HERO_PX = 208          # hero image edge, logical px
MASCOT_PX = 150        # compact mascot edge used by the context-menu workflow
BTN_PX = 52            # circular control diameter
BTN_GAP = 12
BUBBLE_MS = 5000       # speech bubble auto-hide
BUBBLE_MAX_PX = 300    # speech bubble wrap width before it grows taller
EXEC_POLL_MS = 400     # sandbox job poll interval
EXEC_POLL_LIMIT = 50   # ~20s ceiling on one sandboxed command


# --------------------------------------------------------------------------- #
# Resource resolution — works frozen (PyInstaller) and from source
# --------------------------------------------------------------------------- #

def _search_roots() -> list[Path]:
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    here = Path(__file__).resolve().parent
    roots += [here, here.parent, Path.cwd()]
    return roots


def resolve_path(*candidates: str) -> Path | None:
    """First existing file among `candidates`, searched across every root."""
    for root in _search_roots():
        for name in candidates:
            target = root / name
            if target.is_file():
                return target
    return None


def hero_pixmap() -> QPixmap:
    found = resolve_path(*IMAGE_CANDIDATES)
    if found is None:
        sys.stderr.write(
            f"[{APP_ID}] hero image not found; tried {IMAGE_CANDIDATES} in "
            f"{[str(r) for r in _search_roots()]}\n"
        )
        return QPixmap()
    pm = QPixmap(str(found))
    if pm.isNull():
        sys.stderr.write(f"[{APP_ID}] failed to decode {found}\n")
    return pm


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    for path in (CONFIG_PATH, PREVIOUS_CONFIG_PATH, LEGACY_CONFIG_PATH):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data:
            return data
    return {}


def save_config(data: dict) -> bool:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(CONFIG_PATH, 0o600)  # the file holds a secret
        except OSError:
            pass
        return True
    except OSError as exc:
        sys.stderr.write(f"[{APP_ID}] cannot write {CONFIG_PATH}: {exc}\n")
        return False


# --------------------------------------------------------------------------- #
# Theme — dark by default, light when the desktop asks for it
# --------------------------------------------------------------------------- #

class Theme:
    def __init__(self, dark: bool = True) -> None:
        self.dark = dark
        # Keep the configuration surface and its status treatments matte and
        # monochrome regardless of the desktop's light/dark preference.
        self.bg = QColor("#121212")
        self.stroke = QColor("#333333")
        self.sheet = QColor("#FFFFFF")
        self.dim = QColor("#CCCCCC")
        self.accent = QColor("#FFFFFF")
        self.accent_deep = QColor("#000000")
        self.on_accent = QColor("#000000")
        self.danger = QColor("#FFFFFF")
        self.ok = QColor("#FFFFFF")

    def qss(self) -> str:
        return f"""
        QWidget {{
            color: #FFFFFF;
            font-family: "Inter", "Cantarell", "Noto Sans", sans-serif;
            font-size: 13px;
        }}
        #card {{
            background: #121212;
            border: 1px solid #333333;
            border-radius: 20px;
        }}
        #title {{
            font-size: 16px;
            font-weight: 700;
            background: transparent;
        }}
        #subtitle, #hint {{
            color: #CCCCCC;
            font-size: 11px;
            background: transparent;
        }}
        #apiKey {{
            background: #222222;
            color: #FFFFFF;
            border: 1px solid #444444;
            border-radius: 12px;
            padding: 11px 13px;
            selection-background-color: #FFFFFF;
            selection-color: #000000;
        }}
        #apiKey:hover  {{ border-color: #777777; }}
        #apiKey:focus  {{ border-color: #FFFFFF; background: #222222; }}
        #save {{
            background: #FFFFFF;
            color: #000000;
            border: 1px solid #FFFFFF;
            border-radius: 12px;
            padding: 11px;
            font-size: 13px;
            font-weight: 700;
        }}
        #save:hover   {{ background: #000000; color: #FFFFFF; border: 1px solid #FFFFFF; }}
        #save:pressed {{ background: #222222; color: #FFFFFF; border: 1px solid #FFFFFF; }}
        #save:disabled {{ background: #444444; color: #CCCCCC; border: 1px solid #555555; }}
        #remember {{
            color: #FFFFFF;
            font-size: 11px;
            background: transparent;
            spacing: 6px;
        }}
        #remember::indicator {{
            width: 15px; height: 15px;
            border-radius: 2px;
            border: 1px solid #666666;
            background: #222222;
        }}
        #remember::indicator:checked {{
            background: #FFFFFF;
            border-color: #FFFFFF;
        }}
        #status {{
            color: #FFFFFF;
            font-size: 11px;
            background: transparent;
        }}
        #pin {{
            background: #000000;
            color: #FFFFFF;
            border: 1px solid #FFFFFF;
            border-radius: 11px;
            padding: 3px 11px;
            font-size: 10px;
        }}
        #pin:hover   {{ background: #222222; }}
        #pin:checked {{ background: #000000; color: #FFFFFF; border-color: #FFFFFF; }}
        """


def system_prefers_dark() -> bool:
    """Follow the desktop colour scheme; default to dark when unknown."""
    scheme = QGuiApplication.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


# --------------------------------------------------------------------------- #
# Vector glyph icons — drawn, so no font/emoji dependency can break them
# --------------------------------------------------------------------------- #

def _blank(size: int) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(Qt.BrushStyle.NoBrush)
    return pm, p


def _stroke(p: QPainter, color: QColor, w: float) -> None:
    pen = QPen(color)
    pen.setWidthF(w)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)


def mic_pixmap(color: QColor, size: int) -> QPixmap:
    pm, p = _blank(size)
    s = size / 24.0
    _stroke(p, color, 1.9 * s)
    p.drawLine(QPointF(12 * s, 3.2 * s), QPointF(12 * s, 11.4 * s))          # capsule
    p.drawArc(QRectF(6.6 * s, 8.0 * s, 10.8 * s, 8.2 * s), 0, -180 * 16)     # bracket
    p.drawLine(QPointF(12 * s, 16.2 * s), QPointF(12 * s, 19.6 * s))          # stem
    p.drawLine(QPointF(8.4 * s, 19.8 * s), QPointF(15.6 * s, 19.8 * s))       # base
    p.end()
    return pm


def speaker_pixmap(color: QColor, size: int) -> QPixmap:
    pm, p = _blank(size)
    s = size / 24.0
    _stroke(p, color, 1.9 * s)
    cone = QPainterPath()
    cone.moveTo(3.2 * s, 9.2 * s)
    cone.lineTo(7.0 * s, 9.2 * s)
    cone.lineTo(11.2 * s, 5.2 * s)
    cone.lineTo(11.2 * s, 18.8 * s)
    cone.lineTo(7.0 * s, 14.8 * s)
    cone.lineTo(3.2 * s, 14.8 * s)
    cone.closeSubpath()
    p.drawPath(cone)
    p.drawArc(QRectF(12.2 * s, 7.4 * s, 7.2 * s, 9.2 * s), -60 * 16, 120 * 16)  # wave 1
    p.drawArc(QRectF(15.0 * s, 5.2 * s, 9.0 * s, 13.6 * s), -52 * 16, 104 * 16) # wave 2
    p.end()
    return pm


def power_pixmap(color: QColor, size: int) -> QPixmap:
    pm, p = _blank(size)
    s = size / 24.0
    _stroke(p, color, 1.9 * s)
    p.drawArc(QRectF(4.4 * s, 4.4 * s, 15.2 * s, 15.2 * s), 62 * 16, 236 * 16)
    p.drawLine(QPointF(12 * s, 2.4 * s), QPointF(12 * s, 11.0 * s))
    p.end()
    return pm


# --------------------------------------------------------------------------- #
# Animated circular control
# --------------------------------------------------------------------------- #

class GlowButton(QWidget):
    """Circular control. Custom-painted because QSS has no transition support —
    this way hover / press / active states interpolate smoothly."""

    toggled = pyqtSignal(bool)
    clicked = pyqtSignal()

    def __init__(
        self,
        glyph: callable[[QColor, int], QPixmap],
        theme: Theme,
        checkable: bool = False,
        tooltip: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._glyph = glyph
        self._theme = theme
        self._hover = 0.0
        self._press = 0.0
        self._checked = False
        self._checkable = checkable

        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFixedSize(BTN_PX, BTN_PX)

        self._icon = glyph(theme.sheet, int(BTN_PX * 0.5))
        self._anim = self._fade(0.0, 0.0, self._apply_hover)
        self._press_anim = self._fade(0.0, 0.0, self._apply_press)

    # -- animation plumbing ------------------------------------------------ #

    def _fade(self, start: float, end: float, slot) -> QVariantAnimation:
        a = QVariantAnimation(self)
        a.setDuration(120)
        a.setStartValue(float(start))
        a.setEndValue(float(end))
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.valueChanged.connect(slot)
        return a

    def _apply_hover(self, v) -> None:
        self._hover = float(v)
        self.update()

    def _apply_press(self, v) -> None:
        self._press = float(v)
        self.update()

    def setChecked(self, checked: bool) -> None:
        """GlowButton is a plain QWidget, so it has no inherited check state —
        track it ourselves and only emit when it actually changes."""
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self.update()
        self.toggled.emit(checked)

    def toggle(self) -> None:
        self.setChecked(not self._checked)  # emits toggled() from setChecked
        self.clicked.emit()

    def isChecked(self) -> bool:
        return self._checked

    # -- input ------------------------------------------------------------- #

    def enterEvent(self, event) -> None:
        self._anim.setEndValue(1.0)
        self._anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._anim.setEndValue(0.0)
        self._anim.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_anim.setEndValue(1.0)
            self._press_anim.start()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_anim.setEndValue(0.0)
            self._press_anim.start()
            if self.rect().contains(event.position().toPoint()):
                self.toggle()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- paint ------------------------------------------------------------- #

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        active = self.isChecked()
        th = self._theme
        lit = max(self._hover, 1.0 if active else 0.0)
        shrink = 1.0 - 0.05 * self._press

        radius = (self.width() / 2.0) * shrink
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        disc = QRectF(
            center.x() - radius, center.y() - radius, radius * 2, radius * 2
        )

        # outer glow
        if lit > 0.01:
            halo = QColor(th.accent if active else th.sheet)
            halo.setAlphaF(0.26 * lit)
            glow = disc.adjusted(-5, -5, 5, 5)
            p.setPen(QPen(halo, 6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(glow)
            p.setPen(Qt.PenStyle.NoPen)

        # face
        face = QColor(th.accent) if active else QColor(
            255, 255, 255, int(20 + 34 * lit)
        ) if th.dark else QColor(
            15, 17, 22, int(14 + 30 * lit)
        )
        if not active and self._press > 0.01:
            face = face.darker(108)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(face)
        p.drawEllipse(disc)

        # ring
        ring = QColor(th.accent if active else th.stroke)
        ring.setAlphaF(0.9 if active else 0.55 + 0.45 * lit)
        p.setPen(QPen(ring, 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(disc.adjusted(-0.7, -0.7, 0.7, 0.7))

        # glyph
        glyph_color = th.on_accent if active else (
            th.sheet if th.dark else QColor("#11141c")
        )
        icon = self._glyph(glyph_color, self._icon.width())
        x = (self.width() - icon.width()) / 2.0
        y = (self.height() - icon.height()) / 2.0
        p.drawPixmap(QPointF(x, y), icon)
        p.end()


# --------------------------------------------------------------------------- #
# Hero image
# --------------------------------------------------------------------------- #

class HeroView(QWidget):
    """Transparent image surface. A left-press anywhere on it starts a drag."""

    def __init__(self, source: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(HERO_PX, HERO_PX)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._glow = 0.0
        self._set_source(source)

    def _set_source(self, source: QPixmap) -> None:
        if source.isNull():
            self._scaled = QPixmap()
            return
        self._scaled = source.scaled(
            HERO_PX,
            HERO_PX,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def set_glow(self, amount: float) -> None:
        amount = max(0.0, min(1.0, amount))
        if abs(amount - self._glow) > 0.01:
            self._glow = amount
            self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        if self._glow > 0.01:
            ring = QColor("#3d8bff")
            ring.setAlphaF(0.5 * self._glow)
            halo = QRectF(self.rect()).adjusted(-5, -5, 5, 5)
            p.setPen(QPen(ring, 2.0 + 3.0 * self._glow))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(halo)

        if not self._scaled.isNull():
            x = (self.width() - self._scaled.width()) / 2.0
            y = (self.height() - self._scaled.height()) / 2.0
            p.drawPixmap(QPointF(x, y), self._scaled)
        p.end()

    # -- drag forwarding ---------------------------------------------------- #

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            win = self.window()
            if hasattr(win, "begin_drag"):
                win.begin_drag(event)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        win = self.window()
        if getattr(win, "dragging", False):
            win.update_drag(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        win = self.window()
        if getattr(win, "dragging", False):
            win.end_drag()
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class DragSurface:
    """Mixin: makes a container's empty body act as a drag handle."""

    def _host(self):
        win = self.window()
        return win if hasattr(win, "begin_drag") else None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            host = self._host()
            if host is not None:
                host.begin_drag(event)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        host = self._host()
        if host is not None and host.dragging:
            host.update_drag(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        host = self._host()
        if host is not None and host.dragging:
            host.end_drag()
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class Card(DragSurface, QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.OpenHandCursor)


class Panel(DragSurface, QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class SpeechBubble(QWidget):
    """Frameless, translucent caption that floats beside the mascot.

    Kept as its own top-level window rather than a child widget: a bubble that
    sits *above* the mascot would otherwise be clipped by the mascot window's
    own bounds, and a separate surface can be positioned past the desktop edge
    without resizing the mascot.
    """

    PAD = 14
    RADIUS = 12
    decision = pyqtSignal(str, str)

    def __init__(self, theme: "Theme", owner: QWidget) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self._owner = owner
        self.theme = theme
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._label = QLabel(self)
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self.PAD, self.PAD - 2, self.PAD, self.PAD - 2)
        layout.addWidget(self._label)
        self._decision_token: str | None = None
        self._actions = QWidget(self)
        self._actions_layout = QHBoxLayout(self._actions)
        self._actions_layout.setContentsMargins(0, 4, 0, 0)
        self._actions_layout.setSpacing(6)
        self._option_buttons: list[QPushButton] = []
        self._actions.hide()
        layout.addWidget(self._actions)

        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(self.hide)

    def show_text(self, text: str, *, tone: str = "dim") -> None:
        text = " ".join(text.split())
        if not text:
            return
        color = {
            "dim": self.theme.sheet,
            "ok": self.theme.ok,
            "err": self.theme.danger,
        }.get(tone, self.theme.sheet)
        self._label.setStyleSheet(
            f"color: {color.name()}; background: transparent; border: none;"
        )
        self._label.setText(text)
        self._clear_options()
        self._actions.hide()
        self._decision_token = None
        self.adjustSize()
        width = min(BUBBLE_MAX_PX, max(160, self._label.width()))
        self._label.setFixedWidth(width)
        self.resize(self.sizeHint())
        self._reposition()
        self.show()
        self.raise_()
        self._hide.start(BUBBLE_MS)

    def _clear_options(self) -> None:
        while self._actions_layout.count():
            item = self._actions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._option_buttons.clear()

    def show_prompt(
        self,
        token: str,
        prompt: str,
        options: list[str] | None = None,
        *,
        label: str = "Approval required",
        reason: str = "",
    ) -> None:
        self._decision_token = token
        self._label.setStyleSheet(
            f"color: {self.theme.sheet.name()}; background: transparent; border: none;"
        )
        header = f"{label} · {reason}" if reason else label
        self._label.setText(f"{header}\n{prompt}")
        self._clear_options()
        for option in options or ["Approve", "Reject"]:
            button = QPushButton(str(option), self._actions)
            button.clicked.connect(
                lambda _checked=False, choice=str(option): self._answer(choice)
            )
            self._actions_layout.addWidget(button)
            self._option_buttons.append(button)
        self._actions.show()
        self._hide.stop()
        self.adjustSize()
        self._label.setFixedWidth(min(BUBBLE_MAX_PX, max(200, self._label.width())))
        self.resize(self.sizeHint())
        self._reposition()
        self.show()
        self.raise_()

    def _answer(self, option: str) -> None:
        token, self._decision_token = self._decision_token, None
        if token is None:
            return
        self._actions.hide()
        self._clear_options()
        self.hide()
        self.decision.emit(token, option)

    def _reposition(self) -> None:
        """Sit to the left of the mascot, flipping right when it would clip."""
        anchor = self._owner.frameGeometry()
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.adjustSize()

        x = anchor.right() - self.width()
        y = anchor.center().y() - self.height() // 2
        if available and x < available.left():
            x = anchor.left()
        if available:
            x = max(available.left(), min(x, available.right() - self.width()))
            y = max(available.top(), min(y, available.bottom() - self.height()))
        self.move(x, y)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 0))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        rect = self.rect().adjusted(1, 1, -1, -1)
        for inset, alpha in ((0, 26), (1, 20), (2, 13)):
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(
                rect.adjusted(inset, inset + 2, -inset, -inset),
                self.RADIUS,
                self.RADIUS,
            )
        fill = QColor(self.theme.bg)
        fill.setAlpha(228)
        painter.setBrush(fill)
        painter.setPen(QPen(QColor(self.theme.stroke), 1.0))
        painter.drawRoundedRect(rect, self.RADIUS, self.RADIUS)

    def follow(self) -> None:
        if self.isVisible():
            self._reposition()


class DaemonLink(QObject):
    """Asynchronous NDJSON client for the daemon control socket."""

    event_received = pyqtSignal(dict)
    reply = pyqtSignal(dict)
    linked = pyqtSignal(bool)

    RECONNECT_MS = 2000

    def __init__(self, path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = path
        self._socket: QLocalSocket | None = None
        self._buffer = b""
        self._requests: dict[int, dict[str, object]] = {}
        self._next_request_id = 0
        self._is_linked: bool | None = None
        self._stopped = False

        self._retry = QTimer(self)
        self._retry.setInterval(self.RECONNECT_MS)
        self._retry.timeout.connect(self.open)

    @staticmethod
    def socket_path() -> str:
        """Resolve the daemon socket exactly the way the daemon does."""
        try:
            from kakarot_daemon.control import resolve_socket_path

            return str(resolve_socket_path())
        except Exception:
            runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
            base = Path(runtime_dir) if runtime_dir else Path("/run/user/1000")
            return str(base / "kakarot" / "kakarot.sock")

    def start(self) -> None:
        self._stopped = False
        self._retry.start()
        self.open()

    def open(self) -> None:
        if self._stopped:
            return
        current = self._socket
        if current is not None and current.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            return
        self._teardown_subscription()
        sock = QLocalSocket(self)
        self._socket = sock
        sock.connected.connect(self._subscription_connected)
        sock.readyRead.connect(self._read)
        sock.disconnected.connect(self._subscription_disconnected)
        sock.errorOccurred.connect(self._subscription_error)
        sock.connectToServer(self._path)

    def _set_linked(self, linked: bool) -> None:
        linked = bool(linked)
        if linked is not self._is_linked:
            self._is_linked = linked
            self.linked.emit(linked)

    def _subscription_connected(self) -> None:
        sock = self._socket
        if sock is None:
            return
        sock.write(b'{"command":"subscribe"}\n')
        sock.flush()
        self._set_linked(True)

    def _subscription_disconnected(self) -> None:
        self._teardown_subscription()
        self._set_linked(False)

    def _subscription_error(self, _error) -> None:
        sock = self._socket
        if sock is None:
            return
        if sock.state() == QLocalSocket.LocalSocketState.UnconnectedState:
            self._teardown_subscription()
            self._set_linked(False)

    def _teardown_subscription(self) -> None:
        sock, self._socket = self._socket, None
        self._buffer = b""
        if sock is not None:
            sock.abort()
            sock.deleteLater()

    def stop(self) -> None:
        """Stop reconnects and close all sockets without blocking the GUI."""
        self._stopped = True
        self._retry.stop()
        self._teardown_subscription()
        self._set_linked(False)
        for request_id in tuple(self._requests):
            self._finish_request(request_id, None, emit_reply=False)

    def _read(self) -> None:
        sock = self._socket
        if sock is None:
            return
        chunk = bytes(sock.read(65536))
        if not chunk:
            # Peer closed: drop the subscription and let the timer reconnect.
            self._teardown_subscription()
            self._set_linked(False)
            return
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(payload, dict):
                self.event_received.emit(payload)

    def send(self, command: str, arguments: dict | None = None) -> None:
        """Send a legacy command asynchronously; its answer arrives on reply."""
        payload = {"command": command}
        if arguments:
            payload.update(arguments)
        self._send_message(payload, timeout_ms=60000 if command == "manual_input" else 30000)

    def send_event(self, msg_type: str, payload: dict) -> None:
        """Send one typed core event asynchronously."""
        self._send_message(
            {"sender": "mascot", "type": msg_type, "payload": payload},
            timeout_ms=60000 if msg_type == "manual_input" else 30000,
        )

    def _send_message(self, message: dict, *, timeout_ms: int) -> None:
        self._next_request_id += 1
        request_id = self._next_request_id
        sock = QLocalSocket(self)
        timeout = QTimer(self)
        timeout.setSingleShot(True)
        self._requests[request_id] = {
            "socket": sock,
            "buffer": bytearray(),
            "message": message,
            "timeout": timeout,
        }
        timeout.timeout.connect(
            lambda request_id=request_id: self._finish_request(
                request_id, {"ok": False, "error": "daemon did not answer"}
            )
        )
        timeout.start(timeout_ms)
        sock.connected.connect(lambda request_id=request_id: self._request_connected(request_id))
        sock.readyRead.connect(lambda request_id=request_id: self._request_ready(request_id))
        sock.errorOccurred.connect(lambda _error, request_id=request_id: self._request_error(request_id))
        sock.disconnected.connect(lambda request_id=request_id: self._request_disconnected(request_id))
        sock.connectToServer(self._path)

    def _request_state(self, request_id: int) -> dict[str, object] | None:
        return self._requests.get(request_id)

    def _request_connected(self, request_id: int) -> None:
        state = self._request_state(request_id)
        if state is None:
            return
        sock = state["socket"]
        message = state["message"]
        assert isinstance(sock, QLocalSocket) and isinstance(message, dict)
        sock.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        sock.flush()

    def _request_ready(self, request_id: int) -> None:
        state = self._request_state(request_id)
        if state is None:
            return
        sock = state["socket"]
        buffer = state["buffer"]
        assert isinstance(sock, QLocalSocket) and isinstance(buffer, bytearray)
        buffer.extend(bytes(sock.readAll()))
        if len(buffer) > 65536:
            self._finish_request(request_id, {"ok": False, "error": "daemon response too large"})
            return
        if b"\n" not in buffer:
            return
        line, _rest = bytes(buffer).split(b"\n", 1)
        try:
            answer = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            answer = {"ok": False, "error": "invalid daemon response"}
        self._finish_request(
            request_id, answer if isinstance(answer, dict) else {"ok": False, "error": "invalid daemon response"}
        )

    def _request_error(self, request_id: int) -> None:
        self._finish_request(request_id, {"ok": False, "error": "daemon unreachable"})

    def _request_disconnected(self, request_id: int) -> None:
        if request_id in self._requests:
            self._finish_request(request_id, {"ok": False, "error": "daemon disconnected"})

    def _finish_request(
        self, request_id: int, answer: dict | None, *, emit_reply: bool = True
    ) -> None:
        state = self._requests.pop(request_id, None)
        if state is None:
            return
        timeout = state["timeout"]
        sock = state["socket"]
        assert isinstance(timeout, QTimer) and isinstance(sock, QLocalSocket)
        timeout.stop()
        timeout.deleteLater()
        sock.abort()
        sock.deleteLater()
        if emit_reply and answer is not None:
            if answer.get("type") == "response":
                answer = {key: value for key, value in answer.items() if key != "type"}
            self.reply.emit(answer)


class FloatingAssistant(QWidget):
    STATE_SETTINGS = 0
    STATE_ACTIVE = 1

    def __init__(self) -> None:
        super().__init__(None)
        self.theme = Theme(system_prefers_dark())
        self._config = load_config()
        self._api_key = str(self._config.get("gemini_api_key", "") or "")

        self._dragging = False
        self._grab_offset: QPoint | None = None

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(self._window_icon())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.setStyleSheet(self.theme.qss())

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(6)

        self.stack = QStackedWidget(self)
        self.stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        root.addWidget(self.stack)

        self._build_settings_state()
        self._build_active_state()

        self.pin = QPushButton("Pinned", self)
        self.pin.setObjectName("pin")
        self.pin.setCheckable(True)
        self.pin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pin.setFixedHeight(22)
        self.pin.toggled.connect(self.set_pinned)
        root.addWidget(self.pin, 0, Qt.AlignmentFlag.AlignHCenter)

        self._pulse = QTimer(self)
        self._pulse.setInterval(55)
        self._pulse.timeout.connect(self._on_pulse)

        self._apply_pinned(bool(self._config.get("pinned", True)))
        self._restore_position()
        self.stack.setCurrentIndex(
            self.STATE_ACTIVE if self._api_key else self.STATE_SETTINGS
        )
        self.resize(self.sizeHint())

        # Floating caption and the daemon link. Both are optional at runtime:
        # the widget stays usable when the daemon is down, it just cannot
        # execute or report state.
        self.bubble = SpeechBubble(self.theme, self)
        self.daemon = DaemonLink(DaemonLink.socket_path(), self)
        self.daemon.event_received.connect(self._on_daemon_event)
        self.daemon.reply.connect(self._on_daemon_reply)
        self.daemon.linked.connect(lambda ok: None if ok else self._say("Daemon offline", "err"))
        self.bubble.decision.connect(self._send_hitl_decision)
        self.daemon.start()

        self._exec_poll = QTimer(self)
        self._exec_poll.setInterval(EXEC_POLL_MS)
        self._exec_poll.timeout.connect(self._poll_exec_job)
        self._exec_job: str | None = None
        self._exec_ticks = 0

    # ------------------------------------------------------------- window -- #

    def _window_icon(self) -> QIcon:
        src = hero_pixmap()
        return QIcon(src) if not src.isNull() else QIcon()

    def showEvent(self, event: QEvent) -> None:
        super().showEvent(event)
        self.keep_on_screen()

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 0))

    def set_pinned(self, pinned: bool) -> None:
        self._apply_pinned(pinned)

    def _apply_pinned(self, pinned: bool) -> None:
        """Single source of truth: the pin button's state and the window flags
        are always set together, so they can never drift apart."""
        pinned = bool(pinned)
        self.pin.blockSignals(True)
        self.pin.setChecked(pinned)
        self.pin.blockSignals(False)
        self.pin.setText("Pinned" if pinned else "Unpinned")

        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if self.windowFlags() != flags:
            # Changing flags on a visible window hides it; show() again.
            self.setWindowFlags(flags)
            self.show()
        self.keep_on_screen()

    def _screen_at(self, point: QPoint):
        return QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()

    def keep_on_screen(self) -> None:
        screen = self._screen_at(self.frameGeometry().center())
        if screen is None:
            return
        area = screen.availableGeometry()
        box = self.frameGeometry()
        self.move(
            min(max(box.left(), area.left()), max(area.left(), area.right() - box.width() + 1)),
            min(max(box.top(), area.top()), max(area.top(), area.bottom() - box.height() + 1)),
        )

    def _restore_position(self) -> None:
        if "pos_x" in self._config and "pos_y" in self._config:
            self.move(int(self._config["pos_x"]), int(self._config["pos_y"]))

    def persist(self) -> None:
        self._config["pos_x"] = self.x()
        self._config["pos_y"] = self.y()
        self._config["pinned"] = self.pin.isChecked()
        self._config["have_key"] = bool(self._api_key)
        save_config(self._config)

    def closeEvent(self, event) -> None:
        self._pulse.stop()
        self.persist()
        self.daemon.stop()
        self.bubble.close()
        super().closeEvent(event)

    # ------------------------------------------------------------- drag --- #

    @property
    def dragging(self) -> bool:
        return self._dragging

    def begin_drag(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # No grabMouse(): the pressed child keeps the implicit grab, so the
        # move/release stream keeps arriving at that same child.
        self._dragging = True
        self._grab_offset = (
            event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        )

    def update_drag(self, event: QMouseEvent) -> None:
        """Reposition from the cursor's global coordinates. This is the value
        the WM reports in the event, so it stays exact across X11, XWayland
        and native Wayland, and across fractional-DPI changes."""
        if not self._dragging or self._grab_offset is None:
            return
        target = event.globalPosition().toPoint() - self._grab_offset
        screen = self._screen_at(target)
        if screen is not None:
            area = screen.availableGeometry()
            target.setX(
                min(max(target.x(), area.left() - 48), max(area.left(), area.right() - 48))
            )
            target.setY(
                min(max(target.y(), area.top() - 24), max(area.top(), area.bottom() - 24))
            )
        self.move(target)

    def end_drag(self) -> None:
        self._dragging = False
        self._grab_offset = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.begin_drag(event)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self.update_drag(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self.end_drag()
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------- state 1 UI --- #

    def _build_settings_state(self) -> None:
        card = Card(self.stack)
        col = QVBoxLayout(card)
        col.setContentsMargins(20, 22, 20, 20)
        col.setSpacing(11)

        title = QLabel("Gemini API key", card)
        title.setObjectName("title")
        col.addWidget(title)

        sub = QLabel(
            "Create one in Google AI Studio, then paste it here. "
            "It is stored only on this machine.",
            card,
        )
        sub.setObjectName("subtitle")
        sub.setWordWrap(True)
        col.addWidget(sub)

        self.key_input = QLineEdit(card)
        self.key_input.setObjectName("apiKey")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("AIza…")
        self.key_input.setClearButtonEnabled(True)
        self.key_input.setText(self._api_key)
        self.key_input.returnPressed.connect(self.save_key)
        col.addWidget(self.key_input)

        self.remember = QCheckBox("Remember on this device", card)
        self.remember.setObjectName("remember")
        self.remember.setChecked(bool(self._config.get("remember", True)))
        col.addWidget(self.remember)

        self.save_button = QPushButton("Save and start", card)
        self.save_button.setObjectName("save")
        self.save_button.setDefault(True)
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.save_button.clicked.connect(self.save_key)
        col.addWidget(self.save_button)

        self.status = QLabel("", card)
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        col.addWidget(self.status)

        self.stack.addWidget(card)

    # -------------------------------------------------------- state 2 UI --- #

    def _build_active_state(self) -> None:
        panel = Panel(self.stack)
        col = QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(BTN_GAP)
        col.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.hero = HeroView(hero_pixmap(), panel)
        col.addWidget(self.hero, 0, Qt.AlignmentFlag.AlignHCenter)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(BTN_GAP + 2)
        row.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.mic_button = GlowButton(
            mic_pixmap, self.theme, True, "Toggle microphone", panel
        )
        self.speaker_button = GlowButton(
            speaker_pixmap, self.theme, True, "Talk-back", panel
        )
        self.power_button = GlowButton(power_pixmap, self.theme, False, "Quit", panel)

        self.mic_button.toggled.connect(self._on_mic)
        self.speaker_button.toggled.connect(self._on_speaker)
        self.power_button.clicked.connect(self.quit_app)
        for b in (self.mic_button, self.speaker_button, self.power_button):
            row.addWidget(b)
        col.addLayout(row)

        self.hint = QLabel("Ready when you are", panel)
        self.hint.setObjectName("hint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        col.addWidget(self.hint)

        self.stack.addWidget(panel)

    # ------------------------------------------------------------ actions -- #

    def _say(self, text: str, tone: str = "dim") -> None:
        color = {
            "dim": self.theme.dim,
            "ok": self.theme.ok,
            "err": self.theme.danger,
        }[tone]
        self.status.setStyleSheet(f"color: {color.name()};")
        self.status.setText(text)
        # Mirror every status line into the floating caption, which is what the
        # user actually sees when the mascot is not being watched closely.
        self.bubble.show_text(text, tone=tone)

    # ------------------------------------------------------ daemon events -- #

    def _on_daemon_event(self, payload: dict) -> None:
        """Map a pushed daemon state event onto the mascot's visible state."""
        msg_type = payload.get("type")
        if msg_type == "heartbeat":
            return
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            event_payload = payload
        if msg_type == "ui_say":
            text = str(event_payload.get("text") or "")
            status = str(event_payload.get("status") or "idle")
            self._say(text, "err" if status == "error" else "ok" if status == "success" else "dim")
            return
        if msg_type in {"human_required", "auth_request", "hitl_prompt"}:
            request_id = str(
                event_payload.get("request_id") or event_payload.get("token") or ""
            )
            prompt = str(
                event_payload.get("prompt") or event_payload.get("command") or ""
            )
            options = event_payload.get("options")
            if not isinstance(options, list) or not options:
                options = ["Approve", "Reject"]
            label = str(event_payload.get("label") or "Approval required")
            reason = str(event_payload.get("reason") or "")
            if request_id and prompt:
                self.bubble.show_prompt(
                    request_id,
                    prompt,
                    [str(option) for option in options],
                    label=label,
                    reason=reason,
                )
            return
        snapshot = payload.get("state")
        if not isinstance(snapshot, dict):
            return
        phase = str(snapshot.get("phase") or "")
        muted = bool(snapshot.get("muted"))
        error = str(snapshot.get("error") or "")

        if phase == "LISTENING":
            self.mic_button.setChecked(True)
            self.bubble.show_text("Listening…")
        elif phase == "PROCESSING_TOOL":
            self.mic_button.setChecked(False)
            self.bubble.show_text("Thinking…")
        elif phase == "SPEAKING":
            self.bubble.show_text("Speaking…")
        elif phase == "IDLE_MUTED":
            self.mic_button.setChecked(False)
        if error:
            self._say(error, "err")

    def _on_daemon_reply(self, payload: dict) -> None:
        # A finished sandbox job also carries "job", so "done" must be checked
        # first or the poll would restart itself forever.
        if payload.get("done"):
            self._finish_exec(payload)
            return
        if not payload.get("ok", True):
            self._say(str(payload.get("error") or "command failed"), "err")
            return
        job = payload.get("job")
        if isinstance(job, str):
            self._exec_job = job
            self._exec_ticks = 0
            self._exec_poll.start()
            self.bubble.show_text("Running…")

    def _poll_exec_job(self) -> None:
        if self._exec_job is None:
            self._exec_poll.stop()
            return
        self._exec_ticks += 1
        if self._exec_ticks > EXEC_POLL_LIMIT:
            self._exec_poll.stop()
            self._exec_job = None
            self._say("Command timed out", "err")
            return
        self.daemon.send("result", {"job": self._exec_job})

    def _finish_exec(self, payload: dict) -> None:
        self._exec_poll.stop()
        self._exec_job = None
        if payload.get("requires_confirmation"):
            # The daemon refused to run it: the AST gate sent a notification and
            # is waiting on the human, so report that instead of an error.
            self._say("Needs your confirmation", "err")
            return
        body = str(payload.get("stdout") or "").strip() or str(
            payload.get("stderr") or payload.get("reason") or ""
        )
        if not body:
            body = "(no output)"
        lines = [line for line in body.splitlines() if line.strip()]
        self._say(lines[0][:120] if lines else "(no output)",
                  "ok" if payload.get("ok") else "err")

    # ------------------------------------------------------ menu actions -- #

    def force_listen(self) -> None:
        """Open the microphone gate through the daemon, never the hardware."""
        self.mic_button.setChecked(True)
        self.bubble.show_text("Listening…")
        self.daemon.send_event("force_listen", {})

    def prompt_command(self) -> None:
        """Non-blocking command entry; the daemon gates and sandboxes it."""
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Run a command")
        dialog.setLabelText("Command (read-only runs immediately; anything else needs approval)")
        dialog.setInputMode(QInputDialog.InputMode.TextInput)
        dialog.setTextValue("")
        dialog.setFixedWidth(460)
        self._exec_dialog = dialog
        dialog.finished.connect(lambda _result, dialog=dialog: self._clear_exec_dialog(dialog))
        dialog.textValueSelected.connect(self._run_command)
        dialog.open()

    def _clear_exec_dialog(self, dialog: QInputDialog) -> None:
        if self._exec_dialog is dialog:
            self._exec_dialog = None

    def _run_command(self, shell: str) -> None:
        shell = shell.strip()
        if not shell:
            return
        self._say("Submitting…")
        self.daemon.send("execute", {"shell": shell})

    def _send_hitl_decision(self, request_id: str, option: str) -> None:
        self.daemon.send_event(
            "human_response",
            {
                "request_id": request_id,
                "option": option,
                "approved": option == "Approve",
            },
        )

    def prompt_api_key(self) -> None:
        """Reuse the setup screen's key field rather than a bare dialog."""
        self.stack.setCurrentIndex(self.STATE_SETTINGS)
        self.key_input.setFocus()
        self.bubble.show_text("Enter your Gemini API key")

    def toggle_mascot(self) -> None:
        if self.isVisible():
            self.bubble.hide()
            self.hide()
        else:
            self.show()
            self.raise_()
            self.bubble.follow()

    def build_menu(self) -> QMenu:
        """Dark, keyboard-navigable context menu shared by the mascot."""
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background: {self.theme.bg.name(QColor.NameFormat.HexArgb)};
                     color: {self.theme.sheet.name()};
                     border: 1px solid {self.theme.stroke.name()};
                     border-radius: 12px; padding: 6px; }}
            QMenu::item {{ padding: 8px 18px; border-radius: 8px; }}
            QMenu::item:selected {{ background: {self.theme.accent.name()};
                                   color: {self.theme.on_accent.name()}; }}
            QMenu::separator {{ height: 1px; background: {self.theme.stroke.name()};
                               margin: 6px 10px; }}
        """)
        menu.addAction(QAction("🎙️  Force Listen", self, triggered=self.force_listen))
        menu.addAction(QAction("⚡  Execute Command", self, triggered=self.prompt_command))
        menu.addAction(QAction("⚙️   Configure API Key", self, triggered=self.prompt_api_key))
        menu.addSeparator()
        menu.addAction(QAction("Quit Kakarot", self, triggered=self.quit_app))
        return menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.build_menu().exec(event.globalPos())

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().moveEvent(event)
        # Position is restored during __init__, before the caption exists.
        bubble = getattr(self, "bubble", None)
        if bubble is not None:
            bubble.follow()

    def save_key(self) -> None:
        key = self.key_input.text().strip()
        if not key:
            self._say("A key is required before the assistant can start.", "err")
            return
        if len(key) < 20 or any(ch.isspace() for ch in key):
            self._say("That does not look like a valid Gemini API key.", "err")
            return

        self._api_key = key
        self._config["remember"] = self.remember.isChecked()
        # Store the shared credential in the keyring, never in settings.json.
        # The daemon reads the same entry, so this screen configures both
        # processes instead of saving a key only this alternate front end sees.
        try:
            from kakarot_daemon.credentials import set_gemini_api_key

            set_gemini_api_key(key)
            self._config.pop("gemini_api_key", None)
        except Exception as exc:
            self._say(f"Could not store the key: {exc}", "err")
            return
        self._config["pos_x"] = self.x()
        self._config["pos_y"] = self.y()

        if not save_config(self._config):
            self._say(f"Could not write {CONFIG_PATH}.", "err")
            QMessageBox.warning(self, "Save failed", f"Cannot write {CONFIG_PATH}.")
            return

        self._say("Saved.", "ok")
        self.go_active()

    def go_active(self) -> None:
        self.stack.setCurrentIndex(self.STATE_ACTIVE)
        self.hint.setText("Drag me anywhere")
        self.resize(self.sizeHint())
        QTimer.singleShot(700, lambda: self.mic_button.setChecked(True))

    def _on_mic(self, checked: bool) -> None:
        if checked:
            self.hint.setText("Listening…")
        elif not self.speaker_button.isChecked():
            self.hint.setText("Microphone off")

    def _on_speaker(self, checked: bool) -> None:
        if checked:
            self._pulse.start()
            self.hint.setText("Speaking…")
        else:
            self._pulse.stop()
            self.hero.set_glow(0.0)
            self.hint.setText("Listening…" if self.mic_button.isChecked() else "Ready")

    def quit_app(self) -> None:
        self._pulse.stop()
        self.persist()
        self.close()
        QApplication.instance().quit()

    # --------------------------------------------------------- animation --- #

    def _on_pulse(self) -> None:
        t = (self._pulse.elapsed() % 1500) / 1500.0
        self.hero.set_glow(0.5 + 0.5 * abs(1.0 - 2.0 * t))


class ApplicationActivation(QObject):
    """Implement the freedesktop activation API used by DBusActivatable."""

    def __init__(self, window: FloatingAssistant) -> None:
        super().__init__()
        self._window = window

    def _show_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    @pyqtSlot("QVariantMap")
    def Activate(self, _platform_data: dict) -> None:
        self._show_window()

    @pyqtSlot("QStringList", "QVariantMap")
    def Open(self, _uris: list[str], _platform_data: dict) -> None:
        self._show_window()

    @pyqtSlot(str, "QVariantList", "QVariantMap")
    def ActivateAction(
        self, action_name: str, _parameter: list, _platform_data: dict
    ) -> None:
        if action_name == "quit":
            self._window.close()
        else:
            self._show_window()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    # High-DPI scaling is always on in Qt6 and these attributes are gone; the
    # remaining knobs are set defensively so a future attribute drop can't
    # abort startup with an AttributeError.
    for attr_name in ("AA_EnableHighDpiScaling", "AA_UseHighDpiPixmaps"):
        attr = getattr(Qt.ApplicationAttribute, attr_name, None)
        if attr is not None:
            QApplication.setAttribute(attr, True)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(True)

    window = FloatingAssistant()
    window.show()

    bus = QDBusConnection.sessionBus()
    activation = ApplicationActivation(window)
    if not bus.isConnected():
        sys.stderr.write(f"[{APP_ID}] session D-Bus unavailable; desktop bus activation disabled\n")
    elif not bus.registerObject(
        "/org/freedesktop/Application",
        "org.freedesktop.Application",
        activation,
        QDBusConnection.RegisterOption.ExportAllSlots,
    ):
        sys.stderr.write(f"[{APP_ID}] could not export the desktop activation interface\n")
    elif not bus.registerService(APP_ID):
        sys.stderr.write(
            f"[{APP_ID}] could not own its D-Bus name: {bus.lastError().message()}\n"
        )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
