"""HITL-gated OpenCode delegation and bounded sandbox wiring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kakarot_daemon import tools as tools_module
from kakarot_daemon.tools import ToolRegistry, tool_declarations


class OpenCodeDelegationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        self.binary = self.root / "opencode"
        self.binary.write_text("#!/bin/sh\n", encoding="utf-8")
        self.binary.chmod(0o755)

    def _registry(self):
        registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
        return registry

    async def test_declaration_is_minified(self) -> None:
        declaration = next(
            item
            for item in tool_declarations()
            if item["name"] == "delegate_to_opencode"
        )
        self.assertEqual(
            declaration["description"],
            "Delegates a coding task or self-repair to the local OpenCode DeepSeek agent.",
        )
        properties = declaration["parameters"]["properties"]
        self.assertEqual(set(properties), {"task_description", "target_files"})
        self.assertNotIn("description", properties["task_description"])
        self.assertNotIn("description", properties["target_files"])

    async def test_approval_payload_and_bounded_workspace_execution(self) -> None:
        registry = self._registry()
        approvals: list[tuple[tuple, dict]] = []
        runs: list[tuple[str, int, dict]] = []

        async def authorize(*args, **kwargs):
            approvals.append((args, kwargs))
            return True

        def fake_run(command, timeout, **kwargs):
            runs.append((command, timeout, kwargs))
            return {
                "ok": True,
                "stdout": "\x1b[0mdelegated\n",
                "stderr": "\x1b[31mwarning\x1b[0m\n",
                "returncode": 0,
            }

        registry._authorize_command = authorize
        with (
            patch.object(tools_module, "PROJECT_ROOT", self.root),
            patch.object(
                tools_module, "_find_opencode_binary", return_value=self.binary
            ),
            patch.object(tools_module, "OPENCODE_MODEL", "opencode/test-deepseek"),
            patch.object(tools_module, "OPENCODE_DELEGATION_TIMEOUT_SECONDS", 7),
            patch.object(tools_module, "run_sandboxed_command", side_effect=fake_run),
            patch.object(tools_module, "_stop_opencode_scope") as stop_scope,
        ):
            result = await registry._delegate_to_opencode(
                {
                    "task_description": "Fix the bug",
                    "target_files": ["src/app.py"],
                }
            )

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["stdout"], "delegated\n")
        self.assertEqual(result["stderr"], "warning\n")
        self.assertEqual(result["delegated_to"], "opencode")
        self.assertEqual(result["files"], ["src/app.py"])
        self.assertEqual(result["model"], "opencode/test-deepseek")
        stop_scope.assert_not_called()

        self.assertEqual(approvals[0][0], ("delegate_to_opencode",))
        payload = approvals[0][1]
        self.assertEqual(payload["label"], "OpenCode Delegation")
        self.assertIn("local DeepSeek agent", payload["reason"])
        self.assertEqual(
            payload["prompt"], "Task: Fix the bug\nFiles: src/app.py"
        )
        self.assertEqual(payload["options"], ["✓ Approve", "✕ Reject"])

        command, timeout, kwargs = runs[0]
        self.assertEqual(timeout, 7)
        self.assertIn("/tmp/opencode/bin/opencode run --standalone --auto", command)
        self.assertIn("--format default", command)
        self.assertIn("--model opencode/test-deepseek", command)
        self.assertIn("--file src/app.py", command)
        self.assertIn("Task: Fix the bug", command)
        self.assertIn("Files: src/app.py", command)
        self.assertEqual(kwargs["cwd"], "/workspace")
        self.assertEqual(kwargs["writable_workspace"], self.root)
        self.assertTrue(kwargs["allow_network"])
        self.assertTrue(kwargs["scope"])
        self.assertTrue(kwargs["scope_unit"].endswith(".scope"))
        self.assertIn("/tmp/opencode/node-bin", dict(kwargs["extra_env"])["PATH"])

    async def test_denial_never_spawns_opencode(self) -> None:
        registry = self._registry()

        async def authorize(*_args, **_kwargs):
            return False

        registry._authorize_command = authorize
        with (
            patch.object(tools_module, "PROJECT_ROOT", self.root),
            patch.object(
                tools_module, "_find_opencode_binary", return_value=self.binary
            ),
            patch.object(tools_module, "run_sandboxed_command") as runner,
        ):
            result = await registry._delegate_to_opencode(
                {
                    "task_description": "Remove the old code",
                    "target_files": ["src/app.py"],
                }
            )

        self.assertEqual(result, "User denied execution")
        runner.assert_not_called()

    async def test_target_files_cannot_escape_the_project(self) -> None:
        registry = self._registry()
        with (
            patch.object(tools_module, "PROJECT_ROOT", self.root),
            patch.object(
                tools_module, "_find_opencode_binary", return_value=self.binary
            ),
            self.assertRaises(ValueError),
        ):
            await registry._delegate_to_opencode(
                {
                    "task_description": "Edit outside files",
                    "target_files": ["../outside.py"],
                }
            )

    async def test_failed_run_reaps_the_transient_scope(self) -> None:
        registry = self._registry()

        async def authorize(*_args, **_kwargs):
            return True

        def fake_run(_command, timeout, **_kwargs):
            return {
                "ok": False,
                "error": "Command timed out after 7 seconds.",
                "stdout": "partial\n",
                "stderr": "",
                "returncode": -1,
            }

        registry._authorize_command = authorize
        with (
            patch.object(tools_module, "PROJECT_ROOT", self.root),
            patch.object(
                tools_module, "_find_opencode_binary", return_value=self.binary
            ),
            patch.object(tools_module, "OPENCODE_DELEGATION_TIMEOUT_SECONDS", 7),
            patch.object(tools_module, "run_sandboxed_command", side_effect=fake_run),
            patch.object(tools_module, "_stop_opencode_scope") as stop_scope,
        ):
            result = await registry._delegate_to_opencode(
                {
                    "task_description": "Fix the bug",
                    "target_files": ["src/app.py"],
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Command timed out after 7 seconds.")
        stop_scope.assert_called_once()
        self.assertTrue(stop_scope.call_args.args[0].endswith(".scope"))

    async def test_one_argument_legacy_approval_handler_still_gates(self) -> None:
        registry = self._registry()
        seen: list[str] = []

        async def authorize(prompt):
            seen.append(prompt)
            return True

        def fake_run(_command, timeout, **_kwargs):
            return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

        registry._authorize_command = authorize
        with (
            patch.object(tools_module, "PROJECT_ROOT", self.root),
            patch.object(
                tools_module, "_find_opencode_binary", return_value=self.binary
            ),
            patch.object(tools_module, "run_sandboxed_command", side_effect=fake_run),
        ):
            await registry._delegate_to_opencode(
                {
                    "task_description": "Fix the bug",
                    "target_files": ["src/app.py"],
                }
            )

        self.assertEqual(seen, ["Task: Fix the bug\nFiles: src/app.py"])


if __name__ == "__main__":
    unittest.main()
