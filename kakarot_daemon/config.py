"""Configuration for the standalone Kakarot runtime.

The daemon intentionally reads only small values at startup. Credentials stay
in the operating-system keyring when available; keyring failures use the
encrypted per-user secret store, separate from this configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "kakarot"


def _xdg(variable: str, default: str) -> Path:
    """Read an XDG base directory, falling back to the spec's default."""
    value = os.environ.get(variable, "").strip()
    return Path(value).expanduser() if value else Path.home() / default


def _data_dir() -> Path:
    """State, databases, and the audit log (``~/.local/share/kakarot``)."""
    configured = os.environ.get("KAKAROT_APP_DATA")
    if configured:
        return Path(configured).expanduser()
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME


def _config_dir() -> Path:
    """User configuration (``~/.config/kakarot``)."""
    configured = os.environ.get("KAKAROT_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME


def _runtime_dir() -> Path:
    """Ephemeral sockets and Wayland buffers (``/run/user/$UID/kakarot``)."""
    configured = os.environ.get("KAKAROT_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser()
    return _xdg("XDG_RUNTIME_DIR", ".local/run") / APP_NAME


@dataclass(frozen=True)
class KakarotDaemonConfig:
    data_dir: Path
    model: str
    input_device: str | int | None
    output_device: str | int | None
    tts_model: str = "gemini-3.8-flash-tts"
    # TTS quotas are per model. The lite voice has a separate free-tier bucket
    # and keeps the assistant audible when the primary voice is exhausted.
    tts_fallback_models: tuple[str, ...] = (
        "gemini-3.8-flash-lite-tts",
        "gemini-2.5-flash-preview-tts",
    )
    fallback_models: tuple[str, ...] = (
        # A healthy lighter model first: the 3.8/3.6/3.5 endpoints can sit in
        # a demand spike (503 or a hung request) while this one answers.
        "gemini-3.1-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
    )
    input_queue_chunks: int = 10
    output_buffer_ms: int = 750
    # Local endpointing is the default voice turn-taking mode. It makes the
    # microphone usable without a wake model and without asking the user to
    # click "mute" after every sentence.
    vad_threshold: float = 0.015
    vad_silence_seconds: float = 0.9
    vad_min_speech_seconds: float = 0.30
    # Speaker echo can look exactly like barge-in on a laptop microphone. The
    # monitor is therefore opt-in; when enabled, headphones are strongly
    # recommended.
    barge_in_enabled: bool = False
    # Continuous follow-up listening is opt-in. On laptop speakers the
    # microphone can hear the answer after playback and start an endless
    # self-conversation; requiring one explicit unmute per turn is the safe
    # default until echo cancellation or a wake model is available.
    follow_up_listening: bool = False

    @property
    def database_path(self) -> Path:
        return self.data_dir / "kakarot-reminders.sqlite3"

    @property
    def config_dir(self) -> Path:
        """Per-user configuration; created on first use."""
        return _config_dir()

    @property
    def runtime_dir(self) -> Path:
        """Ephemeral state; owned by the user session, never persisted."""
        return _runtime_dir()

    def ensure_directories(self) -> None:
        """Create the XDG directories the daemon writes to with private modes."""
        for directory in (self.data_dir, self.config_dir, self.runtime_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                # Non-POSIX or read-only test filesystems still get a chance to
                # run; the individual file writers report real failures.
                pass

    @classmethod
    def from_environment(cls) -> KakarotDaemonConfig:
        def device(name: str) -> str | int | None:
            value = os.environ.get(name, "").strip()
            if not value:
                return None
            return int(value) if value.isdigit() else value

        def models(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = os.environ.get(name)
            if raw is None:
                return default
            return tuple(item for item in (p.strip() for p in raw.split(",")) if item)

        def number(name: str, default: float, low: float, high: float) -> float:
            try:
                value = float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default
            return min(high, max(low, value))

        def enabled(name: str, default: bool = False) -> bool:
            raw = os.environ.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            data_dir=_data_dir(),
            # The agent model must be unary-REST capable.  A ``*-live`` model is
            # accepted by the API only over the bidirectional WebSocket transport
            # that AGENTS.md forbids, and it fails every ``generate_content``
            # call with "only supports real-time bidirectional streaming".
            model=os.environ.get("KAKAROT_GEMINI_MODEL", "gemini-3.8-flash").strip(),
            # Tried in order when the model above is unavailable or has retired,
            # so a demand spike degrades latency instead of silence.  All are
            # unary models that accept audio input and function declarations.
            fallback_models=models(
                "KAKAROT_GEMINI_FALLBACK_MODELS",
                (
                    "gemini-3.1-flash-lite",
                    "gemini-3.6-flash",
                    "gemini-3.5-flash",
                ),
            ),
            # TTS runs as a second unary request on the final answer, so the
            # daemon keeps its native-audio speaker path without a live session.
            tts_model=os.environ.get(
                "KAKAROT_GEMINI_TTS_MODEL", "gemini-3.8-flash-tts"
            ).strip(),
            tts_fallback_models=models(
                "KAKAROT_GEMINI_TTS_FALLBACK_MODELS",
                (
                    "gemini-3.8-flash-lite-tts",
                    "gemini-2.5-flash-preview-tts",
                ),
            ),
            input_device=device("KAKAROT_INPUT_DEVICE"),
            output_device=device("KAKAROT_OUTPUT_DEVICE"),
            vad_threshold=number("KAKAROT_VAD_THRESHOLD", 0.015, 0.003, 0.20),
            vad_silence_seconds=number("KAKAROT_VAD_SILENCE_MS", 900, 300, 3000) / 1000.0,
            vad_min_speech_seconds=number("KAKAROT_VAD_MIN_SPEECH_MS", 300, 80, 1500) / 1000.0,
            barge_in_enabled=enabled("KAKAROT_BARGE_IN", False),
            follow_up_listening=enabled("KAKAROT_FOLLOW_UP", False),
        )
