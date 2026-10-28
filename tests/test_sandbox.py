import os
import shlex
import shutil
import tempfile
import unittest
import asyncio
import uuid
from pathlib import Path

from kakarot_daemon.sandbox import (
    build_bwrap_invocation,
    execute_sandboxed,
    run_sandboxed_command,
)
from kakarot_daemon.tools import ToolRegistry

class TestSandbox(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_root(self):
        # 1. Read-Only Root Assertion
        res = await execute_sandboxed("touch /usr/test.tmp")
        self.assertFalse(res["ok"])
        self.assertIn("Read-only file system", res["stderr"])
        self.assertNotEqual(res["returncode"], 0)

    async def test_home_directory_shielding(self):
        # 2. Home Directory Shielding
        res = await execute_sandboxed("ls ~/.ssh")
        self.assertFalse(res["ok"])
        self.assertIn("No such file or directory", res["stderr"])

    async def test_scratch_write_access(self):
        # 3. Scratch Write Access
        res = await execute_sandboxed('echo "hello" > /tmp/test.txt && cat /tmp/test.txt')
        self.assertTrue(res["ok"])
        self.assertEqual(res["stdout"].strip(), "hello")

    async def test_network_gating(self):
        # 4. Network Gating (unprivileged ping requires cap_net_raw, so we test TCP via curl)
        res = await execute_sandboxed("curl -I --connect-timeout 1 http://8.8.8.8", allow_network=False)
        self.assertFalse(res["ok"])
        self.assertTrue(res["returncode"] != 0)

    def test_builder_uses_only_explicit_system_mounts(self):
        invocation = build_bwrap_invocation("echo hello")
        self.assertNotIn(
            ["--ro-bind", "/", "/"],
            [invocation[index : index + 3] for index in range(len(invocation) - 2)],
        )
        for path in ("/usr", "/bin", "/lib", "/lib64", "/etc/alternatives"):
            self.assertIn(path, invocation)
        self.assertNotIn("/etc", invocation)
        self.assertIn("--tmpfs", invocation)
        self.assertIn(str(Path.home()), invocation)
        self.assertIn("/run/user", invocation)
        self.assertIn("--unshare-all", invocation)

    async def test_home_config_and_runtime_dir_are_unreadable(self):
        config_dir = Path.home() / ".config"
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        )
        if not config_dir.is_dir() or not runtime_dir.is_dir():
            self.skipTest("private config/runtime directories are unavailable")

        token = uuid.uuid4().hex
        config_marker = config_dir / f".kakarot-sandbox-{token}"
        runtime_marker = runtime_dir / f".kakarot-sandbox-{token}"
        config_marker.write_text("CONFIG-SECRET", encoding="utf-8")
        runtime_marker.write_text("RUNTIME-SECRET", encoding="utf-8")
        self.addCleanup(config_marker.unlink, missing_ok=True)
        self.addCleanup(runtime_marker.unlink, missing_ok=True)

        command = (
            f"cat {shlex.quote(str(config_marker))} "
            f"{shlex.quote(str(runtime_marker))}"
        )
        registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
        result = await registry._execute_command({"command": command})
        self.assertNotIn("CONFIG-SECRET", result["stdout"])
        self.assertNotIn("RUNTIME-SECRET", result["stdout"])
        self.assertFalse(result["ok"])
        self.assertIn("No such file or directory", result["stderr"])

    async def test_writable_workspace_is_an_explicit_opt_in(self):
        if shutil.which("bwrap") is None:
            self.skipTest("bubblewrap is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            result = await asyncio.to_thread(
                run_sandboxed_command,
                "pwd; printf 'written\\n' > marker.txt",
                timeout=10,
                cwd="/workspace",
                writable_workspace=root,
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["stdout"].splitlines()[0], "/workspace")
            self.assertEqual((root / "marker.txt").read_text(), "written\n")


if __name__ == '__main__':
    unittest.main()
