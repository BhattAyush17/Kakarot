"""Nonmodal key drawer. Keyring and HTTP work never block the GUI thread."""
import threading
from kakarot_overlay.qt import QtCore as C, QtWidgets as W, Signal
from kakarot_overlay.config import load_api_key, save_api_key, test_api_key


class SettingsDialog(W.QDialog):
    key_saved = Signal()
    completed = Signal(str, bool, str)

    def __init__(self, parent=None, *, embedded=False):
        self.embedded = embedded
        flags = (
            C.Qt.WindowType.Widget
            if embedded
            else C.Qt.WindowType.Tool | C.Qt.WindowType.FramelessWindowHint
        )
        super().__init__(parent, flags)
        self.setWindowTitle("Kakarot settings")
        self.setFixedWidth(310 if embedded else 340)
        self.setStyleSheet("QDialog{background:#111113;border:1px solid #3f3f46;border-radius:10px;} QLabel{color:#f4f4f5;} QLineEdit{background:#1c1c1f;color:white;padding:9px;border:1px solid #3f3f46;border-radius:6px;} QPushButton{background:#252528;color:white;padding:8px;border:1px solid #3f3f46;border-radius:6px;} QPushButton:hover{background:#303034;} QPushButton:disabled{color:#777;}")
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(W.QLabel("KAKAROT · VOICE SETTINGS"))
        row = W.QHBoxLayout()
        self.key = W.QLineEdit()
        self.key.setPlaceholderText("Gemini API key")
        self.key.setEchoMode(W.QLineEdit.EchoMode.Password)
        eye = W.QPushButton("Show")
        eye.setCheckable(True)
        eye.toggled.connect(lambda visible: self.key.setEchoMode(W.QLineEdit.EchoMode.Normal if visible else W.QLineEdit.EchoMode.Password))
        row.addWidget(self.key)
        row.addWidget(eye)
        layout.addLayout(row)
        self.status = W.QLabel("Credentials are stored in your desktop keyring when available.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = W.QHBoxLayout()
        self.test = W.QPushButton("Test key")
        self.save = W.QPushButton("Save")
        close = W.QPushButton("Close")
        close.clicked.connect(self.accept if not embedded else self.hide_panel)
        self.test.clicked.connect(lambda: self.work("test"))
        self.save.clicked.connect(lambda: self.work("save"))
        for button in (self.test, self.save, close):
            row.addWidget(button)
        layout.addLayout(row)
        self.completed.connect(self.result)
        self.busy = False
        self.work("load")

    def work(self, operation):
        if self.busy:
            return
        self.busy = True
        self.test.setEnabled(False)
        self.save.setEnabled(False)
        key = self.key.text().strip()
        self.status.setText("Working…")
        def run():
            try:
                if operation == "load":
                    value = load_api_key()
                elif operation == "test":
                    value = test_api_key(key)
                else:
                    if not key:
                        raise ValueError("Enter a Gemini API key first.")
                    # Never persist a credential that Google has already
                    # rejected: the old flow saved it and marked the daemon
                    # "connected", leaving the user with a mute microphone.
                    test_api_key(key)
                    backend = save_api_key(key)
                    value = f"Saved to {backend}. Start listening to use it."
                self.completed.emit(operation, True, value)
            except Exception as exc:
                message = str(exc).strip() or type(exc).__name__
                if key:
                    message = message.replace(key, "[hidden]")
                self.completed.emit(operation, False, message[:400])
        threading.Thread(target=run, daemon=True, name="kakarot-key-settings").start()

    def result(self, operation, success, value):
        self.busy = False
        self.test.setEnabled(True)
        self.save.setEnabled(True)
        if operation == "load" and success:
            self.key.setText(value)
            self.status.setText("Credentials are stored in your desktop keyring when available.")
            if not value:
                if self.embedded:
                    parent = self.parentWidget()
                    if parent is not None and hasattr(parent, "toggle_settings"):
                        if not self.isVisible():
                            parent.toggle_settings()
                else:
                    self.open_anchored()
        else:
            self.status.setText(value)
        if operation == "save" and success:
            self.key_saved.emit()

    def open_anchored(self):
        if self.embedded:
            self.show()
            parent = self.parentWidget()
            if parent is not None and parent.layout() is not None:
                parent.layout().activate()
                parent.adjustSize()
            return
        self.adjustSize()
        parent = self.parentWidget()
        screen = parent.screen().availableGeometry()
        x = max(screen.left(), min(parent.x()-self.width()-10, screen.right()-self.width()+1))
        y = max(screen.top(), min(parent.y(), screen.bottom()-self.height()+1))
        target = C.QRect(x, y, self.width(), self.height())
        self.setGeometry(target)
        self.show()
        self.raise_()
        self.activateWindow()
        self.animation = C.QPropertyAnimation(self, b"geometry", self)
        self.animation.setDuration(180)
        self.animation.setStartValue(target.translated(12, 0))
        self.animation.setEndValue(target)
        self.animation.start()

    def hide_panel(self):
        self.hide()
        if self.embedded:
            parent = self.parentWidget()
            if parent is not None and parent.layout() is not None:
                parent.layout().activate()
                parent.adjustSize()
