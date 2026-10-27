"""Regression tests for the daemon's stateless Gemini agent loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, ClassVar

from google.genai import types

from kakarot_daemon.live import (
    MAX_HISTORY_ENTRIES,
    MAX_TOOL_ROUNDS,
    KakarotLiveService,
    _is_plain_user_turn,
)


class _FakeCall:
    name = "execute_command"
    args: ClassVar[dict[str, Any]] = {"command": "ls"}


class _FakeModelContent:
    role = "model"
    parts: ClassVar[list[Any]] = [
        types.Part(
            function_call=types.FunctionCall(
                name="execute_command", args={"command": "ls"}
            )
        )
    ]


class _FakeResponse:
    """A model turn that always asks for a tool, like a long tool chain."""

    function_calls: ClassVar[list[Any]] = [_FakeCall()]
    text = None

    def __init__(self) -> None:
        self.candidates = [
            SimpleNamespace(content=_FakeModelContent()),
        ]


class _Models:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_content(self, **_: Any) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse()


class _Tools:
    class repository:
        character: ClassVar[dict[str, Any]] = {}
        declarations: ClassVar[list[Any]] = []

        async def refresh(self) -> None:
            pass

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}


def _service() -> KakarotLiveService:
    audio = SimpleNamespace(
        muted=True,
        set_barge_in_listener=lambda listener: None,
    )
    return KakarotLiveService(api_key="k", model="m", audio=audio, tools=_Tools())


def _user(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def _model_call(index: int) -> types.Content:
    return types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    name="execute_command",
                    args={"command": f"echo {index}"},
                )
            )
        ],
    )


def _tool_response(index: int) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    name="execute_command",
                    response={"result": {"ok": True, "index": index}},
                )
            )
        ],
    )


def test_tool_chain_keeps_the_user_request_in_history() -> None:
    async def scenario() -> None:
        service = _service()
        service._client = SimpleNamespace(models=_Models())
        service._types = types
        service._tool_config = None

        await asyncio.wait_for(
            service.agent_loop(_user("run the long tool chain")), timeout=5
        )

        assert len(service._history) == 1 + MAX_TOOL_ROUNDS * 2
        assert _is_plain_user_turn(service._history[0])
        # Every model function call is followed by its function response.
        for index in range(1, len(service._history), 2):
            call = service._history[index]
            response = service._history[index + 1]
            assert call.role == "model"
            assert call.parts[0].function_call is not None
            assert response.role == "user"
            assert response.parts[0].function_response is not None

    asyncio.run(scenario())


def test_successful_generate_clears_a_previous_health_error() -> None:
    async def scenario() -> None:
        changes: list[dict[str, Any]] = []
        audio = SimpleNamespace(
            muted=True,
            set_barge_in_listener=lambda listener: None,
        )
        service = KakarotLiveService(
            api_key="k",
            model="m",
            audio=audio,
            tools=_Tools(),
            on_change=lambda **event: changes.append(event),
        )
        service._client = SimpleNamespace(models=_Models())
        service._types = types
        service._tool_config = None
        service._changed(error="previous turn failed")

        await service._generate([], None)

        assert changes[-1] == {"error": ""}

    asyncio.run(scenario())


def test_trim_history_drops_whole_turns_only() -> None:
    service = _service()
    service._history = [
        _user("older request"),
        types.Content(role="model", parts=[types.Part(text="older answer")]),
        _user("current request"),
    ]
    for index in range(MAX_TOOL_ROUNDS):
        service._history.append(_model_call(index))
        service._history.append(_tool_response(index))

    assert len(service._history) == MAX_HISTORY_ENTRIES + 2
    service._trim_history()

    assert len(service._history) == MAX_HISTORY_ENTRIES
    assert _is_plain_user_turn(service._history[0])
    assert service._history[0].parts[0].text == "current request"
    # No orphaned function response may lead the history.
    assert not any(
        getattr(part, "function_response", None) is not None
        for part in service._history[0].parts
    )
