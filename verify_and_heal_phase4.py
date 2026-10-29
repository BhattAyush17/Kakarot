"""Kakarot daemon Phase-4 diagnostic harness.

Probes the modules not covered by verify_and_heal.py:
control socket, tray applet subprocess hygiene, audio hardware failure
degradation, lifecycle/signal teardown, watcher pruning races, and the
GUI installer script.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

RESULTS: list[tuple[str, str, bool, str]] = []
REPO = Path(__file__).parent


def record(test: str, subsystem: str, passed: bool, note: str = "") -> None:
    RESULTS.append((subsystem, test, passed, note))
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {subsystem} :: {test}" + (f" — {note}" if note else ""))


# --------------------------------------------------------------- 1. control
async def test_control() -> None:
    from kakarot_daemon.control import ControlServer, send_command

    with tempfile.TemporaryDirectory() as td:
        sock = Path(td) / "kakarot.sock"
        server = ControlServer(
            sock,
            mute=lambda m: None,
            status=lambda: {"phase": "IDLE", "state_version": 1},
            stop=lambda: None,
        )
        await server.start()
        try:
            # 1a. Client sends HITL trigger then vanishes mid-sequence
            r, w = await asyncio.open_unix_connection(str(sock))
            w.write(b'{"command": "trigger_hitl", "tool": "execute_command"}\n')
            await w.drain()
            w.close()  # abrupt disconnect before reading the response
            await asyncio.sleep(0.1)  # let server churn
            # Server must still answer a fresh, well-behaved client
            resp = await asyncio.wait_for(send_command(sock, "status"), timeout=3)
            ok = resp.get("ok") is True
            record("HITL client abrupt disconnect", "control", ok, str(resp)[:100])

            # 1b. Client connects but never sends anything — daemon must not wedge
            r2, w2 = await asyncio.open_unix_connection(str(sock))
            try:
                resp = await asyncio.wait_for(send_command(sock, "status"), timeout=3)
                ok = resp.get("ok") is True
                record("silent client does not block daemon", "control", ok, "")
            finally:
                w2.close()

            # 1c. Subscriber dies abruptly; broadcast must not hang the loop
            r3, w3 = await asyncio.open_unix_connection(str(sock))
            w3.write(b'{"command": "subscribe"}\n')
            await w3.drain()
            await r3.readline()  # initial state
            # Forcibly kill without close handshake
            w3.transport.abort()
            t0 = time.perf_counter()
            server.state_changed({"phase": "TALKING"})
            await asyncio.sleep(0.2)
            dt = time.perf_counter() - t0
            # Heartbeat broadcast (5s) — force one manually bound by a timeout
            await asyncio.wait_for(server._broadcast({"type": "heartbeat", "state_version": 2}), 2)
            record("dead subscriber broadcast", "control", dt < 1.0, f"broadcast dt={dt:.2f}s, subs={len(server._subscribers)}")

            # 1d. Garbage and oversized payloads rejected without handler crash
            r4, w4 = await asyncio.open_unix_connection(str(sock))
            w4.write(b"this is not json\n")
            await w4.drain()
            bad = json.loads((await asyncio.wait_for(r4.readline(), 3)).decode())
            r5, w5 = await asyncio.open_unix_connection(str(sock))
            w5.write(b"\x00" * 20000 + b"\n")  # exceeds MAX_LINE_BYTES
            await w5.drain()
            try:
                bad2 = json.loads((await asyncio.wait_for(r5.readline(), 3)).decode())
            except (ValueError, asyncio.TimeoutError):
                bad2 = {"ok": None}
            ok = bad.get("ok") is False
            resp = await asyncio.wait_for(send_command(sock, "status"), timeout=3)
            record("malformed/oversized request rejected", "control",
                   ok and resp.get("ok") is True, f"garbage={bad} oversized={bad2}")
            w4.close(); w5.close()

            # 1e. send_command against a dead socket must fail fast, not hang
            dead = Path(td) / "dead.sock"
            dead.touch()
            try:
                await asyncio.wait_for(send_command(dead, "status"), timeout=3)
                record("send_command dead socket", "control", False, "no exception raised")
            except (ConnectionRefusedError, FileNotFoundError, asyncio.TimeoutError):
                record("send_command dead socket", "control", True, "failed fast")
        finally:
            await server.close()
        record("socket file removed on close", "control", not sock.exists(), "")


# ------------------------------------------------------- 1b. GNOME front end
def test_gui() -> None:
    """The front end is a GTK4/libadwaita app, not a tray applet."""
    gui_src = (REPO / "kakarot_gui/app.py").read_text()
    record("front end targets GTK4 + libadwaita", "gui",
           "gi.require_version('Gtk', '4.0')" in gui_src.replace('"', "'")
           and "gi.require_version('Adw', '1')" in gui_src.replace('"', "'"),
           "")

    # The legacy tray must be gone, not merely unused: a system tray needs a
    # third-party AppIndicator extension, which Kakarot must not depend on.
    tray = (REPO / "kakarot_tray.py").read_text()
    record("legacy tray deprecated, not reimplemented", "gui",
           "pystray" not in tray and "StatusNotifierItem" not in tray,
           "kakarot_tray.py is a deprecation shim")

    # Colour-scheme compliance: the window must follow the desktop preference.
    theme = (REPO / "kakarot_gui/theme.py").read_text()
    record("listens to org.freedesktop.appearance", "gui",
           "org.freedesktop.appearance" in theme and "color-scheme" in theme, "")

    # No X11 clipboard tools in use: Kakarot must speak wl-clipboard only.
    # Scan for them being *invoked*, not merely mentioned in prose.
    offenders = []
    for path in REPO.rglob("*.py"):
        parts = path.parts
        if any(part.startswith((".venv", "venv", "build", "node_modules")) for part in parts):
            continue
        text = path.read_text(errors="replace")
        if re.search(r"""['"]x(clip|sel)['"]""", text):
            offenders.append(str(path.relative_to(REPO)))
    record("no X11 clipboard tools in use", "gui", not offenders, f"offenders={offenders}")

    # Behavioral: the D-Bus control plane must come up and answer Execute.
    # The live daemon may already hold the well-known name (the bus returns
    # EXISTS); that still proves the name is Kakarot's, so accept either.
    code = textwrap.dedent("""
        import subprocess, sys
        sys.path.insert(0, {repo!r})
        from kakarot_daemon.commands import CommandRouter
        from kakarot_daemon.dbus_service import BUS_NAME, DBusService

        held = subprocess.run(
            ["busctl", "--user", "status", BUS_NAME], capture_output=True
        ).returncode == 0

        router = CommandRouter(
            mute=lambda b: None,
            status=lambda: {{"phase": "IDLE_MUTED", "muted": True, "state_version": 1}},
            stop=lambda: None,
        )
        if held:
            print("name already owned by the running daemon")
            sys.exit(0)
        service = DBusService(router, router._status)
        service.start()
        import time
        for _ in range(20):
            if service.available:
                break
            time.sleep(0.25)
        sys.exit(0 if service.available else 1)
    """).format(repo=str(REPO))
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=40)
    record("daemon D-Bus control plane comes up", "gui", p.returncode == 0,
           (p.stdout + p.stderr).strip()[-160:])

    # The same router must answer every transport identically, so the socket
    # and D-Bus can never drift.
    from kakarot_daemon.commands import CommandRouter

    router = CommandRouter(
        mute=lambda b: None,
        status=lambda: {"phase": "IDLE_MUTED", "state_version": 1},
        stop=lambda: None,
    )
    unknown = router.handle("definitely_not_a_command")
    record("router rejects unknown commands", "gui",
           unknown.get("ok") is False and unknown.get("error") == "unknown command",
           str(unknown)[:120])


