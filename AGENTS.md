# AGENTS.md

## Project Purpose
Kakarot is an autonomous, memory-bounded, and unprivileged local AI desktop agent for Linux Wayland environments. It acts as a stateless REST orchestrator (via Gemini SDK) that delegates tasks to specialized, lightweight subprocesses while strictly maintaining a 192 MiB memory footprint.

## Architecture & State Management
* **Stateless Execution:** Kakarot uses a Stateless REST State Machine (Unary/SSE) via the `google.genai` async client. **Never** implement or propose WebSockets (Multimodal Live API), as they break long-running sandboxed execution and leak memory.
* **Resource Constraints:** The daemon operates under a strict `systemd` cgroup limit of 192 MiB (`MemoryHigh`/`MemoryMax`). Do not introduce memory-heavy libraries (e.g., Playwright, Selenium, Pandas) into the daemon process.
* **Two Processes, Two Interpreters:** The daemon runs under the project venv (no `gi`). The GTK4 front end runs under `/usr/bin/python3`, which owns PyGObject/GTK4/libadwaita. Never import `gi` from a daemon module.
* **No System Tray:** GNOME has no system tray, and depending on a third-party AppIndicator extension is forbidden. The legacy `kakarot_tray.py` is a deprecation shim only; do not reintroduce `pystray` or `StatusNotifierItem`.

## Native GNOME Integration
* **Control Plane:** The daemon owns `io.github.kakarot.Daemon` at `/io/github/kakarot/Daemon` (`kakarot_daemon/dbus_service.py`), exposing `Execute(s,s)->s`, `Status()->s`, a `StateChanged(s)` signal, and a read-only `Version` property. D-Bus is the primary IPC; the UNIX socket is compatibility-only.
* **Shared Routing:** Both transports must delegate to `CommandRouter` in `kakarot_daemon/commands.py` so their behaviour can never drift.
* **Identity:** Every user-visible identifier is reverse-DNS. App ID `io.github.kakarot`, daemon/icon/metainfo ID `io.github.kakarot.Daemon`, launcher `io.github.kakarot.desktop`.
* **Lifecycle:** `kakarot.service` is the single source of truth (`After=`/`WantedBy=graphical-session.target`). D-Bus activation must route through `SystemdService=`, never spawn a competing daemon.
* **Marshalling:** Always pass explicit GVariant tuples for `v`/`a{sv}` replies, and wrap every D-Bus dispatch in a per-message `try` so one bad call cannot kill the bus thread.

## Wayland Compliance
* **Never** invoke `xclip` or `xsel`; use `wl-copy`/`wl-paste` (`kakarot_daemon/clipboard.py`). Preserve and restore the user's existing clipboard around synthetic `Ctrl+C`.
* **Screen access** must go through XDG Desktop Portals (`org.freedesktop.portal.ScreenCast`). Never reach into the compositor or read `/dev/dri` directly.
* **Alerts** must use `org.freedesktop.Notifications` (`kakarot_daemon/notifications.py`), never `notify-send` or custom popup overlays.
* **Paths:** data/audit in `~/.local/share/kakarot/`, configuration and API keys in `~/.config/kakarot/`, runtime state in `$XDG_RUNTIME_DIR/kakarot/`. Resolve them through `kakarot_daemon/config.py` rather than hardcoding `~`.

## Security & Sandboxing Boundaries
* **The Sandbox:** All system interactions must execute through the Bubblewrap (`bwrap`) sandbox (`kakarot_daemon/sandbox.py`). The base filesystem (`/usr`) is strictly read-only, and `$HOME` is masked unless explicitly mounted. Network access (`--unshare-net`) is disabled by default.
* **AST Command Gating:** The execution engine (`kakarot_daemon/security.py`) uses a strict `shlex` AST parser.
  * **Tier-1 (Autonomous):** Read-only commands (e.g., `ls`, `cat`, `df`, `systemctl status`) execute instantly.
  * **Tier-2 (Mutating/HITL):** Commands containing shell operators (`|`, `>`, `$()`) or mutating binaries trigger a Human-In-The-Loop (HITL) prompt (`AWAITING_CONFIRMATION`) via the local UNIX socket.
* **Privacy Firewall:** `kakarot_daemon/scrubber.py` must aggressively redact JWTs, API keys (e.g., `AQ.*`), private keys, and passwords before any data leaves the local machine. Password manager windows (KeepassXC, 1Password) must be blacklisted from screen perception.

## Tool Implementations & Fallbacks
* **Desktop Perception:** Wayland context is gathered via AT-SPI2 (`org.a11y.Bus`). Traversal must be an iterative DFS bounded to 64 depth to prevent recursion errors. Must include a 2.0s async timeout and fallback detection for Electron/Chromium blind spots. Terminal extraction must use `getSelection()` to avoid leaking scrollback history. `pyatspi` is a system binding absent from the venv, so this capability degrades gracefully and must be stubbed in tests.
* **Web Ingestion:** Must use `trafilatura` for low-memory Markdown extraction. Text must be truncated to 4,000 characters and passed through the privacy scrubber.
* **Media Playback:** Uses a Tiered Pipeline to protect daemon memory:
  * **Tier 1:** `systemd-run --user --scope mpv --really-quiet <url>` (Runs in the host desktop slice).
  * **Tier 2:** If `mpv` exits non-zero (DRM/JS block), pivot to `xdg-open` / browser launch.
  * **Audio Routing:** Subprocesses must inject `PIPEWIRE_NODE=echocancel_sink` to prevent microphone feedback.

## Memory & Database Rules
* **Knowledge Engine:** `kakarot.db` uses SQLite FTS5. Queries must be sanitized (`_sanitize_fts5_query`) into double-quoted OR phrases.
* **Concurrency:** Background WAL maintenance must run as `PRAGMA wal_checkpoint(PASSIVE)` on a separate thread with a `busy_timeout` of at least 5000ms to prevent lock contention.

## Audit & Transparency
* **Audit Track Manager:** Every autonomous tool invocation must be appended to `~/.local/share/kakarot/audit_log.md` using ISO 8601 timestamps.
* **Disk I/O:** File appends must be non-blocking via `asyncio.to_thread`. The logger must automatically rotate files at 5 MiB to prevent UI freezing. Screen context text must be redacted from the physical log.

## Development Commands
* Install dependencies: `venv/bin/pip install -r requirements-core.txt` (Ensure `sounddevice`, `trafilatura`, and `jeepney` are present).
* Install the native GNOME stack: `./install_gui.sh` (launcher, scalable icon, AppStream metadata, D-Bus services, systemd unit).
* Start Agent locally: `venv/bin/python3 -m kakarot_daemon.daemon`
* Start the front end locally: `/usr/bin/python3 -m kakarot_gui` (add `--background` to start without showing a window).
* Run the full diagnostic suite: `venv/bin/python3 verify_and_heal.py && venv/bin/python3 verify_and_heal_phase4.py && venv/bin/python3 verify_and_heal_e2e.py`
* Restart systemd service: `systemctl --user restart kakarot.service`
* Inspect the control plane: `gdbus call --session --dest io.github.kakarot.Daemon --object-path /io/github/kakarot/Daemon --method io.github.kakarot.Daemon.Status`
* View Audit Log: `tail -f ~/.local/share/kakarot/audit_log.md`
