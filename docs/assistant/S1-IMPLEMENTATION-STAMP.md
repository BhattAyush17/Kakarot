# S1 lightweight foundation stamp

Stage: S1

Status: IN_PROGRESS

Implementation commit: uncommitted working tree

Environment: Fedora 44 Wayland; CPython 3.12.13 isolated `.venv-light`; `google-genai` 2.25.0

Requirements addressed: R01, R03, R14, R15, R16, R19, R21, R28

Implemented:

- Default lightweight requirements exclude Torch, WhisperX, Kokoro, OpenAI, and Anthropic packages.
- Legacy provider adapters and local speech/model preload paths load only when selected.
- Gemini configuration and keyring secret support use config schema version 3.
- Provider-neutral capability readiness replaces Whisper-only readiness for new paths.
- Session injection, control, playback, and sprite-loader queues/buffers are bounded.
- Local WebSocket authentication uses a header, keeping the token out of access-log URLs.
- Linux virtual-environment paths are supported.
- Built-in lightweight sprite assets and on-demand sheet loading reduce persona memory.
- SQLite/FTS5 long-term memory has also begun under S3, with private-memory exclusion from Gemini tool results.

Checks:

- Seven focused tests pass for lightweight imports, Gemini configuration, audio resampling, bounded queues, persistence, private filtering, deletion, and validation.
- Changed/new core modules compile under Python 3.12.
- Focused Ruff correctness/import checks pass.
- Combined cloud-disabled offscreen sample: 227,930,112 bytes peak RSS; 197,423,104 bytes peak PSS.
- No local ML or legacy provider module is imported by the lightweight backend path.

Open gates:

- Live Wayland persona/audio/shortcut test.
- Live Gemini voice, interruption, reconnect, and 30-minute soak.
- Full process accounting including launcher and a real settings workflow.
- Dependency constraints/lock and migration fixtures for existing user configurations.
- Broader regression suite for legacy profiles and Windows.

Rollback: requirements and config migrations need a release backup/rollback exercise before verification.
