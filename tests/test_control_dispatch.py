"""Non-blocking dispatch regressions for the NDJSON control plane.

A manual turn can take tens of seconds (model latency plus tool execution).
The socket must acknowledge the input immediately and run the turn in the
background, and HITL responses must wake the waiting tool without the dispatch
call waiting for that tool to finish.
"""

from __future__ import annotations

import asyncio
import contextlib
import unittest
from pathlib import Path

from kakarot_daemon import control


def _server(**kwargs) -> control.ControlServer:
    return control.ControlServer(
        Path("/tmp/kakarot-control-dispatch-test.sock"),
        mute=lambda _muted: None,
        status=lambda: {"phase": "IDLE_MUTED"},
        stop=lambda: None,
        **kwargs,
    )


def _capture(sink: list[tuple[str, dict]]):
    async def capture(event_type: str, payload: dict) -> None:
        sink.append((event_type, payload))

    return capture


async def _drain(tasks: set[asyncio.Task]) -> None:
    if tasks:
        await asyncio.wait(tuple(tasks), timeout=1.0)


class ManualInputDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_input_returns_before_the_turn_finishes(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        sent: list[tuple[str, dict]] = []

        async def slow_turn(text: str) -> str:
            started.set()
            await release.wait()
            return f"answer:{text}"

        server = _server(manual_input=slow_turn)
        server.broadcast = _capture(sent)

        response = await asyncio.wait_for(
            server.dispatch_message(
                {
                    "sender": "mascot",
                    "type": "manual_input",
                    "payload": {"text": "hello"},
                }
            ),
            timeout=0.2,
        )

        self.assertEqual(response, {"ok": True, "status": "queued"})
        await asyncio.sleep(0)
        self.assertTrue(started.is_set())
        self.assertFalse(release.is_set())

        release.set()
        await _drain(server._manual_tasks)

        self.assertIn(("ui_say", {"text": "Working on that…", "status": "busy"}), sent)
        self.assertIn(("ui_say", {"text": "answer:hello", "status": "idle"}), sent)
        await server.close()

    async def test_manual_input_backlog_is_bounded(self) -> None:
        release = asyncio.Event()

        async def slow_turn(_text: str) -> str:
            await release.wait()
            return ""

        server = _server(manual_input=slow_turn)
        server.broadcast = _capture([])

        responses = [
            await server.dispatch_message(
                {
                    "sender": "mascot",
                    "type": "manual_input",
                    "payload": {"text": f"message {index}"},
                }
            )
            for index in range(control.MAX_PENDING_MANUAL_INPUTS)
        ]
        await asyncio.sleep(0)
        rejected = await server.dispatch_message(
            {
                "sender": "mascot",
                "type": "manual_input",
                "payload": {"text": "one too many"},
            }
        )

        self.assertEqual(
            [response["status"] for response in responses],
            ["queued"] * control.MAX_PENDING_MANUAL_INPUTS,
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["status"], "busy")

        release.set()
        await _drain(server._manual_tasks)
        await server.close()

    async def test_manual_input_failure_is_reported_without_breaking_dispatch(
        self,
    ) -> None:
        sent: list[tuple[str, dict]] = []

        async def failing_turn(_text: str) -> str:
            raise RuntimeError("model down")

        server = _server(manual_input=failing_turn)
        server.broadcast = _capture(sent)

        response = await server.dispatch_message(
            {
                "sender": "mascot",
                "type": "manual_input",
                "payload": {"text": "hello"},
            }
        )
        self.assertEqual(response, {"ok": True, "status": "queued"})

        await _drain(server._manual_tasks)
        self.assertIn(("ui_say", {"text": "model down", "status": "error"}), sent)

        # The server still accepts and runs the next input.
        async def ok_turn(text: str) -> str:
            return f"ok:{text}"

        server._manual_input = ok_turn
        second = await server.dispatch_message(
            {
                "sender": "mascot",
                "type": "manual_input",
                "payload": {"text": "again"},
            }
        )
        self.assertEqual(second, {"ok": True, "status": "queued"})
        await _drain(server._manual_tasks)
        self.assertIn(("ui_say", {"text": "ok:again", "status": "idle"}), sent)
        await server.close()

    async def test_shutdown_cancels_queued_turns(self) -> None:
        started = asyncio.Event()

        async def stuck_turn(_text: str) -> str:
            started.set()
            await asyncio.Event().wait()
            return ""

        server = _server(manual_input=stuck_turn)
        server.broadcast = _capture([])
        await server.dispatch_message(
            {
                "sender": "mascot",
                "type": "manual_input",
                "payload": {"text": "hello"},
            }
        )
        await asyncio.sleep(0)
        self.assertTrue(started.is_set())

        await asyncio.wait_for(server.close(), timeout=1.0)

        self.assertEqual(server._manual_tasks, set())


class HumanResponseDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_human_response_is_acknowledged_without_waiting_for_the_tool(
        self,
    ) -> None:
        server = _server()
        waiter: asyncio.Future[str | None] = (
            asyncio.get_running_loop().create_future()
        )
        server._authorization_waiters["request-1"] = waiter
        tool_finished = asyncio.Event()

        async def tool_turn() -> None:
            await waiter
            # Stand-in for the approved command: slow, but no longer the
            # socket's problem once the response has been accepted.
            await asyncio.sleep(0.5)
            tool_finished.set()

        turn = asyncio.create_task(tool_turn())
        response = await asyncio.wait_for(
            server.dispatch_message(
                {
                    "sender": "mascot",
                    "type": "human_response",
                    "payload": {
                        "request_id": "request-1",
                        "option": "✓ Apply",
                    },
                }
            ),
            timeout=0.1,
        )

        self.assertEqual(response, {"ok": True, "status": "accepted"})
        self.assertTrue(waiter.done())
        self.assertEqual(waiter.result(), "✓ Apply")
        self.assertFalse(tool_finished.is_set())

        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        await server.close()

    async def test_unknown_or_expired_request_is_rejected_immediately(self) -> None:
        server = _server()
        response = await asyncio.wait_for(
            server.dispatch_message(
                {
                    "sender": "mascot",
                    "type": "auth_response",
                    "payload": {"request_id": "gone", "approved": True},
                }
            ),
            timeout=0.1,
        )
        self.assertFalse(response["ok"])
        self.assertNotIn("status", response)
        await server.close()


if __name__ == "__main__":
    unittest.main()
