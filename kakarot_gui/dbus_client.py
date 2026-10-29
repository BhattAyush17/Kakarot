"""Session-bus client for the Kakarot daemon.

Talks to ``io.github.kakarot.Daemon`` over the session bus using GDBus, so the
front end never parses the daemon's private NDJSON protocol and needs no
third-party D-Bus library.
"""

from __future__ import annotations

import json
import logging

from gi.repository import Gio, GLib

from kakarot_gui import APP_ID, DAEMON_INTERFACE, DAEMON_NAME, DAEMON_PATH

LOGGER = logging.getLogger(__name__)

RETRY_SECONDS = 2.0
APP_ICON = "io.github.kakarot.Daemon"


class DaemonClient:
    """Async facade over the daemon's ``Execute`` method and state signal."""

    def __init__(self) -> None:
        self._connection: Gio.DBusConnection | None = None
        self._proxy: Gio.DBusProxy | None = None
        self._state: dict = {}
        self._handlers: list = []
        self._retry_source = 0
        self._subscription = 0
        # ``stop`` runs even when ``start`` never did (early quit), so the
        # name-watch source must exist from construction time.
        self._watch_name_source = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._connect()

    def stop(self) -> None:
        if self._watch_name_source:
            GLib.source_remove(self._watch_name_source)
            self._watch_name_source = 0
        if self._retry_source:
            GLib.source_remove(self._retry_source)
            self._retry_source = 0
        if self._proxy is not None:
            self._proxy.disconnect_signals()
            self._proxy = None
        if self._subscription:
            self._connection.signal_unsubscribe(self._subscription)
            self._subscription = 0
        self._connection = None

    def _connect(self) -> None:
        # Synchronous: the front end needs the connection before it can build
        # a proxy, and this happens once at startup.
        self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._watch_name(self._connection)

    def _watch_name(self, connection: Gio.DBusConnection) -> None:
        # GDBus's signal_subscribe takes positional arguments only: sender,
        # interface, member, object_path, arg0, flags, callback, user_data.
        self._subscription = connection.signal_subscribe(
            "org.freedesktop.DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            "/org/freedesktop/DBus",
            DAEMON_NAME,
            Gio.DBusSignalFlags.NONE,
            self._on_name_owner_changed,
            None,
        )
        # The daemon is a systemd unit; ask the bus to start it if it is down.
        # Failure is not fatal: the unit may be masked, and the window then
        # simply reports the daemon as unavailable.
        try:
            connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "StartServiceByName",
                GLib.Variant("(su)", (DAEMON_NAME, 0)),
                GLib.VariantType("(u)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
        except GLib.Error:
            LOGGER.debug("Daemon is not D-Bus activatable; waiting for systemd")
        self._build_proxy()

    def _build_proxy(self) -> None:
        if self._connection is None:
            return
        if self._proxy is not None:
            self._proxy.disconnect_signals()
            self._proxy = None
        try:
            self._proxy = Gio.DBusProxy.new_sync(
                self._connection,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES
                | Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None,
                DAEMON_NAME,
                DAEMON_PATH,
                DAEMON_INTERFACE,
                None,
            )
        except GLib.Error as exc:
            LOGGER.debug("Daemon proxy unavailable: %s", exc)
            self._schedule_retry()
            return
        if self._proxy.get_name_owner() is None:
            self._schedule_retry()
            return
        self._proxy.connect("g-signal", self._on_state_changed)
        self._refresh()

    def _on_name_owner_changed(
        self, _conn, _sender, _path, _interface, signal_name, parameters, _user_data
    ) -> None:
        # Gio.DBusConnection.signal_subscribe hands over
        # (connection, sender, path, interface, signal_name, parameters, user_data).
        if signal_name != "NameOwnerChanged":
            return
        try:
            name, _old_owner, new_owner = parameters.unpack()
        except (TypeError, ValueError):
            return
        if name != DAEMON_NAME:
            return
        if new_owner:
            self._build_proxy()
        else:
            self._set_state({})

    def _schedule_retry(self) -> None:
        if self._retry_source:
            return
        self._retry_source = GLib.timeout_add_seconds(
            RETRY_SECONDS, self._on_retry
        )

    def _on_retry(self) -> bool:
        self._retry_source = 0
        self._build_proxy()
        return GLib.SOURCE_REMOVE

    # -- state ------------------------------------------------------------

    @property
    def state(self) -> dict:
        return dict(self._state)

    @property
    def available(self) -> bool:
        return bool(self._state)

    def connect_state(self, callback) -> None:
        """Call ``callback(state)`` on every published snapshot."""
        self._handlers.append(callback)

    def _set_state(self, state: dict) -> None:
        self._state = state
        for handler in tuple(self._handlers):
            handler(dict(state))

    def _on_state_changed(self, _proxy, _sender, signal_name, parameters) -> None:
        # GDBusProxy's "g-signal" passes (proxy, sender, signal_name, parameters);
        # the payload is the last argument, not the third.
        if signal_name != "StateChanged":
            return
        try:
            payload = json.loads(parameters.unpack()[0])
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict):
            self._set_state(payload)

    def _refresh(self) -> None:
        if self._proxy is None:
            return
        GLib.idle_add(self._refresh_now)

    def _refresh_now(self) -> bool:
        if self._proxy is None:
            return GLib.SOURCE_REMOVE
        try:
            reply = self._proxy.call_sync(
                "Status", None, Gio.DBusCallFlags.NONE, 3000, None
            )
            payload = json.loads(reply.unpack()[0])
        except GLib.Error as exc:
            self._schedule_retry()
            return GLib.SOURCE_REMOVE
        if isinstance(payload, dict):
            self._set_state(payload)
        return GLib.SOURCE_REMOVE

    # -- commands ---------------------------------------------------------

    def command(self, command: str, arguments: dict | None = None) -> str:
        """Send one control command; returns the daemon's JSON reply."""
        if self._proxy is None or self._proxy.get_name_owner() is None:
            self._schedule_retry()
            return '{"ok": false, "error": "daemon unavailable"}'
        try:
            reply = self._proxy.call_sync(
                "Execute",
                GLib.Variant("(ss)", (command, json.dumps(arguments or {}))),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as exc:
            return json.dumps({"ok": False, "error": exc.message})
        return reply.unpack()[0]

    def notify(self, title: str, body: str, urgency: int = 1) -> None:
        """Surface a native GNOME notification, respecting Do Not Disturb."""
        if self._connection is None:
            return
        params = GLib.Variant(
            "(susssasa{sv}i)",
            (
                APP_ICON,
                0,
                title,
                body,
                body,
                [],
                {"urgency": GLib.Variant("y", urgency)},
                -1,
            ),
        )
        self._connection.call(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            params,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
