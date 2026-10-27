"""Overlay configuration and key validation."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request

from kakarot_daemon.config import KakarotDaemonConfig
from kakarot_daemon.credentials import (
    get_gemini_api_key,
    set_gemini_api_key,
    verify_gemini_api_key,
)


def load_api_key() -> str:
    return get_gemini_api_key()


def save_api_key(api_key: str) -> str:
    return set_gemini_api_key(api_key)


def model_name() -> str:
    return KakarotDaemonConfig.from_environment().model


def test_api_key(api_key: str) -> str:
    """Test a Gemini API key without exposing it in a URL or error message."""
    if not api_key.strip():
        raise ValueError("Enter a Gemini API key first.")
    verify_gemini_api_key(api_key, opener=urllib.request.urlopen)
    return "Key verified. Voice access is checked when listening starts."


def load_preferences() -> dict:
    try:
        value = json.loads((KakarotDaemonConfig.from_environment().config_dir / "overlay.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preferences(x: int, y: int, hidden: bool) -> None:
    directory = KakarotDaemonConfig.from_environment().config_dir
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=".overlay-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"x": x, "y": y, "hidden": hidden}, stream)
        os.replace(temporary, directory / "overlay.json")
        try:
            os.chmod(directory / "overlay.json", 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
