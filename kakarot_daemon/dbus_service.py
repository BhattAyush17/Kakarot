"""Native session-bus control plane for the Kakarot daemon.

This is the GNOME-native IPC surface: GNOME shortcuts, shell extensions, and
any other app can drive Kakarot with ``gdbus call`` or a proxy library instead of
speaking the daemon's private NDJSON dialect.  The private UNIX socket
(:mod:`kakarot_daemon.control`) stays for existing local clients; both share one
:class:`~kakarot_daemon.commands.CommandRouter`, so the vocabulary cannot drift.

Interface: ``io.github.kakarot.Daemon`` at ``/io/github/kakarot/Daemon``.

    io.github.kakarot.Daemon.Execute(s command, s arguments_json) -> s json
    io.github.kakarot.Daemon.Status() -> s json
    io.github.kakarot.Daemon.Version -> s
    Signal StateChanged(s json)

``jeepney`` is used rather than ``dbus-python``/``Gio`` so the 192 MiB daemon
cgroup does not have to carry a GLib main loop: this module owns a private
thread with its own connection, and the only work it hands back to the daemon's
event loop is command dispatch itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from kakarot_daemon.commands import CommandRouter

LOGGER = logging.getLogger(__name__)

BUS_NAME = "io.github.kakarot.Daemon"
OBJECT_PATH = "/io/github/kakarot/Daemon"
INTERFACE = "io.github.kakarot.Daemon"
VERSION = "kakarot"

INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"
PROPERTIES = "org.freedesktop.DBus.Properties"
PEER = "org.freedesktop.DBus.Peer"

INTROSPECTION = f"""<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="{INTERFACE}">
    <method name="Execute">
      <arg name="command" type="s" direction="in"/>
      <arg name="arguments" type="s" direction="in"/>
      <arg name="result" type="s" direction="out"/>
    </method>
    <method name="Status">
      <arg name="result" type="s" direction="out"/>
    </method>
    <property name="Version" type="s" access="read"/>
    <signal name="StateChanged">
      <arg name="state" type="s"/>
    </signal>
  </interface>
  <interface name="{INTROSPECTABLE}">
    <method name="Introspect">
      <arg name="xml" type="s" direction="out"/>
    </method>
  </interface>
  <interface name="{PROPERTIES}">
    <method name="Get">
      <arg name="interface" type="s" direction="in"/>
      <arg name="property" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg name="interface" type="s" direction="in"/>
      <arg name="properties" type="a{{sv}}" direction="out"/>
    </method>
  </interface>
  <interface name="{PEER}">
    <method name="Ping"/>
    <method name="GetMachineId">
      <arg name="id" type="s" direction="out"/>
    </method>
  </interface>
