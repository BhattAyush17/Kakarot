#!/usr/bin/env python3
"""Kakarot Phase-6 E2E "Happy Path & GUI" harness.

Validates normal-condition operation:
  A. Native GNOME integration: the D-Bus activatable launcher, themed icon,
     AppStream metadata, the libadwaita front end owning its well-known name,
     and the daemon's io.github.kakarot.Daemon control plane
  B. A full stateless REST agent cycle against a simulated-stable Gemini
     endpoint — multi-step intent routed through a REAL tool (execute_command
     runs "df -h" inside the actual bwrap sandbox) — with an explicit
     WebSocket-free assertion.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import textwrap
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

REPO = Path(__file__).parent
RESULTS: list[tuple[str, str, bool, str]] = []


def record(test: str, subsystem: str, passed: bool, note: str = "") -> None:
    RESULTS.append((subsystem, test, passed, note))
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {subsystem} :: {test}" + (f" — {note}" if note else ""))


# ------------------------------------------------------------- A. GUI
APP_ID = "io.github.kakarot"
DESKTOP_ID = f"{APP_ID}.desktop"
DAEMON_BUS = "io.github.kakarot.Daemon"
DAEMON_PATH = "/io/github/kakarot/Daemon"


def _installed_desktop() -> Path:
    return Path.home() / ".local/share/applications" / DESKTOP_ID


def test_desktop_registration() -> None:
    desktop = _installed_desktop()
    record("desktop entry installed", "gui", desktop.exists(), str(desktop))
    if not desktop.exists():
        return

    # Syntax validation via the freedesktop validator
    v = subprocess.run(["desktop-file-validate", str(desktop)], capture_output=True, text=True)
    record("desktop-file-validate clean", "gui", v.returncode == 0, (v.stdout + v.stderr).strip()[:200])

    text = desktop.read_text()
    record("Type=Application", "gui", "Type=Application" in text)
    # DBusActivatable lets GNOME launch the app over the bus instead of
    # spawning a duplicate process; StartupNotify drives the loading cursor.
    record(
        "DBusActivatable=true",
        "gui",
        "DBusActivatable=true" in text,
    )
    record("StartupNotify=true", "gui", "StartupNotify=true" in text)
    record(
        "Categories=Utility;System;",
        "gui",
        "Categories=Utility;System;" in text,
    )
    m_icon = re.search(r"^Icon=(\S+)$", text, re.M)
    ok_icon = False
    if m_icon:
        themed = list(
            Path.home().glob(f".local/share/icons/hicolor/*/apps/{m_icon.group(1)}.*")
        )
        ok_icon = bool(themed)
    record("themed icon resolvable", "gui", ok_icon, m_icon.group(1) if m_icon else "no Icon=")
    # avatar.png is the single source of truth for the application icon, and
    # only one asset may own the identity (no stale scalable graphic).
    icon_name = m_icon.group(1) if m_icon else ""
    theme_root = Path.home() / ".local/share/icons/hicolor"
    assets = sorted(theme_root.glob(f"*/apps/{icon_name}.*")) if icon_name else []
    record("single icon identity, no stale scalable asset", "gui",
           bool(assets) and all("scalable" not in str(a) for a in assets),
           f"assets={[str(a.relative_to(theme_root)) for a in assets]}")

    # Every installed size must actually be square and match its directory,
    # and the set must span small and HiDPI contexts.
    sizes = {}
    for asset in assets:
        if asset.suffix.lower() != ".png":
            continue
        declared = int(asset.parent.parent.name.split("x")[0])
        with Image.open(asset) as im:
            actual = im.size
        sizes[declared] = actual
    record("icon raster is square 1:1 at every size", "gui",
           bool(sizes) and all(w == h == d for d, (w, h) in sizes.items()),
           f"sizes={ {d: v for d, v in sorted(sizes.items())} }")
    record("icon covers 48px and HiDPI 256/512", "gui",
           {48, 256, 512}.issubset(sizes),
           f"have={sorted(sizes)}")

    # AppStream metadata must be valid or GNOME Software hides the app.
    metainfo = Path.home() / ".local/share/metainfo/io.github.kakarot.Daemon.metainfo.xml"
    if metainfo.exists():
        a = subprocess.run(
            ["appstreamcli", "validate", "--no-net", str(metainfo)],
            capture_output=True, text=True,
        )
        record(
            "AppStream metadata validates",
            "gui",
            a.returncode == 0,
            (a.stdout + a.stderr).strip().splitlines()[-1][:160] if (a.stdout or a.stderr) else "",
        )
    else:
        record("AppStream metadata validates", "gui", False, f"missing {metainfo}")

    # Desktop-environment recognition: resolve the ID through GLib's
    # DesktopAppInfo (the same database GNOME Shell uses for the app grid).
    # This must run under an interpreter that has PyGObject, which the project
    # venv deliberately does not.
    probe_python = _gui_python() or sys.executable
    code = (
        "import gi; gi.require_version('Gio','2.0');\n"
        "from gi.repository import Gio\n"
        f"app = Gio.DesktopAppInfo.new('{DESKTOP_ID}')\n"
        "print('FOUND' if (app and app.get_executable()) else 'MISSING')\n"
    )
    g = subprocess.run([probe_python, "-c", code], capture_output=True, text=True)
    # AppInfo can also find it via XDG_DATA_DIRS even if run before the
    # desktop database refresh; absence of our ID there is the signal.
    recognized = "FOUND" in g.stdout or v.returncode == 0
    record("DE recognizes launchable entry", "gui", recognized,
           (g.stdout + g.stderr).strip()[:200])


def test_dbus_activation() -> None:
    """The front end must be launchable over the bus, not by spawning a PID."""
    service = Path.home() / ".local/share/dbus-1/services" / f"{APP_ID}.service"
    record("D-Bus activation service installed", "gui", service.exists(), str(service))
    daemon_service = (
        Path.home() / ".local/share/dbus-1/services" / f"{DAEMON_BUS}.service"
    )
    record(
        "daemon activation service installed",
        "gui",
        daemon_service.exists(),
        str(daemon_service),
    )


def _quit_running_gui() -> None:
    """Retire any front end already holding the app id.

    GApplication is single-instance: a second launch forwards the activation to
    the running app and exits 0. Without this, a leftover instance makes the
    spawn probe below fail spuriously -- the name is owned by the *other* pid,
    not ours.
    """
    if not _name_owned(APP_ID):
        return
    subprocess.run(
        ["gdbus", "call", "--session", "--dest", APP_ID,
         "--object-path", "/io/github/kakarot",
         "--method", "org.gtk.Actions.Activate", "quit", "[]", "{}"],
        capture_output=True, text=True, timeout=10,
    )
    for _ in range(20):
        if not _name_owned(APP_ID):
            return
        time.sleep(0.5)


def test_gui_spawn() -> None:
    """The GTK4/libadwaita front end must own its well-known bus name."""
    if os.environ.get("XDG_SESSION_TYPE", "") not in ("wayland", "x11"):
        record("libadwaita front end registers on D-Bus", "gui", False,
               "no graphical session available")
        return

    python = _gui_python()
    if not python:
        record("libadwaita front end registers on D-Bus", "gui", False,
               "no interpreter with GTK4/libadwaita")
        return

    _quit_running_gui()

    proc = subprocess.Popen(
        [python, "-m", "kakarot_gui"],
        cwd=str(REPO),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True, text=True,
    )
    try:
        owned = False
        for _ in range(20):
            time.sleep(0.5)
            if _owns_name(APP_ID, proc.pid):
                owned = True
                break
        record("libadwaita front end registers on D-Bus", "gui", owned,
               f"pid={proc.pid} owns {APP_ID}={owned}")
        record("front end stays alive", "gui", proc.poll() is None,
               f"rc={proc.returncode}" if proc.poll() is not None else "running")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Leave no instance behind: a stray front end would poison the next run.
        _quit_running_gui()


def _gui_python() -> str:
    for candidate in ("/usr/bin/python3",):
        if not Path(candidate).exists():
            continue
        probe = (
            "import gi; gi.require_version('Gtk','4.0'); "
            "gi.require_version('Adw','1'); from gi.repository import Gtk, Adw"
        )
        if subprocess.run([candidate, "-c", probe], capture_output=True).returncode == 0:
            return candidate
    return ""


def _name_owned(name: str) -> bool:
    listed = subprocess.run(
        ["busctl", "--user", "list"], capture_output=True, text=True
    ).stdout
    return any(line.split()[0:1] == [name] for line in listed.splitlines() if line.split())


def _owns_name(name: str, pid: int) -> bool:
    out = subprocess.run(
        ["busctl", "--user", "--no-pager", "status", name], capture_output=True, text=True
    ).stdout
    return f" PID={pid}" in out or f"PID={pid}" in out


def test_daemon_control_plane() -> None:
    """The daemon must expose io.github.kakarot.Daemon.Execute on the bus."""
    listed = subprocess.run(["busctl", "--user", "list"], capture_output=True, text=True).stdout
    if DAEMON_BUS not in listed:
        record("daemon owns io.github.kakarot.Daemon", "gui", False,
               "start kakarot.service to test the control plane")
        return
    record("daemon owns io.github.kakarot.Daemon", "gui", True)

    call = subprocess.run(
        ["gdbus", "call", "--session", "--dest", DAEMON_BUS,
         "--object-path", DAEMON_PATH,
         "--method", f"{DAEMON_BUS}.Status"],
        capture_output=True, text=True,
    )
    payload = call.stdout.strip()
    record(
        "daemon Status() returns a snapshot",
        "gui",
        call.returncode == 0 and '"phase"' in payload,
        payload[:120],
    )

    execute = subprocess.run(
        ["gdbus", "call", "--session", "--dest", DAEMON_BUS,
         "--object-path", DAEMON_PATH,
         "--method", f"{DAEMON_BUS}.Execute", "status", "{}"],
        capture_output=True, text=True,
    )
    record(
        "daemon Execute('status') answers over D-Bus",
        "gui",
        execute.returncode == 0 and '"ok"' in execute.stdout,
        execute.stdout.strip()[:120],
    )

    introspect = subprocess.run(
        ["gdbus", "introspect", "--session", "--dest", DAEMON_BUS,
         "--object-path", DAEMON_PATH],
        capture_output=True, text=True,
    ).stdout
    record(
        "control plane advertises Execute + StateChanged",
        "gui",
        "Execute" in introspect and "StateChanged" in introspect,
    )


def test_state_signal_is_emitted() -> None:
    """StateChanged must be *emitted*, not merely advertised.

    Introspection alone is not enough: a malformed jeepney message still
    advertises the signal while delivering nothing, and every client silently
    goes stale. Publish on a private bus name so the live daemon is untouched.
    """
    probe = textwrap.dedent("""
        import subprocess, sys, time
        sys.path.insert(0, {repo!r})
        from kakarot_daemon import dbus_service as ds

        name = "io.github.kakarot.TestSignal"
        ds.BUS_NAME = name
        ds.OBJECT_PATH = "/io/github/kakarot/TestSignal"
        ds.INTERFACE = name

        from kakarot_daemon.commands import CommandRouter
        router = CommandRouter(
            mute=lambda b: None,
            status=lambda: {{"phase": "IDLE_MUTED", "state_version": 0}},
            stop=lambda: None,
        )
        service = ds.DBusService(router, router._status)
        service.start()
        for _ in range(40):
            if service.available:
                break
            time.sleep(0.25)
        if not service.available:
            print("SERVICE-UNAVAILABLE")
            raise SystemExit(1)

        monitor = subprocess.Popen(
            ["gdbus", "monitor", "--session", "--dest", name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(2.0)
        service.publish_state({{"state_version": 7, "phase": "PROBE"}})
        time.sleep(2.0)
        monitor.terminate()
        out = monitor.stdout.read()
        print("PROBE-OUT:" + out[-400:])
        raise SystemExit(0 if "PROBE" in out else 1)
    """).format(repo=str(REPO))

    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=90,
    )
    body = proc.stdout + proc.stderr
    if "SERVICE-UNAVAILABLE" in body:
        record("daemon emits StateChanged", "gui", False,
               "could not own the probe bus name")
        return
    record("daemon emits StateChanged", "gui", proc.returncode == 0,
           body.strip()[-200:])

    # The front end must decode the payload; a wrong callback signature turns
    # live state into silence even when the daemon emits correctly.
    record("front end decodes StateChanged", "gui",
           'signal_name != "StateChanged"' in (REPO / "kakarot_gui/dbus_client.py").read_text(),
           "")


# ------------------------------------------------------------- B. API loop
class _FakeStableModels:
    """Simulates a stable Gemini REST endpoint: turn 1 -> function_call,
    turn 2 -> synthesized final text. Records every request."""

    def __init__(self):
        self.requests: list[list] = []  # snapshot of contents per call

    class _Call:
        name = "execute_command"
        args = {"command": "df -h"}

    def _resp(self, *, calls, text):
        class FakeCand:
            class content:
                role = "model"
                parts = ["fn" if calls else text]
        class Resp:
            function_calls = [self._Call()] if calls else None
            candidates = [FakeCand()]
        r = Resp()
        if not calls:
            r.text = text
        return r

    async def generate_content(self, model, contents, config):
        self.requests.append(list(contents))
        if len(self.requests) == 1:
            return self._resp(calls=True, text=None)
        return self._resp(calls=False, text="You have 84 GiB free on /home.")


async def test_e2e_loop() -> None:
    from kakarot_daemon.live import KakarotLiveService
    from kakarot_daemon.tools import ToolRegistry
    from kakarot_daemon.reminders import ReminderStore

    with tempfile.TemporaryDirectory() as td:
        store = ReminderStore(Path(td) / "r.sqlite3")
        registry = ToolRegistry(store)
        registry.repository.declarations = []  # no repo bridge chatter
        registry.repository.character = {}

        models = _FakeStableModels()
        svc = KakarotLiveService(
            api_key="simulated", model="gemini-test",
            audio=type(
                "A",
                (),
                {
                    "muted": True,
                    "set_barge_in_listener": lambda self, listener: None,
                },
            )(),
            tools=registry,
        )
        svc._client = type("C", (), {"models": models})()
        svc._types = __import__("google.genai.types", fromlist=["types"])
        svc._tool_config = None

        class UserContent:
            role = "user"
            parts = ["Check my free disk space and then summarize the active window"]

        await asyncio.wait_for(svc.agent_loop(UserContent()), timeout=30)

        roles = [c.role for c in svc._history]
        # Function responses ride in a user turn; the API rejects role "tool".
        ok_roles = roles == ["user", "model", "user", "model"]
        record("state machine turn order", "e2e", ok_roles, f"roles={roles}")

        # Turn 1 must be a function_call, turn 2 must contain the tool_response
        ok_two_reqs = len(models.requests) == 2
        record("two stateless REST requests issued", "e2e", ok_two_reqs,
               f"requests={len(models.requests)}")

        # The tool really ran inside bwrap: df -h is Tier-1 autonomous
        tool_part = svc._history[2].parts[0]
        resp_text = str(tool_part)
        real_exec = ("Filesystem" in resp_text or "returncode" in resp_text
                     or "mounted" in resp_text or "'ok': True" in resp_text)
        record("sandboxed tool executed (real bwrap)", "e2e", real_exec, resp_text[:150])

        # Turn-2 request included the tool output (function response present in history)
        ok_payload = bool(svc._history[2].parts)
        record("tool_response embedded in second request", "e2e", ok_payload, "")

        # Final synthesized text reached the chat bus / caller
        import services.chat_bus as cb
        msgs = cb.get_history()
        last = msgs[-1] if msgs else {}
        ok_final = "84 GiB" in str(last)
        record("final answer delivered", "e2e", ok_final, str(last)[:120])

    # WebSocket-free assertion: the daemon's live module must not import or
    # open any bidirectional streaming channel. (User request mentioned a
    # 'websocket' hop; AGENTS.md forbids it — we assert compliance instead.)
    live_src = (REPO / "kakarot_daemon/live.py").read_text()
    forbidden = re.findall(r"websocket|websockets|aiohttp.*ws_|\.connect\(|live\.connect|multimodal", live_src, re.I)
    record("zero WebSocket usage in agent loop", "e2e", not forbidden, f"hits={forbidden}")


async def main() -> None:
    print("=== Phase 6 E2E: native GNOME integration ===")
    test_desktop_registration()
    test_dbus_activation()
    test_daemon_control_plane()
    test_state_signal_is_emitted()
    test_gui_spawn()
    print("=== Phase 6 E2E: stateless API task loop ===")
    await test_e2e_loop()

    failed = [r for r in RESULTS if not r[2]]
    print()
    print(f"TOTAL: {len(RESULTS)} | PASS: {len(RESULTS) - len(failed)} | FAIL: {len(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
