"""The Kakarot window: live status, the audit trail, and settings.

Built with GTK4 and libadwaita so it inherits GNOME's adaptive styling, rounded
corners, and client-side decorations.  No custom theme: the window must look
native on light, dark, and accent-coloured desktops alike.
"""

from __future__ import annotations

import json
import os

from gi.repository import Adw, Gio, GLib, Gtk

AUDIT_LOG_NAME = "audit_log.md"
TAIL_BYTES = 200_000
MAX_LINES = 5000

PHASE_TITLES = {
    "IDLE_MUTED": "Microphone off",
    "LISTENING": "Listening",
    "PROCESSING_TOOL": "Running a tool",
    "SPEAKING": "Speaking",
    "DISCONNECTED": "Reconnecting",
    "ERROR": "Needs attention",
    "UNKNOWN": "Unknown",
}


def data_dir() -> str:
    """Kakarot's XDG data directory (``~/.local/share/kakarot``)."""
    override = os.environ.get("KAKAROT_APP_DATA")
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "kakarot")


class KakarotWindow(Adw.ApplicationWindow):
    """Single window: a status row on top, the audit log below."""

    def __init__(self, application, client) -> None:
        super().__init__(application=application, title="Kakarot")
        self._client = client
        self.set_default_size(720, 640)
        self.set_size_request(360, 400)

        self._toasts = Adw.ToastOverlay()
        self.set_content(self._toasts)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle.new("Kakarot", "Autonomous Linux Desktop Agent"))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload audit log")
        refresh.connect("clicked", lambda *_: self._load_audit())
        header.pack_start(refresh)
        menu = Gio.Menu()
        menu.append("_Preferences", "app.preferences")
        menu.append("_Keyboard Shortcuts", "app.shortcuts")
        menu.append(f"_About {application.props.application_id}", "app.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_button)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._status = Adw.StatusPage(icon_name="microphone-sensitive-symbolic")
        # The status page is a banner, not the whole window: the audit trail
        # is the reason this window exists, so it gets the remaining space.
        self._status.set_vexpand(False)
        content.append(self._status)
        toolbar.set_content(content)
        self._toasts.set_child(toolbar)

        # Controls live in a bottom bar so the window stays usable when the
        # log is empty.
        controls = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6,
                          margin_start=6, margin_end=6)
        self._mute_button = Gtk.Button(label="Stop listening", sensitive=False)
        self._mute_button.add_css_class("destructive-action")
        self._mute_button.set_halign(Gtk.Align.START)
        self._mute_button.connect("clicked", self._on_mute_clicked)
        controls.append(self._mute_button)
        toolbar.add_bottom_bar(controls)

        self._log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._log_view.set_left_margin(12)
        self._log_view.set_right_margin(12)
        self._log_view.set_top_margin(6)
        self._log_view.set_bottom_margin(6)
        scroller = Gtk.ScrolledWindow(vexpand=True, child=self._log_view)
        self._log_buffer = self._log_view.get_buffer()
        content.append(scroller)

        client.connect_state(self._on_state)
        self._load_audit()

    # -- state ------------------------------------------------------------

    def _on_state(self, state: dict) -> None:
        if not state:
            self._status.set_title("Daemon unavailable")
            self._status.set_description("Start kakarot.service to begin.")
            self._status.set_icon_name("dialog-warning-symbolic")
            self._mute_button.set_sensitive(False)
            return
        phase = state.get("phase", "UNKNOWN")
        self._status.set_title(PHASE_TITLES.get(phase, phase))
        description = state.get("error") or self._describe(state)
        self._status.set_description(description)
        self._status.set_icon_name(self._icon_for(phase))

        listening = bool(state.get("listening", state.get("muted") is False))
        self._mute_button.set_sensitive(True)
        self._mute_button.set_label("Stop listening" if listening else "Start listening")

    def _describe(self, state: dict) -> str:
        pending = state.get("reminders") or []
        if pending:
            return f"{len(pending)} pending reminder(s)."
        return "Voice is on. Ask Kakarot to do something."

    def _icon_for(self, phase: str) -> str:
        return {
            "IDLE_MUTED": "microphone-off-symbolic",
            "LISTENING": "microphone-sensitive-symbolic",
            "PROCESSING_TOOL": "content-loading-symbolic",
            "SPEAKING": "audio-volume-high-symbolic",
            "DISCONNECTED": "network-offline-symbolic",
            "ERROR": "dialog-warning-symbolic",
        }.get(phase, "dialog-information-symbolic")

    def _on_mute_clicked(self, button: Gtk.Button) -> None:
        listening = button.get_label() == "Stop listening"
        reply = self._client.command("mute" if listening else "unmute")
        # Parse rather than string-match: the daemon answers with JSON, and
        # separators are not a contract.
        try:
            ok = json.loads(reply).get("ok") is True
        except (TypeError, ValueError):
            ok = False
        if not ok:
            self.toast("Kakarot rejected that command")
            return
        self.toast("Listening stopped" if listening else "Listening started")

    # -- audit log --------------------------------------------------------

    def _load_audit(self) -> None:
        path = os.path.join(data_dir(), AUDIT_LOG_NAME)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as handle:
                if size > TAIL_BYTES:
                    handle.seek(size - TAIL_BYTES)
                text = handle.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            self._log_buffer.set_text("No audit log yet.")
            return
        lines = text.splitlines()
        if len(lines) > MAX_LINES:
            text = "... (earlier entries trimmed)\n" + "\n".join(lines[-MAX_LINES:])
        self._log_buffer.set_text(text)
        # Follow the tail, which is what an operator actually reads.
        end = self._log_buffer.get_end_iter()
        self._log_buffer.place_cursor(end)
        self._log_view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)

    def toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        self._toasts.add_toast(toast)

    def present(self) -> None:
        super().present()
        self._load_audit()
