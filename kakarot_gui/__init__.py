"""Kakarot's GNOME-native front end.

A GTK4 + libadwaita application that lives in the background and presents
Kakarot's audit trail and settings.  It replaces the legacy system-tray applet:
GNOME has no native tray, and a third-party AppIndicator extension is not a
dependency Kakarot should carry.  The window is opt-in, so the app can run as
a session-resident background presence like any other GNOME service.
"""

from __future__ import annotations

APP_ID = "io.github.kakarot"
DAEMON_NAME = "io.github.kakarot.Daemon"
DAEMON_PATH = "/io/github/kakarot/Daemon"
DAEMON_INTERFACE = "io.github.kakarot.Daemon"

__all__ = ["APP_ID", "DAEMON_NAME", "DAEMON_PATH", "DAEMON_INTERFACE"]
