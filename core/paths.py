"""Cross-platform paths for mutable Kakarot user data."""

from __future__ import annotations

import os
from pathlib import Path


def user_data_dir() -> Path:
    configured = os.environ.get("KAKAROT_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return base / "Kakarot"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "kakarot"
