"""Launch user-defined desktop applications by name.

The user maintains an allowlist of apps in the config (Config UI "Apps" tab):
each entry has a display name, the command/path to launch, and optional aliases.
Both the deterministic voice fast-path (services.voice_intents) and the LLM tool
(services.tool_runtime) pass only the *name* the user referred to; this module
resolves that name against the allowlist and runs the configured command.

Security: neither the model nor the speech path can launch an arbitrary path —
only commands the user explicitly registered are ever executed.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
import unicodedata
from typing import Any

from core.config_store import get_config

# Match resolution: an app must reach this score to be considered a hit. Mirrors
# the threshold idea from tools.home_assistant so a vague word does not launch
# the wrong program.
MATCH_THRESHOLD = 100


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    # Drop punctuation so a name like "Photoshop, Zezinho" tokenizes cleanly and
    # both words can trigger a match (aliases are still the intended field).
    cleaned = re.sub(r"[^\w\s]", " ", stripped.lower())
    return " ".join(cleaned.split())


def _tokens(text: str) -> set[str]:
    return {token for token in _normalize(text).split() if token}


def _app_names(app: dict[str, Any]) -> list[str]:
    names = [app.get("name"), *(app.get("aliases") or [])]
    return [str(name).strip() for name in names if str(name or "").strip()]


def _configured_apps(config: dict[str, Any] | None, enabled_only: bool = True) -> list[dict[str, Any]]:
    config = config if isinstance(config, dict) else get_config()
    apps = config.get("tools", {}).get("applications")
    if not isinstance(apps, list):
        return []
    result: list[dict[str, Any]] = []
    for app in apps:
        if not isinstance(app, dict):
            continue
        if not str(app.get("command") or "").strip():
            continue
        if enabled_only and not app.get("enabled", True):
            continue
        result.append(app)
    return result


def _app_match_score(app: dict[str, Any], query: str) -> int:
    query_key = _normalize(query)
    query_tokens = _tokens(query)
    if not query_key or not query_tokens:
        return 0
    best = 0
    for name in _app_names(app):
        name_key = _normalize(name)
        name_tokens = _tokens(name)
        if not name_tokens:
            continue
        if query_key == name_key:
            best = max(best, 1000)
        elif name_key in query_key:
            best = max(best, 800 + len(name_tokens))
        elif query_key in name_key:
            best = max(best, 650 + len(query_tokens))
        elif name_tokens.issubset(query_tokens):
            best = max(best, 500 + len(name_tokens) * 10)
        else:
            overlap = len(name_tokens & query_tokens)
            if overlap:
                best = max(best, overlap * 20 - len(name_tokens - query_tokens) * 8)
    return best


def _match_app(query: str, apps: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored = sorted(
        ((_app_match_score(app, query), app) for app in apps),
        key=lambda item: item[0],
        reverse=True,
    )
    if scored and scored[0][0] >= MATCH_THRESHOLD:
        return scored[0][1]
    return None


def resolve_application(name: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return the configured app matching ``name``, or None."""
    query = str(name or "").strip()
    if not query:
        return None
    return _match_app(query, _configured_apps(config))


def list_applications(config: dict[str, Any] | None = None) -> dict[str, Any]:
    apps = _configured_apps(config)
    names = [str(app.get("name") or "").strip() for app in apps if str(app.get("name") or "").strip()]
    answer = (
        "Apps I can open: " + ", ".join(names) + "."
        if names
        else "No applications are configured yet. Add them in the Kakarot config, Apps tab."
    )
    return {"ok": True, "applications": names, "count": len(names), "answer": answer}


def _launch(command: str) -> None:
    """Launch a command/path. Runs synchronously; call via asyncio.to_thread."""
    command = command.strip()
    if sys.platform == "win32" and os.path.exists(command):
        # os.startfile handles .exe/.lnk/folders/registered file types and does
        # not block on the child process.
        os.startfile(command)  # type: ignore[attr-defined]
        return
    # Path with arguments, a URL/protocol, or a non-win32 host: hand it to the
    # shell so PATH lookup and argument splitting behave like a Run dialog.
    if sys.platform == "win32":
        subprocess.Popen(command, shell=True)  # noqa: S602 - allowlisted command only
    else:
        subprocess.Popen(command, shell=True, start_new_session=True)  # noqa: S602


async def open_application(name: str, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve ``name`` against the allowlist and launch the configured command."""
    started = time.perf_counter()
    config = config if isinstance(config, dict) else get_config()
    apps = _configured_apps(config)
    if not apps:
        return {
            "ok": False,
            "error": "No applications are configured. Add them in the Kakarot config, Apps tab.",
            "available": [],
        }

    app = _match_app(str(name or ""), apps)
    if app is None:
        available = [str(app.get("name") or "").strip() for app in apps]
        message = f"No configured application matches {name!r}."
        if available:
            message += " Apps I can open: " + ", ".join(available) + "."
        return {"ok": False, "error": message, "answer": message, "available": available}

    command = str(app.get("command") or "").strip()
    app_name = str(app.get("name") or command).strip()
    try:
        await asyncio.to_thread(_launch, command)
    except Exception as exc:  # noqa: BLE001 - surface any launch failure to the caller
        message = f"Could not open {app_name}: {exc}"
        return {"ok": False, "error": message, "answer": message, "app": app_name, "command": command}

    return {
        "ok": True,
        "app": app_name,
        "command": command,
        "answer": f"Opening {app_name}.",
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }


def _native_pick_application() -> str | None:
    """Show the OS "open file" dialog and return the chosen path, or None.

    Runs fully inside one worker thread: the Tk root is created, used and
    destroyed here, so it never touches the server's event loop. The config
    page runs in a browser (no filesystem access), so the picker has to live on
    the backend, which shares the user's interactive desktop session.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:  # noqa: BLE001 - tkinter missing on this host: caller falls back to manual entry
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askopenfilename(
            parent=root,
            title="Select an application",
            filetypes=[
                ("Programs", "*.exe *.lnk *.bat *.cmd *.com *.url"),
                ("All files", "*.*"),
            ],
        )
    finally:
        root.destroy()
    return str(path) or None


async def pick_application() -> dict[str, Any]:
    """Open the native file picker and return the selected path + a suggested name."""
    path = await asyncio.to_thread(_native_pick_application)
    if not path:
        return {"ok": False, "canceled": True}
    name = os.path.splitext(os.path.basename(path))[0].strip()
    return {"ok": True, "path": path, "name": name}
