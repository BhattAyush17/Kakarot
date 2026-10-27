"""Native GNOME notifications over ``org.freedesktop.Notifications``.

Alerts go through the notification daemon rather than a bespoke popup so they
land in the GNOME notification centre, respect Do Not Disturb, and appear on
every other desktop sharing the same session bus.

The daemon runs under the project venv, which has no GLib, so this speaks D-Bus
with :mod:`jeepney` instead of ``Gio.Notification``.
"""

from __future__ import annotations

import asyncio
import logging

from jeepney import MessageGenerator

LOGGER = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"
INTERFACE = "org.freedesktop.Notifications"

APP_NAME = "Kakarot"
APP_ICON = "io.github.kakarot.Daemon"

URGENCY_LOW = 0
URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2

TIMEOUT_DEFAULT = 5_000
TIMEOUT_NEVER = 0


def _connection():
    from jeepney.io.blocking import open_dbus_connection

    return open_dbus_connection(bus="SESSION")


class _Notifications(MessageGenerator):
    """Method table for ``org.freedesktop.Notifications``."""

    interface = INTERFACE

    def __init__(self) -> None:
        super().__init__(object_path=OBJECT_PATH, bus_name=BUS_NAME)


def notify_sync(
    summary: str,
    body: str = "",
    *,
    urgency: int = URGENCY_NORMAL,
    timeout_ms: int = TIMEOUT_DEFAULT,
) -> bool:
    """Show one notification.  Returns False when no daemon is reachable."""
    from jeepney import new_method_call

    try:
        connection = _connection()
    except Exception as exc:  # noqa: BLE001 - a headless session has no bus
        LOGGER.debug("Notification bus unavailable: %s", exc)
        return False
    try:
        reply = connection.send_and_get_reply(
            new_method_call(
                _Notifications(),
                "Notify",
                "susssasa{sv}i",
                (
                    APP_NAME,
                    0,
                    APP_ICON,
                    summary,
                    body,
                    [],
                    {"urgency": ("y", urgency)},
                    timeout_ms,
                ),
            ),
            timeout=5,
        )
        return int(reply.body[0]) > 0
    except Exception as exc:  # noqa: BLE001 - never fail a state transition
        LOGGER.warning("Notification failed: %s", exc)
        return False
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass


async def notify(summary: str, body: str = "", *, urgency: int = URGENCY_NORMAL) -> None:
    """Show one notification without blocking the daemon's event loop."""
    await asyncio.to_thread(notify_sync, summary, body, urgency=urgency)


async def notify_reminder(title: str) -> None:
    await notify("Reminder", title, urgency=URGENCY_CRITICAL)


async def notify_hitl(tool: str) -> None:
    await notify(
        "Confirmation required",
        f"Kakarot needs approval to run: {tool}",
        urgency=URGENCY_CRITICAL,
    )