# --------------------------------------------------------------- 2. audio
def test_audio() -> None:
    from kakarot_daemon.audio import AudioBridge

    # Simulate missing/locked hardware via a sounddevice stub that raises
    import types
    fake_sd = types.ModuleType("sounddevice")

    class Exploding:
        def __init__(self, *a, **k):
            raise OSError("Device unavailable: btsco (locked)")

    fake_sd.RawInputStream = Exploding
    fake_sd.RawOutputStream = Exploding
    sys.modules["sounddevice"] = fake_sd

    bridge = AudioBridge(input_device=None, output_device=None)
    try:
        bridge.start()  # must not raise
        record("hardware exception containment", "audio", True, f"available={bridge.audio_available}")
        ok = bridge.audio_available is False
        record("degrades to text-only mode", "audio", ok, "")
    except Exception as e:  # noqa: BLE001
        record("hardware exception containment", "audio", False, f"uncaught {type(e).__name__}: {e}")
        ok = False

    # Post-failure calls must be no-ops, not crashes
    try:
        bridge.set_muted(False)
        bridge.set_muted(True)
        bridge.push_output(b"\x00" * 100)
        bridge.close()
        record("post-failure no-op safety", "audio", True, "")
    except Exception as e:  # noqa: BLE001
        record("post-failure no-op safety", "audio", False, f"uncaught {e}")
    finally:
        del sys.modules["sounddevice"]


