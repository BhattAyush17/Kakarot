"""Regression tests for the Gemini Live response pipeline.

``google.genai``'s ``AsyncSession.receive()`` drains exactly one interaction
and then returns, so the proxy must re-enter it for every turn. These tests pin
that behaviour plus transcript aggregation and local tool-fault isolation
without touching the network.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from google.genai import types
from starlette.websockets import WebSocketDisconnect

from services import chat_bus, gemini_session


class FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.events: list[dict[str, Any]] = []
        self.changed = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise WebSocketDisconnect()
        return item

    async def send_text(self, payload: str) -> None:
        self.events.append(json.loads(payload))
        self.changed.set()

    async def wait_for(self, event_type: str, count: int = 1) -> None:
        async def _poll() -> None:
            while (
                sum(1 for event in self.events if event.get("type") == event_type)
                < count
            ):
                self.changed.clear()
                await self.changed.wait()

        await asyncio.wait_for(_poll(), timeout=3)


def _audio_clip() -> str:
    # Two bytes = one 24 kHz sample; enough for the resampler.
    return json.dumps(
        {
            "type": "kakarot.whisper_audio",
            "audio": base64.b64encode(b"\x00\x00").decode("ascii"),
        }
    )


def _turn(index: int, *, tool_call: bool = False):
    if tool_call:
        yield types.LiveServerMessage(
            tool_call=types.LiveServerToolCall(
                function_calls=[
                    types.FunctionCall(
                        name="memory_remember",
                        args={"text": "remember this"},
                        id=f"call-{index}",
                    )
                ]
            )
        )
    yield types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="pergunta ")
        )
    )
    yield types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text=str(index))
        )
    )
    yield types.LiveServerMessage(
        server_content=types.LiveServerContent(
            output_transcription=types.Transcription(text="resposta ")
        )
    )
    yield types.LiveServerMessage(
        server_content=types.LiveServerContent(
            output_transcription=types.Transcription(text=str(index)),
            model_turn=types.Content(
                parts=[
                    types.Part(
                        inline_data=types.Blob(
                            data=b"\x01\x02", mime_type="audio/pcm;rate=24000"
                        )
                    )
                ]
            ),
        )
    )
    yield types.LiveServerMessage(
        server_content=types.LiveServerContent(turn_complete=True)
    )


class FakeLive:
    def __init__(self) -> None:
        self.turns: asyncio.Queue[int] = asyncio.Queue()
        self.receive_calls = 0
        self.audio_chunks = 0
        self.tool_responses: list[list[types.FunctionResponse]] = []
        self.tool_call_turns: set[int] = set()

    async def send_realtime_input(
        self,
        *,
        audio: types.Blob | None = None,
        audio_stream_end: bool | None = None,
        text: str | None = None,
    ) -> None:
        if audio is not None:
            self.audio_chunks += 1
        if audio_stream_end:
            await self.turns.put(self.audio_chunks)

    async def send_tool_response(
        self, *, function_responses: list[types.FunctionResponse]
    ) -> None:
        self.tool_responses.append(list(function_responses))

    async def receive(self):
        self.receive_calls += 1
        index = await self.turns.get()
        for message in _turn(index, tool_call=index in self.tool_call_turns):
            yield message


class FakeClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.live = FakeLive()
        self.aio = SimpleNamespace(
            live=SimpleNamespace(connect=self._connect),
            aclose=self._aclose,
        )

    @asynccontextmanager
    async def _connect(self, **_: Any):
        yield self.live

    async def _aclose(self) -> None:
        pass


def _fake_config() -> dict[str, Any]:
    return {
        "gemini": {"live_model": "test-live", "voice": "Kore"},
        "character": {"name": "Kakarot"},
        "speech": {"tts_language": "en"},
    }


async def _run_turn(ws: FakeWebSocket) -> None:
    await ws.incoming.put(_audio_clip())


def test_turn_completion_accepts_both_protocol_signals() -> None:
    idle = types.LiveServerContent(
        interaction_status=types.InteractionStatus.IDLE
    )
    working = types.LiveServerContent(
        interaction_status=types.InteractionStatus.IN_PROGRESS
    )
    assert gemini_session._turn_is_complete(idle)
    assert gemini_session._turn_is_complete(
        types.LiveServerContent(turn_complete=True)
    )
    assert not gemini_session._turn_is_complete(working)
    assert not gemini_session._turn_is_complete(
        types.LiveServerContent(turn_complete=False)
    )


def test_live_pipeline_survives_multiple_turns(monkeypatch) -> None:
    async def scenario() -> None:
        chat_bus.clear_history()
        fake = FakeClient()
        monkeypatch.setattr(gemini_session.genai, "Client", lambda **_: fake)
        monkeypatch.setattr(gemini_session, "get_config", _fake_config)
        ws = FakeWebSocket()
        proxy = asyncio.create_task(
            gemini_session.gemini_session_proxy(ws, "test-key")
        )
        try:
            await ws.wait_for("session.updated")

            await _run_turn(ws)
            await ws.wait_for("response.done", 1)
            await _run_turn(ws)
            await ws.wait_for("response.done", 2)

            # A clean disconnect must end the proxy rather than leave it hung.
            await ws.incoming.put(None)
            await asyncio.wait_for(proxy, timeout=3)

            event_types = [event.get("type") for event in ws.events]
            assert event_types.count("response.created") == 2
            assert event_types.count("response.done") == 2
            assert event_types.count("response.output_audio.delta") == 2
            # One receive() call per turn, plus the pending next-turn call.
            assert fake.live.receive_calls >= 2

            user_events = [
                event
                for event in ws.events
                if event.get("type")
                == "conversation.item.input_audio_transcription.completed"
            ]
            assert [event["transcript"] for event in user_events] == [
                "pergunta 1",
                "pergunta 2",
            ]
            # Transcript fragments are accumulated into one history row per turn.
            assert [(row["role"], row["text"]) for row in chat_bus.get_history()] == [
                ("user", "pergunta 1"),
                ("assistant", "resposta 1"),
                ("user", "pergunta 2"),
                ("assistant", "resposta 2"),
            ]
        finally:
            if not proxy.done():
                proxy.cancel()
                try:
                    await proxy
                except asyncio.CancelledError:
                    pass
            chat_bus.clear_history()

    asyncio.run(scenario())


def test_local_tool_failure_does_not_end_the_session(monkeypatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database unavailable")

    async def scenario() -> None:
        chat_bus.clear_history()
        fake = FakeClient()
        fake.live.tool_call_turns.add(1)
        monkeypatch.setattr(gemini_session.genai, "Client", lambda **_: fake)
        monkeypatch.setattr(gemini_session, "get_config", _fake_config)
        monkeypatch.setattr(gemini_session, "remember", boom)
        ws = FakeWebSocket()
        proxy = asyncio.create_task(
            gemini_session.gemini_session_proxy(ws, "test-key")
        )
        try:
            await ws.wait_for("session.updated")
            await _run_turn(ws)
            await ws.wait_for("response.done", 1)

            assert len(fake.live.tool_responses) == 1
            assert fake.live.tool_responses[0][0].response == {
                "ok": False,
                "error": "database unavailable",
            }
            assert [(row["role"], row["text"]) for row in chat_bus.get_history()] == [
                ("user", "pergunta 1"),
                ("assistant", "resposta 1"),
            ]

            await ws.incoming.put(None)
            await asyncio.wait_for(proxy, timeout=3)
        finally:
            if not proxy.done():
                proxy.cancel()
                try:
                    await proxy
                except asyncio.CancelledError:
                    pass
            chat_bus.clear_history()

    asyncio.run(scenario())
