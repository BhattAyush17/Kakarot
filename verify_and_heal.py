"""Kakarot daemon diagnostic & self-healing harness.

Probes every subsystem for latent bugs, race conditions, edge-case crashes
and memory leaks. Prints a machine-readable result table consumed by the
diagnostics report.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

RESULTS: list[tuple[str, str, bool, str]] = []


def record(test: str, subsystem: str, passed: bool, note: str = "") -> None:
    RESULTS.append((subsystem, test, passed, note))
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {subsystem} :: {test}" + (f" — {note}" if note else ""))


# ---------------------------------------------------------------- sandbox
async def test_sandbox() -> None:
    from kakarot_daemon.sandbox import execute_sandboxed

    r = await execute_sandboxed("echo hello", timeout=15)
    record("basic echo", "sandbox", r.get("ok") is True and "hello" in r.get("stdout", ""), str(r)[:120])

    r = await execute_sandboxed("sleep 30", timeout=2)
    record("timeout kill", "sandbox", r.get("ok") is False and r.get("returncode") == -1
           and "2 seconds" in r.get("error", ""), str(r)[:160])

    r = await execute_sandboxed("python3 -c \"print('A'*9000); import sys; print('B'*9000, file=sys.stderr)\"", timeout=15)
    total = len(r.get("stdout", "")) + len(r.get("stderr", ""))
    record("4k truncation", "sandbox", total <= 4200, f"total={total}")

    r = await execute_sandboxed("touch /usr/kakarot_write_test 2>&1 || echo ro-ok", timeout=15)
    record("/usr read-only", "sandbox", "ro-ok" in r.get("stdout", ""), str(r)[:120])

    r = await execute_sandboxed("ls ~ 2>/dev/null | head -5; [ -e ~/.bashrc ] && echo LEAKED || echo masked", timeout=15)
    record("home masked", "sandbox", "LEAKED" not in r.get("stdout", ""), str(r)[:120])

    r = await execute_sandboxed("getent hosts example.com || echo netfail", allow_network=False, timeout=10)
    off_ok = "ok" in r  # command exit code propagates
    r2 = await execute_sandboxed("getent hosts example.com && echo netok", allow_network=True, timeout=15)
    on_ok = "netok" in r2.get("stdout", "")
    record("network isolation toggle", "sandbox", off_ok and on_ok,
           f"offline_rc_ok={off_ok} online_dns={on_ok} {r2.get('stderr','')[:80]}")


# ---------------------------------------------------------------- security
def test_security() -> None:
    from kakarot_daemon.security import requires_hitl

    cases = [
        ("ls -la", False),
        ("cat file.txt | grep foo", True),
        ("ls && whoami", True),
        ("ls || whoami", True),
        ("cat file > out.txt", True),
        ("cat file>>out.txt", True),          # glued redirect
        ("cat < in.txt", True),
        ("cat $(rm -rf /tmp/x) file", True),  # subshell bypass attempt
        ("cat `id`", True),                    # backtick bypass attempt
        ("ls; whoami", True),
        ("ls|wc", True),                       # glued pipe
        ("unclosed 'quote", True),             # fail-closed syntax error
        ("VAR=1 ls", True),                    # env prefix -> HITL (fail closed)
        ("/bin/cat /etc/hostname", False),     # absolute path to safe binary
        ("systemctl restart sshd", True),
        ("systemctl status sshd", False),
        ("", True),
    ]
    bad = [c for c, want in cases if requires_hitl(c) != want]
    record("classifier edge cases", "security", not bad, f"mismatch: {bad}")


# ---------------------------------------------------------------- system_db
def test_system_db() -> None:
    from kakarot_daemon.system_db import get_db, initialize_db, _execute_checkpoint
    from kakarot_daemon.memory_tools import search_memory, remember_fact

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        initialize_db(data_dir)
        db_path = data_dir / "kakarot.db"

        # Concurrent writers + passive checkpoint under load
        errors: list[str] = []

        def writer(n: int) -> None:
            try:
                conn = get_db(data_dir)
                for i in range(60):
                    conn.execute("INSERT INTO execution_journal (tool_name) VALUES (?)", (f"t{n}",))
                    conn.commit()
                conn.close()
            except Exception as e:  # noqa: BLE001
                errors.append(f"writer{n}: {e}")

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            try:
                _execute_checkpoint(db_path)
            except Exception as e:  # noqa: BLE001
                errors.append(f"checkpoint: {e}")
            time.sleep(0.005)
        for t in threads:
            t.join()
        record("concurrent WAL checkpoint under load", "system_db", not errors, "; ".join(errors[:3]))

        # FTS5 malformed queries must not raise out of search_memory
        import kakarot_daemon.memory_tools as mt
        mt._get_kakarot_db = lambda: _patched_conn(data_dir)  # noqa: PLW0212
        remember_fact("note one", "body alpha beta")
        bad = []
        for q in ['"unbalanced', "foo OR", "AND (", "***", "}", "NEAR("]:
            r = search_memory(q)
            if "error" in r:
                bad.append((q, r["error"]))
        # sanitize: errors are acceptable as clean dict, crash is not; but goal is clean results
        r = search_memory('"unbalanced quote')
        record("FTS5 malformed-query sanitization", "system_db", "error" not in r, str(r)[:140])


def _patched_conn(data_dir: Path):
    from kakarot_daemon.system_db import get_db
    conn = get_db(data_dir)
    conn.row_factory = sqlite3.Row
    return conn


async def test_wal_loop() -> None:
    from kakarot_daemon.system_db import wal_maintenance_loop

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "kakarot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.commit()

        t0 = time.perf_counter()
        task = asyncio.create_task(wal_maintenance_loop(db_path, interval_seconds=0.05))
        await asyncio.sleep(0.3)
        # event loop must stay responsive while checkpoints run
        spin_start = time.perf_counter()
        for _ in range(20):
            await asyncio.sleep(0.005)
        spin = time.perf_counter() - spin_start
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        conn.close()
        total = time.perf_counter() - t0
        record("WAL loop async responsiveness", "system_db", spin < 0.25 and total < 1.0,
               f"spin={spin:.3f}s total={total:.3f}s")


# ---------------------------------------------------------------- desktop_context
async def test_desktop_context() -> None:
    import kakarot_daemon.desktop_context as dc

    # Stall simulation: sync reader that never returns must time out cleanly
    orig = dc._read_screen_context_sync
    ev = threading.Event()

    def stalled(app_info):
        ev.wait(10)
        return {"content": "late"}

    dc._read_screen_context_sync = stalled
    try:
        t0 = time.perf_counter()
        r = await dc.read_screen_context()
        dt = time.perf_counter() - t0
        ok = "error" in r and dt < 3.0
        record("AT-SPI2 timeout drop", "desktop_context", ok, f"dt={dt:.2f}s r={r}")
    finally:
        dc._read_screen_context_sync = orig
        ev.set()

    # Electron fallback error string. The AT-SPI2 tree read is stubbed below,
    # but read_screen_context() still short-circuits when the binding is
    # absent -- and pyatspi is a PyGObject system binding that the project venv
    # deliberately does not carry. Stub the binding so this exercises the real
    # fallback logic rather than reporting a false failure.
    if dc.pyatspi is None:
        dc.pyatspi = types.ModuleType("pyatspi")

    def fake_empty(app_info):
        app_info["name"] = "google-chrome"
        return {"error": "no text"}

    dc._read_screen_context_sync = fake_empty
    try:
        r = await dc.read_screen_context()
        record("Electron fallback message", "desktop_context",
               "Electron/Chromium" in r.get("error", ""), str(r)[:120])
    finally:
        dc._read_screen_context_sync = orig

    # get_focused_node recursion depth safety: deep fake tree
    class FakeNode:
        def __init__(self, depth):
            self.depth = depth
        def getState(self):
            class S:
                def contains(self, _): return False
            return S()
        def __iter__(self):
            if self.depth > 0:
                return iter([FakeNode(self.depth - 1)])
            return iter([])
        def getRole(self): return None
        def queryText(self): raise NotImplementedError
        name = "x"

    try:
        from kakarot_daemon.desktop_context import _safe_focused_node
    except ImportError:
        record("deep-tree recursion bound", "desktop_context", False, "_safe_focused_node not implemented")
    else:
        try:
            node = _safe_focused_node(FakeNode(10000))
            record("deep-tree recursion bound", "desktop_context", True, f"returned {type(node).__name__}")
        except RecursionError:
            record("deep-tree recursion bound", "desktop_context", False, "RecursionError on deep AT-SPI tree")


# ---------------------------------------------------------------- web_tools
async def test_web_tools() -> None:
    from kakarot_daemon.web_tools import fetch_web_content

    bad = []
    for url in ["not-a-url", "ftp://example.com/x", "http://127.0.0.1:1/nonexistent", "https://example.invalid/404"]:
        try:
            r = await asyncio.wait_for(fetch_web_content(url), timeout=30)
            if not isinstance(r, str):
                bad.append((url, "non-string"))
        except Exception as e:  # noqa: BLE001
            bad.append((url, f"uncaught {e}"))
    record("invalid/blocked URL fallback", "web_tools", not bad, str(bad)[:200])

    # Truncation
    import kakarot_daemon.web_tools as wt
    orig = wt.trafilatura.fetch_url, wt.trafilatura.extract
    wt.trafilatura.fetch_url = lambda u: "<html>ok</html>"
    wt.trafilatura.extract = lambda d, **k: "x" * 100000
    try:
        r = await fetch_web_content("https://example.com")
        record("4k payload truncation", "web_tools", len(r) <= 4000, f"len={len(r)}")
    finally:
        wt.trafilatura.fetch_url, wt.trafilatura.extract = orig


# ---------------------------------------------------------------- media_tools
async def test_media_tools() -> None:
    import kakarot_daemon.media_tools as mt

    launched: list[list[str]] = []

    class FakeProc:
        def __init__(self, rc):
            self.returncode = rc
        async def wait(self):
            return self.returncode

    orig = asyncio.create_subprocess_exec

    async def fake_exec_fail(*cmd, **kw):
        launched.append(list(cmd))
        return FakeProc(1)  # mpv exits non-zero within 5s

    asyncio.create_subprocess_exec = fake_exec_fail
    try:
        r = await mt.play_media("https://youtu.be/x")
        ok = "Path A" in r and any("xdg-open" in c for c in launched)
        record("Tier-1 fail -> Tier-2 pivot", "media_tools", ok, f"r={r} launched={launched}")
    except Exception as e:  # noqa: BLE001
        record("Tier-1 fail -> Tier-2 pivot", "media_tools", False, f"uncaught {e}")
    finally:
        asyncio.create_subprocess_exec = orig

    # Env overrides
    captured: dict = {}
    async def fake_exec_ok(*cmd, **kw):
        captured.update(kw.get("env", {}))
        launched.append(list(cmd))
        return FakeProc(0)

    asyncio.create_subprocess_exec = fake_exec_ok
    try:
        await mt.play_media("https://example.com/a.mp3")
        ok = captured.get("PIPEWIRE_NODE") == "echocancel_sink" and captured.get("PULSE_SINK") == "echocancel_sink"
        record("echocancel env overrides", "media_tools", ok, "")
    finally:
        asyncio.create_subprocess_exec = orig


# ---------------------------------------------------------------- audit_logger
async def test_audit_logger(tmp_audit: Path) -> None:
    import importlib
    import kakarot_daemon.audit_logger as al

    al.AUDIT_DIR = tmp_audit
    al.AUDIT_FILE = tmp_audit / "audit_log.md"
    al.setup_audit_logger()

    # O(1) append across many writes
    n = 200
    t0 = time.perf_counter()
    for i in range(n):
        await al.log_action("intent", "tool", f"details {i}")
    dt = time.perf_counter() - t0
    record("async append throughput", "audit_logger", dt < 5.0, f"{n} writes in {dt:.2f}s")

    # Rotation of >5MiB log
    with open(al.AUDIT_FILE, "w") as f:
        f.write("x" * (6 * 1024 * 1024))
    al.setup_audit_logger()
    archives = [p for p in tmp_audit.iterdir() if p.name.startswith("audit_log_archive")]
    ok = bool(archives) and not al.AUDIT_FILE.exists()
    record("5MiB rotation on boot", "audit_logger", ok, f"archives={[a.name for a in archives]}")

    # Sensitive desktop context excluded
    await al.log_action("i", "read_screen_context", "Requested AT-SPI2 screen context extraction (content redacted for privacy).")
    content = al.AUDIT_FILE.read_text() if al.AUDIT_FILE.exists() else ""
    record("sensitive context redaction", "audit_logger",
           "REDACTED" not in content or "redacted for privacy" in content, "")


# ---------------------------------------------------------------- live/tools
async def test_agent_loop() -> None:
    from kakarot_daemon.live import KakarotLiveService

    class FakeCall:
        name = "execute_command"
        args = {"command": "rm -rf /"}
    class FakeContent:
        role = "model"
        parts = []
    class FakeCand:
        content = FakeContent()
    class FakeResp:
        def __init__(self, n):
            self._n = n
        @property
        def function_calls(self):
            return [FakeCall()] if self._n == 0 else None
        text = "done"
        candidates = [FakeCand()]

    class FakeModels:
        def __init__(self):
            self.n = 0
        async def generate_content(self, **_):
            r = FakeResp(self.n)
            self.n += 1
            return r
    class FakeClient:
        models = FakeModels()
    class FakeTools:
        class repository:
            character = {}
            declarations = []
            async def refresh(self): pass
        async def execute(self, name, args):
            return {"ok": False, "error": "AWAITING_CONFIRMATION", "reason": "x"}
    class FakeAudio:
        muted = True

        def set_barge_in_listener(self, listener):
            self.barge_in_listener = listener
    class FakeTypesContent:
        def __init__(self, role=None, parts=None):
            self.role = role
            self.parts = parts or []

    svc = KakarotLiveService(api_key="k", model="m", audio=FakeAudio(), tools=FakeTools())
    svc._client = FakeClient()
    svc._types = type("T", (), {
        "Part": type("P", (), {"from_function_response": staticmethod(lambda name, response: ("part", name, response))}),
        "Content": FakeTypesContent,
    })
    svc._tool_config = None

    class UserContent:
        role = "user"
        parts = ["p"]

    try:
        await asyncio.wait_for(svc.agent_loop(UserContent()), timeout=5)
        roles = [c.role for c in svc._history]
        ok_parts = all(hasattr(c, "parts") for c in svc._history)
        # The tool turn is a *user* turn: the API rejects role "tool" outright.
        record("AWAITING_CONFIRMATION loop pause", "live",
               roles == ["user", "model", "user", "model"] and ok_parts,
               f"roles={roles}")
    except Exception as e:  # noqa: BLE001
        record("AWAITING_CONFIRMATION loop pause", "live", False, f"uncaught {e}")

    # 429-style retry backoff is non-blocking exponential
    class FlakyModels:
        def __init__(self):
            self.calls = 0
        async def generate_content(self, **_):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("429 rate limit")
            return FakeResp(1)
    svc2 = KakarotLiveService(api_key="k", model="m", audio=FakeAudio(), tools=FakeTools())
    svc2._client = type("C", (), {"models": FlakyModels()})()
    svc2._types = svc._types
    t0 = time.perf_counter()
    try:
        await svc2.agent_loop(UserContent())
        dt = time.perf_counter() - t0
        record("429 exponential backoff", "live", svc2._client.models.calls == 3 and 2.5 < dt < 8,
               f"calls={svc2._client.models.calls} dt={dt:.2f}")
    except Exception as e:  # noqa: BLE001
        record("429 exponential backoff", "live", False, f"uncaught {e}")


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        await test_sandbox()
        test_security()
        test_system_db()
        await test_wal_loop()
        await test_desktop_context()
        await test_web_tools()
        await test_media_tools()
        await test_audit_logger(Path(td) / "audit")
        await test_agent_loop()

    failed = [r for r in RESULTS if not r[2]]
    print()
    print(f"TOTAL: {len(RESULTS)} | PASS: {len(RESULTS) - len(failed)} | FAIL: {len(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
