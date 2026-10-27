"""Clipboard access over the Wayland-native ``wl-clipboard`` protocols.

Kakarot must never reach for X11 utilities (``xclip``/``xsel``): under Wayland
those talk to XWayland, which a compositor may not even be running, and they
cannot see the real clipboard.  ``wl-copy``/``wl-paste`` speak
``wl_data_device`` directly, so this works on a pure Wayland session.

Every call is a short-lived subprocess: the tools own the Wayland connection,
and holding one open in the daemon would cost more memory than it is worth.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

LOGGER = logging.getLogger(__name__)

# Wayland clipboard tools block until another client takes the selection, so
# every call is bounded.
TIMEOUT = 5.0


class ClipboardUnavailable(RuntimeError):
    """No Wayland clipboard tool, or no Wayland session to use it in."""


def _tool(name: str) -> str:
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise ClipboardUnavailable("no Wayland display; refusing to use X11 tools")
    path = shutil.which(name)
    if not path:
        raise ClipboardUnavailable(f"{name} is not installed")
    return path


async def paste() -> str:
    """Return the current clipboard contents as text."""
    tool = _tool("wl-paste")
    process = await asyncio.create_subprocess_exec(
        tool,
        "--no-newline",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        raise ClipboardUnavailable("wl-paste timed out") from None
    if process.returncode != 0:
        raise ClipboardUnavailable("wl-paste failed")
    return stdout.decode("utf-8", errors="replace")


async def copy(text: str) -> None:
    """Replace the clipboard contents with ``text``."""
    tool = _tool("wl-copy")
    process = await asyncio.create_subprocess_exec(
        tool,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(
            process.communicate(text.encode("utf-8")), timeout=TIMEOUT
        )
    except asyncio.TimeoutError:
        process.kill()
        raise ClipboardUnavailable("wl-copy timed out") from None
    if process.returncode != 0:
        raise ClipboardUnavailable("wl-copy failed")


async def clear() -> None:
    """Drop the clipboard selection without writing anything."""
    tool = _tool("wl-copy")
    process = await asyncio.create_subprocess_exec(
        tool, "--clear", stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(process.wait(), timeout=TIMEOUT)
