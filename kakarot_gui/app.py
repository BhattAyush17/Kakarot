"""Kakarot's GTK4 / libadwaita application entry point.

Replaces the legacy system-tray applet.  GNOME has no native tray, so Kakarot
runs as an ordinary ``DBusActivatable`` session app: it can sit in the
background with no window, and the launcher or a GNOME shortcut can raise the
window on demand.

Run directly for development::

    /usr/bin/python3 -m kakarot_gui
"""

from __future__ import annotations

import os
import sys

if __package__ in (None, ""):  # allow `python3 kakarot_gui/app.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from kakarot_gui import APP_ID  # noqa: E402
from kakarot_gui.dbus_client import DaemonClient  # noqa: E402
from kakarot_gui.theme import ColorSchemeSync  # noqa: E402
from kakarot_gui.window import KakarotWindow  # noqa: E402

DEVELOPERS = ["Kakarot contributors"]
COPYRIGHT = "© Kakarot contributors"


class KakarotApplication(Adw.Application):
    """Background-capable GNOME app for the Kakarot daemon."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._client = DaemonClient()
        self._window: KakarotWindow | None = None
        self._color_scheme = ColorSchemeSync()

        self._add_action("show", self._on_show)
        self._add_action("preferences", self._on_preferences)
        self._add_action("shortcuts", self._on_shortcuts)
        self._add_action("about", self._on_about)
        self._add_action("quit", self._on_quit)
        self.set_accels_for_action("app.show", ["<Primary>period"])
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self._add_accelerators()

    def _add_accelerators(self) -> None:
        for accelerator, action in (
            ("<Primary>m", "app.mute-toggle"),
            ("<Primary>r", "app.reload-log"),
        ):
            self.set_accels_for_action(action, [accelerator])

    def _add_action(self, name: str, handler) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", handler)
        self.add_action(action)

    # -- lifecycle --------------------------------------------------------

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        Adw.init()
        self._color_scheme.start()
        self._client.start()
        self._add_action("mute-toggle", self._on_mute_toggle)
        self._add_action("reload-log", self._on_reload_log)

    def do_activate(self) -> None:
        window = self._ensure_window()
        window.present()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        # ``--background`` starts the app with no window at all, which is how
        # the systemd unit keeps a session-resident presence.
        background = "--background" in command_line.get_arguments()
        if not background:
            self.do_activate()
        return 0

    def do_shutdown(self) -> None:
        self._client.stop()
        Gtk.Application.do_shutdown(self)

    # -- actions ----------------------------------------------------------

    def _ensure_window(self) -> KakarotWindow:
        if self._window is None:
            self._window = KakarotWindow(self, self._client)
        return self._window

    def _on_show(self, *_args) -> None:
        self.do_activate()

    def _on_preferences(self, *_args) -> None:
        dialog = Adw.PreferencesDialog()
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        group = Adw.PreferencesGroup(title="Service")
        group.add(Adw.ActionRow(
            title="Daemon",
            subtitle="io.github.kakarot.Daemon on the session bus",
            activatable=True,
        ))
        page.add(group)
        dialog.add(page)
        dialog.present(self._ensure_window())

    def _on_shortcuts(self, *_args) -> None:
        window = self._ensure_window()
        dialog = Adw.PreferencesDialog()
        page = Adw.PreferencesPage(title="Shortcuts")
        group = Adw.PreferencesGroup(title="Window")
        for title, accelerator in (
            ("Show window", "<Primary>period"),
            ("Toggle microphone", "<Primary>m"),
            ("Reload audit log", "<Primary>r"),
            ("Quit", "<Primary>q"),
        ):
            row = Adw.ActionRow(title=title)
            row.add_suffix(Gtk.ShortcutLabel(accelerator=accelerator, valign=Gtk.Align.CENTER))
            group.add(row)
        page.add(group)
        dialog.add(page)
        dialog.present(window)

    def _on_about(self, *_args) -> None:
        about = Adw.AboutWindow(
            transient_for=self._ensure_window(),
            application_name="Kakarot",
            application_icon="io.github.kakarot.Daemon",
            version="1.0",
            developer_name=DEVELOPERS[0],
            developers=DEVELOPERS,
            copyright=COPYRIGHT,
            license_type=Gtk.License.APACHE_2_0,
            comments="Autonomous, memory-bounded, unprivileged desktop agent for GNOME.",
        )
        about.present()

    def _on_quit(self, *_args) -> None:
        self.quit()

    def _on_mute_toggle(self, *_args) -> None:
        state = self._client.state
        listening = bool(state.get("listening", state.get("muted") is False))
        self._client.command("mute" if listening else "unmute")

    def _on_reload_log(self, *_args) -> None:
        if self._window is not None:
            self._window._load_audit()


def main(argv: list[str] | None = None) -> int:
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Kakarot")
    return KakarotApplication().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