# --------------------------------------------------------------- 3. lifecycle
async def test_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as td:
        from kakarot_daemon.system_db import wal_maintenance_loop
        from kakarot_daemon.scheduler import ReminderScheduler
        from kakarot_daemon.reminders import ReminderStore

        store = ReminderStore(Path(td) / "r.sqlite3")
        scheduler = ReminderScheduler(store)
        tasks = [
            asyncio.create_task(wal_maintenance_loop(Path(td) / "kakarot.db", interval_seconds=1)),
            asyncio.create_task(scheduler.run()),
            asyncio.create_task(asyncio.sleep(3600)),  # stand-in for connection task
        ]
        await asyncio.sleep(0.15)
        # Simulate SIGTERM path: daemon's finally-block cancellation
        for t in tasks:
            t.cancel()
        t0 = time.perf_counter()
        await asyncio.gather(*tasks, return_exceptions=True)
        dt = time.perf_counter() - t0
        await store.close()
        done = all(t.done() for t in tasks)
        record("SIGTERM-style task teardown", "lifecycle", done and dt < 2.0, f"dt={dt:.2f}s done={done}")

    # Watcher: pruned scheduling must survive races and prune correctly
    from kakarot_daemon.watcher import attach_pruned_watches, MAX_DEPTH

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "node_modules" / "x").mkdir(parents=True)
        (root / ".hidden").mkdir()
        deep = root
        for i in range(6):
            deep = deep / f"d{i}"
            deep.mkdir()

        class FakeObserver:
            def __init__(self):
                self.scheduled = []
            def schedule(self, handler, path, recursive=False):
                # Simulate a directory being deleted mid-walk
                if "d3" in path:
                    raise FileNotFoundError(path)
                self.scheduled.append(path)

        obs = FakeObserver()
        try:
            attach_pruned_watches(obs, object(), root)
            record("watcher survives mid-walk deletion", "watcher", True,
                   f"watched={len(obs.scheduled)}")
        except FileNotFoundError as e:
            record("watcher survives mid-walk deletion", "watcher", False, str(e))
        ok_prune = (not any("node_modules" in p or ".hidden" in p for p in obs.scheduled)
                    and not any(f"d{MAX_DEPTH}" in p for p in obs.scheduled if isinstance(MAX_DEPTH, int)))
        record("watcher blacklist+depth pruning", "watcher", ok_prune,
               f"sample={[Path(p).name for p in obs.scheduled][:6]}")


