"""Credential storage helpers for Kakarot secrets."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any


SERVICE_NAME = "Kakarot"
LEGACY_SERVICE_NAME = "Digi" + "Mon"
INWORLD_API_KEY_SECRET = "inworld_api_key"
OPENAI_API_KEY_SECRET = "openai_api_key"
ANTHROPIC_API_KEY_SECRET = "anthropic_api_key"
GEMINI_API_KEY_SECRET = "gemini_api_key"


class SecretStoreError(RuntimeError):
    """Raised when neither the keyring nor encrypted local storage is usable."""


_fallback_lock = threading.RLock()


@lru_cache(maxsize=1)
def _keyring_module() -> Any | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    return keyring


def _fallback_paths() -> tuple[Path, Path]:
    configured = os.environ.get("KAKAROT_CONFIG_HOME", "").strip()
    if configured:
        directory = Path(configured).expanduser()
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        directory = base / "kakarot"
    return directory / "secrets.enc", directory / "secrets.key"


def _secure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise SecretStoreError("The Kakarot configuration directory has unsafe ownership or type.")
    path.chmod(0o700)


def _read_private(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid():
        raise SecretStoreError("Encrypted local credentials have unsafe ownership or file type.")
    path.chmod(0o600)
    return path.read_bytes()


def _atomic_private_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load_fallback(*, create_key: bool = False):
    """Return (Fernet, encrypted file path) for the per-user fallback."""
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise SecretStoreError(
            "Keyring is unavailable and encrypted fallback support is missing (cryptography)."
        ) from exc

    data_path, key_path = _fallback_paths()
    _secure_directory(data_path.parent)
    key = _read_private(key_path)
    if key is None:
        if not create_key:
            return None, data_path
        generated = Fernet.generate_key()
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = _read_private(key_path)
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(generated)
                stream.flush()
                os.fsync(stream.fileno())
            key = generated
    try:
        return Fernet(key), data_path
    except (TypeError, ValueError) as exc:
        raise SecretStoreError("The encrypted credential key is invalid; remove it and save the key again.") from exc


def _fallback_read(name: str) -> str:
    with _fallback_lock:
        fernet, data_path = _load_fallback()
        if fernet is None:
            return ""
        encrypted = _read_private(data_path)
        if encrypted is None:
            return ""
        try:
            values = json.loads(fernet.decrypt(encrypted))
        except Exception as exc:
            raise SecretStoreError("The encrypted credential file cannot be decrypted.") from exc
        value = values.get(name, "") if isinstance(values, dict) else ""
        return value if isinstance(value, str) else ""


def _fallback_write(name: str, value: str) -> None:
    with _fallback_lock:
        fernet, data_path = _load_fallback(create_key=True)
        encrypted = _read_private(data_path)
        if encrypted is None:
            values = {}
        else:
            try:
                values = json.loads(fernet.decrypt(encrypted))
            except Exception as exc:
                raise SecretStoreError("The encrypted credential file cannot be decrypted.") from exc
            if not isinstance(values, dict):
                raise SecretStoreError("The encrypted credential file has an invalid format.")
        values[name] = value
        blob = fernet.encrypt(json.dumps(values, separators=(",", ":")).encode("utf-8"))
        _atomic_private_write(data_path, blob)


def _fallback_delete(name: str) -> None:
    with _fallback_lock:
        fernet, data_path = _load_fallback()
        if fernet is None:
            return
        encrypted = _read_private(data_path)
        if encrypted is None:
            return
        try:
            values = json.loads(fernet.decrypt(encrypted))
        except Exception as exc:
            raise SecretStoreError("The encrypted credential file cannot be decrypted.") from exc
        if isinstance(values, dict) and name in values:
            del values[name]
            if values:
                blob = fernet.encrypt(json.dumps(values, separators=(",", ":")).encode("utf-8"))
                _atomic_private_write(data_path, blob)
            else:
                data_path.unlink(missing_ok=True)


def get_secret(name: str) -> str:
    """Read the newest usable secret from either local backend.

    The encrypted fallback is checked first.  A successful keyring save
    deletes the fallback copy, so a fallback that still exists means the keyring
    write failed after an older keyring value was left behind.  The old code
    read that stale keyring value first and silently ignored the newer key the
    user had just saved through the settings dialog.
    """
    try:
        fallback = _fallback_read(name)
    except Exception:
        fallback = ""
    if fallback:
        return fallback

    keyring = _keyring_module()
    if keyring is not None:
        try:
            current = keyring.get_password(SERVICE_NAME, name) or ""
            if current:
                return current
            legacy = keyring.get_password(LEGACY_SERVICE_NAME, name) or ""
            if legacy:
                try:
                    keyring.set_password(SERVICE_NAME, name, legacy)
                except Exception:
                    pass
                return legacy
        except Exception:
            pass
    return ""


def set_secret(name: str, value: str) -> str:
    """Save in the OS keyring or encrypted local fallback.

    The fallback key and ciphertext are separate mode-0600 files under a
    mode-0700 config directory. This fallback does not protect against another
    process running as the same user.
    """
    secret = str(value or "").strip()
    if not secret:
        return ""

    keyring = _keyring_module()
    if keyring is not None:
        try:
            keyring.set_password(SERVICE_NAME, name, secret)
            try:
                _fallback_delete(name)
            except Exception:
                pass
            return "desktop keyring"
        except Exception:
            pass

    try:
        _fallback_write(name, secret)
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        raise SecretStoreError(
            f"Could not save in the keyring or encrypted local fallback: {detail}"
        ) from exc
    return "encrypted local fallback (keyring unavailable)"


def delete_secret(name: str) -> None:
    """Delete a secret from both stores when possible."""
    keyring = _keyring_module()
    if keyring is not None:
        try:
            keyring.delete_password(SERVICE_NAME, name)
        except Exception:
            pass
    try:
        _fallback_delete(name)
    except Exception:
        pass
