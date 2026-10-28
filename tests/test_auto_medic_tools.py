"""Auto-Medic diagnostics and HITL-gated daemon restart."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kakarot_daemon import tools as tools_module
from kakarot_daemon.tools import ToolRegistry, tool_declarations


class AutoMedicToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def _registry(self) -> ToolRegistry:
        return ToolRegistry(reminders=object())  # type: ignore[arg-type]

    async def test_diagnostics_runs_tests_and_logs_without_approval(self) -> None:
        registry = self._registry()
        calls: list[tuple[str, int, dict]] = []

        async def authorize(*_args, **_kwargs):
            raise AssertionError("read-only diagnostics must not request approval")

        def fake_run(command, timeout, **kwargs):
            calls.append((command, timeout, kwargs))
            if "pytest" in command:
                return {
                    "ok": True,
                    "stdout": "FAILED tests/test_example.py::test_bad\n",
                    "stderr": "",
                    "returncode": 1,
                }
            return {
                "ok": True,
                "stdout": "kakarot-core.service: traceback\n",
                "stderr": "",
                "returncode": 0,
            }

        registry._authorize_command = authorize
        with (
            patch.object(tools_module, "PROJECT_ROOT", self.root),
            patch.object(
                tools_module, "run_sandboxed_command", side_effect=fake_run
            ),
        ):
            result = await registry._run_self_diagnostics({})

        self.assertTrue(result["ok"])
        self.assertIn("FAILED tests/test_example.py", result["output"])
        self.assertIn("kakarot-core.service: traceback", result["output"])

        by_command = {command: (timeout, kwargs) for command, timeout, kwargs in calls}
        pytest_command = next(
            command for command in by_command if "pytest -q tests/" in command
        )
        pytest_timeout, pytest_kwargs = by_command[pytest_command]
        self.assertEqual(pytest_timeout, tools_module.SELF_DIAGNOSTICS_TIMEOUT_SECONDS)
        self.assertEqual(pytest_kwargs["cwd"], "/workspace")
        self.assertIn(
            (self.root, "/workspace", False), pytest_kwargs["extra_mounts"]
        )
        self.assertEqual(
            by_command[tools_module.SELF_DIAGNOSTICS_LOG_COMMAND][0],
            tools_module.SELF_DIAGNOSTICS_LOG_TIMEOUT_SECONDS,
        )

    async def test_restart_uses_action_tier_and_returns_before_running(self) -> None:
        registry = self._registry()
        approvals: list[tuple[tuple, dict]] = []
        delayed = AsyncMock()

        async def authorize(*args, **kwargs):
            approvals.append((args, kwargs))
            return True

        registry._authorize_command = authorize
        with patch.object(registry, "_delayed_restart", new=delayed):
            result = await registry._restart_daemon({})
            self.assertTrue(result.startswith("Restart initiated"))
            self.assertIsNotNone(registry._restart_task)
            await registry._restart_task

        delayed.assert_awaited_once()
        self.assertEqual(approvals[0][0], ("restart_daemon",))
        payload = approvals[0][1]
        self.assertEqual(payload["level"], "action")
        self.assertEqual(payload["label"], "Daemon Restart Required")
        self.assertEqual(
            payload["reason"],
            "Kakarot needs to reboot to apply new code patches or clear its state.",
        )
        self.assertEqual(
            payload["prompt"],
            "systemctl --user restart kakarot-core.service",
        )
        self.assertEqual(payload["options"], ["✓ Restart", "✕ Cancel"])

    async def test_denied_restart_never_schedules_a_task(self) -> None:
        registry = self._registry()
        delayed = AsyncMock()

        async def authorize(*_args, **_kwargs):
            return False

        registry._authorize_command = authorize
        with patch.object(registry, "_delayed_restart", new=delayed):
            result = await registry._restart_daemon({})

        self.assertEqual(result, "User denied execution")
        self.assertIsNone(registry._restart_task)
        delayed.assert_not_called()

    async def test_delayed_restart_waits_then_calls_systemctl(self) -> None:
        registry = self._registry()

        class Process:
            async def wait(self) -> int:
                return 0

        with (
            patch(
                "kakarot_daemon.tools.asyncio.sleep", new=AsyncMock()
            ) as sleep,
            patch(
                "kakarot_daemon.tools.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=Process()),
            ) as spawn,
        ):
            await registry._delayed_restart()

        sleep.assert_awaited_once_with(2.0)
        args = spawn.await_args.args
        self.assertEqual(
            args,
            ("systemctl", "--user", "restart", "kakarot-core.service"),
        )

    async def test_auto_medic_declarations_are_minified(self) -> None:
        declarations = {
            item["name"]: item for item in tool_declarations()
        }
        diagnostics = declarations["run_self_diagnostics"]
        restart = declarations["restart_daemon"]
        self.assertEqual(
            diagnostics["description"],
            "Runs unit tests and fetches recent daemon logs to diagnose system health.",
        )
        self.assertEqual(
            restart["description"],
            "Restarts the Kakarot daemon to apply code changes.",
        )
        for declaration in (diagnostics, restart):
            self.assertEqual(declaration["parameters"]["properties"], {})


if __name__ == "__main__":
    unittest.main()