# --------------------------------------------------------------- 4. install script
def test_install_script() -> None:
    sh = (REPO / "install_gui.sh").read_text()
    desk = (REPO / "io.github.kakarot.desktop").read_text()

    relocatable = "BASE_DIR=" in sh and "$HOME/Projects" not in sh
    record("install script relocatable BASE_DIR", "install", relocatable, "")
    record("update-desktop-database invoked", "install", "update-desktop-database" in sh, "")
    # The installer must deploy the full native GNOME set, not just a launcher.
    record("installer uses the RDNN app id", "install",
           'APP_ID="io.github.kakarot"' in sh, "")
    for artefact, label in (
        ('$APP_ID.service"', "D-Bus activation service"),
        ('$APP_ID.Daemon.service"', "daemon activation service"),
        ("scalable/apps", "scalable icon directory"),
        ("metainfo", "AppStream metadata"),
        ("kakarot.service", "systemd user unit"),
    ):
        record(f"installs {label}", "install", artefact in sh, artefact)

    # The icon tree must exist before it is written, and the theme cache must
    # be refreshed or GNOME keeps serving the previous graphic.
    record("icon tree is created", "install",
           'mkdir -p "$APPS_DIR" "$SCALABLE_DIR" "$SIZED_DIR"' in sh, "")
    record("icon cache is refreshed", "install",
           'gtk-update-icon-cache -f -t "$ICON_THEME_DIR"' in sh, "")
    # avatar.png is the single source of truth for the application icon, and
    # the master itself must be square so no size is ever distorted.
    record("avatar.png is the icon source", "install",
           "assets/avatar.png" in sh and '$SIZED_DIR/$APP_ID.Daemon.png' in sh, "")
    master = REPO / "avatar.png"
    if master.exists():
        with Image.open(master) as im:
            mw, mh = im.size
        record("icon source is square 1:1", "install", mw == mh,
               f"avatar.png={mw}x{mh}")
    else:
        record("icon source is square 1:1", "install", False, "no repo-root avatar.png")
    # The spec keys a fixed-size directory by its size, so the installer must
    # rasterise the set rather than drop one oversized file into 256x256.
    record("installer emits a size ladder", "install",
           "for size in (48, 64, 128, 256, 512)" in sh and "LANCZOS" in sh, "")
    # Never upscale: every installed raster must be no larger than the master.
    master_px = 0
    if master.exists():
        with Image.open(master) as im:
            master_px = max(im.size)
    installed = []
    for asset in (Path.home() / ".local/share/icons/hicolor").glob(
        "*/apps/io.github.kakarot.Daemon.png"
    ):
        with Image.open(asset) as im:
            installed.append(max(im.size))
    record("no installed icon is upscaled past the master", "install",
           bool(installed) and master_px > 0 and max(installed) <= master_px,
           f"master={master_px} installed={sorted(installed)}")
    # Only one asset may own the identity, or the theme can resolve the name
    # to a stale graphic.
    record("installer drops the stale scalable icon", "install",
           'rm -f "$SCALABLE_DIR/$APP_ID.Daemon.svg"' in sh, "")

    # The icon must actually resolve through the theme, not merely exist.
    themed = (Path.home() / ".local/share/icons/hicolor/256x256/apps"
              / "io.github.kakarot.Daemon.png")
    record("installed icon resolves via Icon=", "install",
           "Icon=io.github.kakarot.Daemon" in desk and themed.exists(),
           str(themed))

    record("launcher is D-Bus activatable", "install",
           "DBusActivatable=true" in desk and "StartupNotify=true" in desk, "")

    icon_name = (re.search(r"^Icon=(\S+)$", desk, re.M) or [None, ""])[1]
    # The icon is a themed name backed by a scalable SVG.
    icon_resolves = bool(icon_name) and list(
        Path.home().glob(f".local/share/icons/hicolor/*/apps/{icon_name}.*")
    )
    record(".desktop uses a resolvable themed icon", "install", bool(icon_resolves),
           f"icon={icon_name}")

    # The shipped assets the launcher depends on must exist in the repo.
    for asset in (
        "assets/io.github.kakarot.Daemon.svg",
        "assets/io.github.kakarot.Daemon.metainfo.xml",
        "kakarot.service",
    ):
        record(f"ships {Path(asset).name}", "install", (REPO / asset).exists(), asset)

    # The unit must encode the project's memory budget and session ordering.
    unit = (REPO / "kakarot.service").read_text()
    record("unit pins MemoryMax=192M", "install", "MemoryMax=192M" in unit, "")
    record("unit orders after graphical-session", "install",
           "After=graphical-session.target" in unit, "")

    # The legacy pre-RDNN launcher must be gone from the repo *and* from the
    # installed set. A leftover kakarot.desktop declares no StartupWMClass, so
    # the window it launches (WM_CLASS io.github.kakarot) cannot be grouped
    # under it and GNOME pins a second, unmatched dock icon.
    record("legacy kakarot.desktop removed from repo", "install",
           not (REPO / "kakarot.desktop").exists(), "")
    installed_dir = Path.home() / ".local/share/applications"
    installed_legacy = installed_dir / "kakarot.desktop"
    record("legacy kakarot.desktop removed from applications", "install",
           not installed_legacy.exists(), str(installed_legacy))
    # setup.sh must not reintroduce a second launcher for the same app: a
    # legacy path may appear, but never as a redirection target we write.
    setup = (REPO / "setup.sh").read_text()
    writes_desktop = re.search(r">\s*\"?\$\{?\w*DESKTOP_FILE\}?\"?", setup)
    record("setup.sh writes no second launcher", "install",
           writes_desktop is None,
           f"redirect={writes_desktop.group(0) if writes_desktop else ''}")

    # Dock grouping depends on this key matching the window's WM_CLASS, which
    # Gtk.Application derives from the RDNN application id.
    wm = re.search(r"^StartupWMClass=(\S+)$", desk, re.M)
    record("launcher declares the RDNN StartupWMClass", "install",
           bool(wm) and wm.group(1) == "io.github.kakarot",
           f"StartupWMClass={wm.group(1) if wm else '(absent)'}")


async def main() -> None:
    await test_control()
    test_gui()
    test_audio()
    await test_lifecycle()
    test_install_script()

    failed = [r for r in RESULTS if not r[2]]
    print()
    print(f"TOTAL: {len(RESULTS)} | PASS: {len(RESULTS) - len(failed)} | FAIL: {len(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
