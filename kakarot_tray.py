#!/usr/bin/env python3
"""Deprecated entry point for Kakarot's system-tray applet.

GNOME has no native system tray, so a tray applet can only appear through a
third-party AppIndicator extension.  Kakarot no longer depends on one: the
front end is a GTK4/libadwaita application (``kakarot_gui``) that follows the
desktop's colour scheme and registers as ``io.github.kakarot`` over the session
bus.

This shim exists so existing launchers and shortcuts keep working; it simply
starts the real application.  It will be removed once downstream references
have migrated to ``kakarot-gui``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOGGER = logging.getLogger("kakarot.tray")

MESSAGE = (
    "kakarot_tray.py is deprecated: GNOME has no native tray. "
    "Launching the GTK4/libadwaita front end (kakarot_gui) instead."
)


def _system_python() -> str:
    """A interpreter that has PyGObject; the project venv does not."""
    for candidate in ("/usr/bin/python3", "/usr/bin/python3.13", "/usr/bin/python3.12"):
        if not os.path.exists(candidate):
            continue
        probe = (
            "import gi; gi.require_version('Gtk','4.0'); "
            "gi.require_version('Adw','1'); from gi.repository import Gtk, Adw"
        )
        if os.system(f"{candidate} -c \"{probe}\" >/dev/null 2>&1") == 0:
            return candidate
    return ""


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    LOGGER.warning(MESSAGE)
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    python = _system_python()
    if not python:
        LOGGER.error(
            "No interpreter with GTK4/libadwaita was found. Install "
            "python3-gobject and python3-gi-libadwaita, or run kakarot-gui."
        )
        return 1

    os.execve(
        python,
        [python, "-m", "kakarot_gui", *sys.argv[1:]],
        {**os.environ, "PYTHONPATH": str(root)},
    )
    return 0  # unreachable unless exec fails


if __name__ == "__main__":
    raise SystemExit(main())
