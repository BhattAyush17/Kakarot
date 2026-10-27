"""Active-window text capture on Wayland, via ``wl-clipboard`` only.

Reading a window's text means asking it to copy itself, then reading the
clipboard.  The clipboard half goes through :mod:`kakarot_daemon.clipboard`,
which refuses to fall back to X11 tools: under a Wayland compositor those read
XWayland's clipboard, which is not the user's real clipboard.

There is no compositor-bypassing alternative: Kakarot does not poke at
``wlroots`` sockets or read other clients' surfaces.
"""

from __future__ import annotations

import asyncio
import logging

from kakarot_daemon import clipboard

LOGGER = logging.getLogger(__name__)

# The focused client needs a moment to publish its selection after the
# synthetic Ctrl+A / Ctrl+C.
SETTLE_SECONDS = 0.15
MAX_CHARS = 8_000


async def read_active_window_text() -> dict[str, str]:
    """Return the focused window's selected (or all) text."""
    # Snapshot the user's clipboard *before* we overwrite it, otherwise there is
    # nothing left to restore and Ctrl+C is destructive.
    try:
        original = await clipboard.paste()
    except clipboard.ClipboardUnavailable as exc:
        return {"error": str(exc)}

    if not await _send_copy():
        return {"error": "Could not send a copy request to the focused window."}

    await asyncio.sleep(SETTLE_SECONDS)
    try:
        captured = await clipboard.paste()
    except clipboard.ClipboardUnavailable as exc:
        captured = original
    finally:
        # Always give the clipboard back, including when the capture failed.
        if captured != original:
            try:
                await clipboard.copy(original)
            except clipboard.ClipboardUnavailable as exc:
                LOGGER.warning("Could not restore the clipboard: %s", exc)

    text = " ".join(captured.split())
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "... (truncated)"
    return {"content": text}


async def _send_copy() -> bool:
    """Send Ctrl+A, Ctrl+C to the focused client.

    ``wtype`` speaks ``virtual-keyboard-v1`` through the compositor, so this is
    an ordinary input event rather than a synthetic X11 keypress.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "wtype",
            "-M", "ctrl", "-P", "a", "-p", "a", "-P", "c", "-p", "c", "-m", "ctrl",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        LOGGER.warning("wtype is not installed; cannot read the focused window")
        return False
    return await process.wait() == 0
