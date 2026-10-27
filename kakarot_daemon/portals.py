"""Screen capture through XDG Desktop Portals, never around the compositor.

Any screen access Kakarot needs must be granted by the user's compositor.  This
module talks to ``org.freedesktop.portal.ScreenCast``: the portal shows the
user the system's own picker, and hands back a PipeWire node that the daemon may
record from.  There is deliberately no path that touches a Wayland socket or an
X11 display directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

LOGGER = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACE = "org.freedesktop.portal.ScreenCast"

PERSISTENCE_MODE_TRANSIENT = 1
PERSISTENCE_MODE_PERSISTENT = 2

# A pending selector: the user may take as long as they like to pick a source.
SELECT_TIMEOUT = 300.0


class PortalUnavailable(RuntimeError):
    """No portal, or the user declined/cancelled the request."""


def portal_available() -> bool:
    """True when an XDG portal implementation is installed and reachable."""
    return bool(os.environ.get("XDG_RUNTIME_DIR")) and bool(
        shutil.which("gdbus")
    )


async def _call(method: str, options: str, handle_token: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "gdbus",
        "call",
        "--session",
        "--dest", PORTAL_BUS,
        "--object-path", PORTAL_PATH,
        "--method", f"{PORTAL_INTERFACE}.{method}",
        options,
        handle_token,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(), timeout=SELECT_TIMEOUT
    )
    if process.returncode != 0:
        raise PortalUnavailable(
            stderr.decode(errors="replace").strip() or f"{method} failed"
        )
    return stdout.decode(errors="replace")


def _request_token() -> str:
    return f"kakarot{os.getpid()}{id(object()) & 0xFFFF:04x}"


async def select_source(*, interactive: bool = True):
    """Ask the user to pick a screen or window.

    Returns the raw portal response as a dict.  Raises
    :class:`PortalUnavailable` when the user cancels or the portal refuses.
    """
    if not portal_available():
        raise PortalUnavailable(
            "xdg-desktop-portal is not available; screen access is not permitted"
        )
    token = _request_token()
    options = json.dumps(
        {
            "handle_token": token,
            "persist_mode": PERSISTENCE_MODE_TRANSIENT,
            "interactive": interactive,
        }
    )
    response = await _call("SelectSources", options, f"select_{token}")
    return _parse_response(response)


def _parse_response(response: str) -> dict:
    """Pull the result dict out of gdbus's GVariant literal output."""
    # e.g. ({'streams': [{'node-id': '42', ...}], 'persist_mode': <1>},)
    start = response.find("{")
    end = response.rfind("}")
    if start == -1 or end <= start:
        raise PortalUnavailable("malformed portal response")
    fragment = response[start : end + 1]
    # gdbus prints Python-ish literals; normalise the few differences so the
    # standard library can read it.
    fragment = fragment.replace("<", "").replace(">", "")
    import ast

    try:
        parsed = ast.literal_eval(fragment)
    except (ValueError, SyntaxError) as exc:
        raise PortalUnavailable(f"unreadable portal response: {exc}") from None
    if not isinstance(parsed, dict):
        raise PortalUnavailable("unexpected portal response")
    return parsed


async def record_to(path: str, *, seconds: float) -> None:
    """Record the user's selected source to ``path`` with PipeWire.

    Requires ``pw-cat`` from ``pipewire-bin``.  Recording only ever covers what
    the user explicitly selected in the portal picker.
    """
    if not shutil.which("pw-cat"):
        raise PortalUnavailable("pw-cat is not installed; cannot record the portal stream")
    selection = await select_source()
    streams = selection.get("streams") or []
    if not streams:
        raise PortalUnavailable("no stream was selected")
    node_id = streams[0].get("node-id")
    if node_id is None:
        raise PortalUnavailable("the portal returned no PipeWire node")
    process = await asyncio.create_subprocess_exec(
        "pw-cat",
        "--record",
        "--target", str(node_id),
        "--filename", path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        process.terminate()
        await process.wait()
