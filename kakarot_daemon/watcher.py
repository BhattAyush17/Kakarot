"""Inotify Watcher Pruning for Kakarot."""

import os
from pathlib import Path

# Try to import watchdog types for type hinting, fallback to object if not installed.
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    Observer = object
    FileSystemEventHandler = object

MAX_DEPTH = 4
BLACKLIST = {
    ".git", 
    "node_modules", 
    "venv", 
    "__pycache__", 
    ".cache", 
    ".local", 
    ".rustup", 
    "target"
}

def attach_pruned_watches(observer: Observer, handler: FileSystemEventHandler, root_path: Path) -> None:
    """
    Manually traverse the directory tree, applying non-recursive watches
    only to paths that are not blacklisted, not hidden, and within the MAX_DEPTH.
    """
    root_str = str(root_path.resolve())
    
    for root, dirs, files in os.walk(root_str):
        rel_path = os.path.relpath(root, root_str)
        if rel_path == '.':
            depth = 0
        else:
            depth = rel_path.count(os.sep) + 1

        if depth >= MAX_DEPTH:
            dirs.clear()
            continue

        # Filter out blacklisted and hidden directories in-place
        valid_dirs = []
        for d in dirs:
            if d.startswith('.'):
                continue
            if d in BLACKLIST:
                continue
            valid_dirs.append(d)
        
        dirs[:] = valid_dirs
        
        # Schedule a non-recursive watch for this specific valid directory.
        # A directory may be deleted between os.walk listing it and us
        # attaching the watch; skip it instead of aborting the whole walk.
        try:
            observer.schedule(handler, root, recursive=False)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