</node>
"""


class DBusService:
    """Own ``io.github.kakarot.Daemon`` on the session bus.

    :param router: Shared command dispatcher.
    :param status: Returns the current snapshot; owned by the coordinator.
    """

    def __init__(
        self,
        router: CommandRouter,
        status: Callable[[], dict[str, Any]],
    ) -> None:
        self._router = router
        self._status = status
        self._connection: Any = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Request the well-known name on a background thread.

        The daemon must keep running when no session bus is reachable (a
        headless unit, an SSH session), so failure is logged, never fatal.
        """
        self._thread = threading.Thread(
            target=self._serve, name="kakarot-dbus", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - teardown must never raise
                LOGGER.debug("Error closing D-Bus connection", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    @property
    def available(self) -> bool:
        """True once the well-known name is owned and being served."""
        return self._connection is not None

    # -- serving ----------------------------------------------------------

    def _serve(self) -> None:
        from jeepney import DBusNameFlags
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import open_dbus_connection

        try:
            connection = open_dbus_connection(bus="SESSION")
        except Exception as exc:  # noqa: BLE001 - no session bus is legitimate
            LOGGER.warning(
                "Session bus unavailable; D-Bus control plane off: %s", exc
            )
            return
        try:
            reply = connection.send_and_get_reply(
                message_bus.RequestName(BUS_NAME, DBusNameFlags.do_not_queue),
                timeout=5,
            )
            code = reply.body[0]
            # 1 = PRIMARY_OWNER, 4 = ALREADY_OWNER
            if code not in (1, 4):
                LOGGER.warning(
                    "Could not own %s on the session bus (code %s)", BUS_NAME, code
                )
                return
            self._connection = connection
            LOGGER.info("Owning %s on the session bus", BUS_NAME)
            method_call = message_type_method_call()
            while True:
                message = connection.receive()
                if message.header.message_type is not method_call:
                    continue
                try:
                    reply = self._dispatch(message)
                    if reply is not None:
                        connection.send(reply)
                except Exception:  # noqa: BLE001 - one bad call must not end the bus
                    LOGGER.warning("D-Bus call failed", exc_info=True)
        except Exception:  # noqa: BLE001 - the daemon outlives its bus thread
            LOGGER.warning("D-Bus control plane stopped", exc_info=True)
        finally:
            self._connection = None
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    def _dispatch(self, message: Any) -> Any:
        from jeepney import HeaderFields, new_error, new_method_return

        def field(name: Any) -> str | None:
            value = message.header.fields.get(name)
            if isinstance(value, list):
                value = value[0] if value else None
            return value

        member = field(HeaderFields.member)
        interface = field(HeaderFields.interface)

        if interface == PEER:
            if member == "Ping":
                return new_method_return(message)
            if member == "GetMachineId":
                return new_method_return(message, "s", ("kakarot",))
        if interface == INTROSPECTABLE and member == "Introspect":
            return new_method_return(message, "s", (INTROSPECTION,))
        if interface == PROPERTIES:
            if member == "GetAll":
                # Values of an 'a{sv}' must be explicit variants.
                return new_method_return(
                    message, "a{sv}", ({"Version": ("s", VERSION)},)
                )
            if member == "Get":
                name = message.body[1] if len(message.body) > 1 else ""
                if name == "Version":
                    return new_method_return(message, "v", (("s", VERSION),))
                return new_error(
                    message,
                    "org.freedesktop.DBus.Error.UnknownProperty",
                    "s",
                    (f"no such property {name}",),
                )
        if interface in (INTERFACE, None):
            if member == "Execute":
                return self._execute(message)
            if member == "Status":
                return new_method_return(
                    message, "s", (json.dumps(self._status()),)
                )
        return new_error(
            message,
            "org.freedesktop.DBus.Error.UnknownMethod",
            "s",
            (f"no such method {interface}.{member}",),
        )

    def _peer_is_same_user(self, message: Any) -> bool:
        """Reject Execute calls from a different Unix user on the session bus.

        The session bus is normally per-user, but a misconfigured bus (or a
        forwarded bus in a remote session) must not turn the agent into a
        cross-user command runner. Status and introspection remain readable.
        """
        from jeepney import DBusAddress, HeaderFields, new_method_call

        connection = self._connection
        sender = message.header.fields.get(HeaderFields.sender)
        if isinstance(sender, list):
            sender = sender[0] if sender else None
        if connection is None or not sender:
            return False
        try:
            address = DBusAddress(
                "/org/freedesktop/DBus",
                bus_name="org.freedesktop.DBus",
                interface="org.freedesktop.DBus",
            )
            reply = connection.send_and_get_reply(
                new_method_call(
                    address,
                    "GetConnectionUnixUser",
                    "s",
                    body=(str(sender),),
                ),
                timeout=2,
            )
            return int(reply.body[0]) == os.getuid()
        except Exception:  # noqa: BLE001 - fail closed
            LOGGER.warning("Could not verify D-Bus peer credentials", exc_info=True)
            return False

    def _execute(self, message: Any) -> Any:
        from jeepney import new_error, new_method_return

        if not self._peer_is_same_user(message):
            return new_error(
                message,
                "org.freedesktop.DBus.Error.AccessDenied",
                "s",
                ("Kakarot only accepts control calls from the owning user.",),
            )

        command = message.body[0] if message.body else ""
        raw = message.body[1] if len(message.body) > 1 else "{}"
        try:
            request = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            request = {}
        if not isinstance(request, dict):
            request = {}
        try:
            result = self._router.handle(str(command), request)
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the bus
            LOGGER.warning("Command %r failed: %s", command, exc)
            result = {"ok": False, "error": str(exc)}
        return new_method_return(message, "s", (json.dumps(result),))

    # -- signals ----------------------------------------------------------

    def publish_state(self, snapshot: dict[str, Any]) -> None:
        """Emit ``StateChanged`` so clients need no polling loop."""
        connection = self._connection
        if connection is None:
            return
        from jeepney import DBusAddress, new_signal

        # jeepney 0.9 takes a DBusAddress plus the body as a *tuple*; passing a
        # bare path and interface silently produces an undeliverable message.
        address = DBusAddress(OBJECT_PATH, BUS_NAME, INTERFACE)
        try:
            connection.send(
                new_signal(address, "StateChanged", "s", (json.dumps(snapshot),))
            )
        except Exception:  # noqa: BLE001 - never fail a state publication
            # A dropped signal must not kill the daemon, but it must be
            # visible: losing it leaves every client silently stale.
            LOGGER.warning("StateChanged signal dropped", exc_info=True)


def message_type_method_call() -> Any:
    from jeepney import MessageType

    return MessageType.method_call


async def call_dbus(command: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call ``Execute`` on the running daemon, preferring D-Bus to the socket.

    Falls back to the private socket when ``gdbus`` is unavailable or the daemon
    was started without a session bus.
    """
    import shutil

    if shutil.which("gdbus"):
        process = await asyncio.create_subprocess_exec(
            "gdbus",
            "call",
            "--session",
            "--dest",
            BUS_NAME,
            "--object-path",
            OBJECT_PATH,
            "--method",
            f"{INTERFACE}.Execute",
            command,
            json.dumps(arguments or {}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0:
            text = stdout.decode().strip()
            # gdbus prints a GVariant literal, e.g. ('{"ok": true}',).
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except ValueError:
                    pass
    from kakarot_daemon.config import KakarotDaemonConfig
    from kakarot_daemon.control import send_command

    return await send_command(
        KakarotDaemonConfig.from_environment().data_dir / "kakarot.sock", command
    )
