"""Small, provider-neutral readiness state for health endpoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock


@dataclass
class Capability:
    state: str = "loading"
    detail: str = ""


_lock = Lock()
_capabilities: dict[str, Capability] = {
    "backend": Capability(),
    "speech": Capability(),
    "memory": Capability("unavailable", "not implemented"),
    "notifications": Capability("unavailable", "not implemented"),
}


def set_capability(name: str, state: str, detail: str = "") -> None:
    if state not in {"loading", "ready", "degraded", "unavailable"}:
        raise ValueError(f"Unsupported capability state: {state}")
    with _lock:
        _capabilities[name] = Capability(state, detail)


def snapshot() -> dict[str, dict[str, str]]:
    with _lock:
        return {name: asdict(value) for name, value in _capabilities.items()}


def locally_ready() -> bool:
    return snapshot()["backend"]["state"] == "ready"
