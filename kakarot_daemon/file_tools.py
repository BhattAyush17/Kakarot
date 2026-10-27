"""Home-confined file tools. No shell, overwrites, or script execution."""

from contextlib import contextmanager
import itertools
import os
from pathlib import Path
import shutil
import stat


def safe_path(raw: str, *, home: Path | None = None) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValueError("A nonempty file path is required")
    root = (home or Path.home()).resolve()
    expanded = Path(raw).expanduser()
    path = Path(
        os.path.abspath(expanded if expanded.is_absolute() else root / expanded)
    )
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Choose a path beneath your home directory")
    relative = path.relative_to(root)
    if any(part.startswith(".") for part in relative.parts):
        raise ValueError("Hidden configuration paths are protected")
    # Lexical containment alone does not stop symlink escapes.
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symbolic links are not permitted")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Path resolves outside your home directory")
    return path


@contextmanager
def directory_fd(path: Path):
    """Walk directories with O_NOFOLLOW, retaining a stable directory handle."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def write_file(file_path: str, content: str) -> dict:
    path = safe_path(file_path)
    if not isinstance(content, str) or len(content.encode("utf-8")) > 1_048_576:
        raise ValueError("Text content must be at most 1 MiB")
    data = content.encode("utf-8")
    with directory_fd(path.parent) as parent:
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        except BaseException:
            os.unlink(path.name, dir_fd=parent)
            raise
    return {"ok": True, "path": str(path), "bytes_written": len(data)}


CATEGORIES = {
    "Documents": {
        ".pdf",
        ".txt",
        ".md",
        ".doc",
        ".docx",
        ".odt",
        ".csv",
        ".xls",
        ".xlsx",
    },
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"},
    "Audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a"},
    "Videos": {".mp4", ".mkv", ".webm", ".mov"},
    "Archives": {".zip", ".tar", ".gz", ".7z"},
    "Scripts": {".py", ".sh", ".js"},
}


def organize_directory(target_directory: str) -> dict:
    folder = safe_path(target_directory)
    moved, skipped, errors = [], 0, []
    with directory_fd(folder) as source:
        # Keep both the scan and returned payload bounded.
        with os.scandir(source) as entries:
            names = [entry.name for entry in itertools.islice(entries, 501)]
        for name in names[:500]:
            try:
                info = os.stat(name, dir_fd=source, follow_symlinks=False)
                if name.startswith(".") or not stat.S_ISREG(info.st_mode):
                    skipped += 1
                    continue
                category = next(
                    (
                        key
                        for key, extensions in CATEGORIES.items()
                        if Path(name).suffix.lower() in extensions
                    ),
                    "Other",
                )
                try:
                    os.mkdir(category, mode=0o700, dir_fd=source)
                except FileExistsError:
                    pass
                target = os.open(
                    category,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=source,
                )
                try:
                    # Reserve the destination exclusively: collisions never overwrite user files.
                    reserved = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=target,
                    )
                    os.close(reserved)
                    try:
                        shutil.move(
                            f"/proc/self/fd/{source}/{name}",
                            f"/proc/self/fd/{target}/{name}",
                        )
                    except BaseException:
                        os.unlink(name, dir_fd=target)
                        raise
                    moved.append({"from": name, "to": f"{category}/{name}"})
                finally:
                    os.close(target)
            except OSError as exc:
                errors.append({"file": name, "error": type(exc).__name__})
    return {
        "ok": not errors,
        "moved_count": len(moved),
        "moved": moved[:20],
        "skipped": skipped,
        "errors": errors[:20],
        "scan_truncated": len(names) > 500,
        "details_truncated": len(moved) > 20 or len(errors) > 20,
    }
