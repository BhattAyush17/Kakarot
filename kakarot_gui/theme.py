"""Follow the desktop's FreeDesktop colour-scheme preference.

GNOME publishes the user's light/dark preference on the session bus as the
``color-scheme`` property of ``org.freedesktop.appearance``.  libadwaita already
picks up the portal setting on most sessions, but Kakarot states the contract
explicitly so the window follows the preference even where the portal is absent
or a third-party app has forced a scheme.
"""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib

APPEARANCE_NAME = "org.freedesktop.appearance"
APPEARANCE_PATH = "/org/freedesktop/appearance"
APPEARANCE_INTERFACE = "org.freedesktop.appearance"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

PREFER_DARK = 2
PREFER_LIGHT = 1


class ColorSchemeSync:
    """Apply the session colour scheme to libadwaita's style manager."""

    def __init__(self) -> None:
        self._subscription = 0
        self._connection: Gio.DBusConnection | None = None

    def start(self) -> None:
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            # Headless session: no preference to follow, leave libadwaita on
            # DEFAULT so it still tracks the toolkit's own detection.
            return
        self._connection = connection
        # Positional only: sender, interface, member, object_path, arg0, flags,
        # callback, user_data.
        self._subscription = connection.signal_subscribe(
            APPEARANCE_NAME,
            PROPERTIES_INTERFACE,
            "PropertiesChanged",
            APPEARANCE_PATH,
            APPEARANCE_INTERFACE,
            Gio.DBusSignalFlags.NONE,
            self._on_properties_changed,
            None,
        )
        self._read()

    def _on_properties_changed(self, *_args) -> None:
        self._read()

    def _read(self) -> None:
        if self._connection is None:
            return
        try:
            reply = self._connection.call_sync(
                APPEARANCE_NAME,
                APPEARANCE_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (APPEARANCE_INTERFACE, "color-scheme")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except GLib.Error:
            # No portal, no preference published: leave libadwaita on DEFAULT.
            return
        value = reply.unpack()[0]
        if value == PREFER_DARK:
            scheme = Adw.ColorScheme.PREFER_DARK
        elif value == PREFER_LIGHT:
            scheme = Adw.ColorScheme.PREFER_LIGHT
        else:
            scheme = Adw.ColorScheme.DEFAULT
        Adw.StyleManager.get_default().set_color_scheme(scheme)
