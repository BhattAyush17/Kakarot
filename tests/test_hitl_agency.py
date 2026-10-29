from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from kakarot_daemon.control import ControlServer
from kakarot_daemon.tools import ToolRegistry


class HumanAgencyProtocolTests(unittest.IsolatedAsyncioTestCase):
    def _server(self) -> ControlServer:
        return ControlServer(
            Path("/tmp/kakarot-hitl-agency-test.sock"),
            mute=lambda _muted: None,
            status=lambda: {"phase": "IDLE_MUTED"},
            stop=lambda: None,
        )

    async def test_command_approval_uses_human_required_and_boolean_compatibility(self):
        server = self._server()
        server._subscribers.add(object())  # type: ignore[arg-type]
        captured = {}

        async def fake_broadcast(event_type, payload):
            captured["type"] = event_type
            captured["payload"] = payload
            server._handle_auth_response(
                {"request_id": payload["request_id"], "approved": True}
            )

        server.broadcast = fake_broadcast  # type: ignore[method-assign]
        approved = await server.request_authorization("touch /tmp/example")

        self.assertTrue(approved)
        self.assertEqual(captured["type"], "human_required")
        payload = captured["payload"]
        uuid.UUID(payload["request_id"])
        self.assertEqual(payload["level"], "decision")
        self.assertEqual(payload["label"], "Approval required")
        self.assertIn("cannot run automatically", payload["reason"])
        self.assertEqual(payload["prompt"], "touch /tmp/example")
        self.assertEqual(payload["options"], ["Approve", "Reject"])

    async def test_command_approval_accepts_custom_agency_payload(self):
        server = self._server()
        server._subscribers.add(object())  # type: ignore[arg-type]
        captured = {}

        async def fake_broadcast(event_type, payload):
            captured["type"] = event_type
            captured["payload"] = payload
            server._handle_auth_response(
                {"request_id": payload["request_id"], "option": "✓ Approve"}
            )

        server.broadcast = fake_broadcast  # type: ignore[method-assign]
        approved = await server.request_authorization(
            "delegate_to_opencode",
            label="OpenCode Delegation",
            reason="Kakarot is offloading a code modification to the local DeepSeek agent.",
            prompt="Task: Fix the bug\nFiles: src/app.py",
            options=["✓ Approve", "✕ Reject"],
        )

        self.assertTrue(approved)
        payload = captured["payload"]
        self.assertEqual(payload["level"], "decision")
        self.assertEqual(payload["label"], "OpenCode Delegation")
        self.assertIn("local DeepSeek agent", payload["reason"])
        self.assertEqual(payload["prompt"], "Task: Fix the bug\nFiles: src/app.py")
        self.assertEqual(payload["options"], ["✓ Approve", "✕ Reject"])

    async def test_custom_option_round_trip(self):
        server = self._server()
        server._subscribers.add(object())  # type: ignore[arg-type]
        captured = {}

        async def fake_broadcast(event_type, payload):
            captured["type"] = event_type
            captured["payload"] = payload
            server._handle_auth_response(
                {
                    "request_id": payload["request_id"],
                    "option": "I'm done",
                    "approved": False,
                }
            )

        server.broadcast = fake_broadcast  # type: ignore[method-assign]
        choice = await server.request_human_intervention(
            "action",
            "Action required",
            "Only you can plug in the external drive.",
            "Connect the drive, then acknowledge.",
            ["I'm done", "Skip"],
        )

        self.assertEqual(choice, "I'm done")
        self.assertEqual(captured["type"], "human_required")
        self.assertEqual(captured["payload"]["level"], "action")
        self.assertEqual(captured["payload"]["options"], ["I'm done", "Skip"])

    async def test_action_tier_authorization_is_preserved(self):
        server = self._server()
        server._subscribers.add(object())  # type: ignore[arg-type]
        captured = {}

        async def fake_broadcast(event_type, payload):
            captured["type"] = event_type
            captured["payload"] = payload
            server._handle_auth_response(
                {"request_id": payload["request_id"], "option": "✓ Restart"}
            )

        server.broadcast = fake_broadcast  # type: ignore[method-assign]
        approved = await server.request_authorization(
            "restart_daemon",
            level="action",
            label="Daemon Restart Required",
            reason="Kakarot needs to reboot to apply new code patches or clear its state.",
            prompt="systemctl --user restart kakarot-core.service",
            options=["✓ Restart", "✕ Cancel"],
        )

        self.assertTrue(approved)
        self.assertEqual(captured["payload"]["level"], "action")
        self.assertEqual(captured["payload"]["label"], "Daemon Restart Required")
        self.assertEqual(
            captured["payload"]["options"], ["✓ Restart", "✕ Cancel"]
        )

    async def test_no_subscriber_denies_without_blocking(self):
        server = self._server()
        self.assertFalse(await server.request_authorization("touch /tmp/example"))
        self.assertIsNone(
            await server.request_human_intervention(
                "input", "Need your input", "Need a destination.", "Which folder?", ["Continue"]
            )
        )


class HumanAgencyToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_human_intervention_defaults_action_to_im_done(self):
        registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
        seen = {}

        async def handler(level, label, reason, prompt, options):
            seen.update(
                level=level,
                label=label,
                reason=reason,
                prompt=prompt,
                options=options,
            )
            return "I'm done"

        registry.set_human_intervention_handler(handler)
        result = await registry._request_human_intervention(
            {
                "level": "action",
                "reason": "The user must approve the external step.",
                "prompt": "Insert the USB drive and confirm.",
            }
        )

        self.assertEqual(
            result, {"ok": True, "level": "action", "response": "I'm done"}
        )
        self.assertEqual(seen["label"], "Action required")
        self.assertEqual(seen["options"], ["I'm done"])

    async def test_invalid_level_is_rejected(self):
        registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            await registry._request_human_intervention(
                {"level": "root", "reason": "bad", "prompt": "bad"}
            )


if __name__ == "__main__":
    unittest.main()
