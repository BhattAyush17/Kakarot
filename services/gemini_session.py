"""Gemini Live native-audio bridge for the existing Kakarot session protocol."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from core.config import settings
from core.config_store import get_config
from kakarot_daemon.usage import UsageTotals
from services.chat_bus import append_message, register_session, unregister_session
from services.memory_store import forget as forget_memory
from services.memory_store import remember
from services.memory_store import search as search_memory
from services.reminders import cancel as cancel_reminder
from services.reminders import create as create_reminder
from services.reminders import list_reminders

LOGGER = logging.getLogger(__name__)

INPUT_RATE = 16_000
CLIENT_RATE = 24_000
MAX_AUDIO_CLIP_BYTES = CLIENT_RATE * 2 * 30

MEMORY_FUNCTIONS = [
    types.FunctionDeclaration(
        name="memory_remember",
        description=(
            "Store a durable local memory only when the user explicitly asks you to "
            "remember something or clearly states a lasting preference or fact."
        ),
        parameters={
            "type": "OBJECT",
            "properties": {
                "text": {"type": "STRING"},
                "project": {"type": "STRING"},
                "private": {"type": "BOOLEAN"},
            },
            "required": ["text"],
        },
    ),
    types.FunctionDeclaration(
        name="memory_search",
        description=(
            "Search durable local memory when earlier preferences, decisions, tasks, "
            "or project facts may help answer the user."
        ),
        parameters={
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING"},
                "project": {"type": "STRING"},
            },
            "required": ["query"],
        },
    ),
    types.FunctionDeclaration(
        name="memory_forget",
        description="Delete one local memory by ID when the user asks to forget it.",
        parameters={
            "type": "OBJECT",
            "properties": {"memory_id": {"type": "INTEGER"}},
            "required": ["memory_id"],
        },
    ),
    types.FunctionDeclaration(
        name="reminder_create",
        description=(
            "Create a local durable timer or reminder. Convert the requested "
            "duration to delay_seconds and reuse operation_id when retrying."
        ),
        parameters={
            "type": "OBJECT",
            "properties": {
                "message": {"type": "STRING"},
                "delay_seconds": {"type": "NUMBER"},
                "operation_id": {"type": "STRING"},
            },
            "required": ["message", "delay_seconds", "operation_id"],
        },
    ),
    types.FunctionDeclaration(
        name="reminder_list",
        description="List active local timers and reminders.",
        parameters={"type": "OBJECT", "properties": {}},
    ),
    types.FunctionDeclaration(
        name="reminder_cancel",
        description="Cancel one active local reminder by ID.",
        parameters={
            "type": "OBJECT",
            "properties": {"reminder_id": {"type": "INTEGER"}},
            "required": ["reminder_id"],
        },
    ),
]


def _resample_pcm16(data: bytes, source_rate: int, target_rate: int) -> bytes:
    if not data or source_rate == target_rate:
        return data
    samples = np.frombuffer(data, dtype="<i2")
    if not samples.size:
        return b""
    target_count = max(1, round(samples.size * target_rate / source_rate))
    source_positions = np.arange(samples.size, dtype=np.float64)
    target_positions = np.linspace(0, samples.size - 1, target_count)
    converted = np.interp(target_positions, source_positions, samples).astype("<i2")
    return converted.tobytes()


def _live_config(config: dict[str, Any]) -> types.LiveConnectConfig:
    gemini = config.get("gemini") or {}
    character = config.get("character") or {}
    language = str((config.get("speech") or {}).get("tts_language") or "en")
    instruction = (
        settings.SYSTEM_PROMPT_CORE
        + f"\nMANDATORY LANGUAGE: Answer only in {language}."
        + f"\nIDENTITY: Your name is {character.get('name') or 'Kakarot'!s}."
        + "\nYou are a personal laptop assistant. Never claim a local action "
        + "succeeded unless a tool result confirms it."
        + "\nUse memory tools for durable recall. Private memories never appear in "
        + "cloud search results. Tell the user when memory storage or deletion "
        + "succeeds."
        + "\n"
        + str(character.get("personality_prompt") or "")
    )
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=instruction,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=str(gemini.get("voice") or "Kore")
                )
            )
        ),
        tools=[types.Tool(function_declarations=MEMORY_FUNCTIONS)],
    )


def _turn_is_complete(content: types.LiveServerContent) -> bool:
    """Mirror the SDK's own "interaction finished" test.

    Older servers set ``turn_complete``; newer ones report
    ``interaction_status`` instead. The Python SDK stops ``receive()`` on
    either signal, so the proxy must recognise both or the client never gets
    ``response.done``.
    """
    status = content.interaction_status
    if (
        status is not None
        and status != types.InteractionStatus.INTERACTION_STATUS_UNSPECIFIED
    ):
        return status == types.InteractionStatus.IDLE
    return bool(content.turn_complete)


async def _execute_memory_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "memory_remember":
            row = await asyncio.to_thread(
                remember,
                str(arguments.get("text") or ""),
                project=str(arguments.get("project") or ""),
                private=bool(arguments.get("private", False)),
            )
            return {"ok": True, "memory": row}
        if name == "memory_search":
            rows = await asyncio.to_thread(
                search_memory,
                str(arguments.get("query") or ""),
                project=str(arguments.get("project") or ""),
                include_private=False,
            )
            return {"ok": True, "memories": rows}
        if name == "memory_forget":
            removed = await asyncio.to_thread(
                forget_memory, int(arguments.get("memory_id") or 0)
            )
            return {"ok": removed, "memory_id": arguments.get("memory_id")}
        if name == "reminder_create":
            row = await asyncio.to_thread(
                create_reminder,
                str(arguments.get("message") or ""),
                delay_seconds=float(arguments.get("delay_seconds") or 0),
                operation_id=str(arguments.get("operation_id") or "") or None,
            )
            return {"ok": True, "reminder": row}
        if name == "reminder_list":
            rows = await asyncio.to_thread(list_reminders)
            return {"ok": True, "reminders": rows}
        if name == "reminder_cancel":
            reminder_id = int(arguments.get("reminder_id") or 0)
            removed = await asyncio.to_thread(cancel_reminder, reminder_id)
            return {"ok": removed, "reminder_id": reminder_id}
        return {"ok": False, "error": f"Unknown tool: {name}"}
    except Exception as exc:  # noqa: BLE001 - a local tool fault must not end the call
        return {"ok": False, "error": str(exc)}


async def gemini_session_proxy(client_ws: WebSocket, api_key: str) -> None:
    await client_ws.accept()
    config = get_config()
    model = str((config.get("gemini") or {}).get("live_model") or "gemini-3.8-live")
    session_id, injection_queue = register_session()
    client = genai.Client(api_key=api_key)
    await client_ws.send_text(
        json.dumps({"type": "session.created", "provider": "gemini"})
    )

    try:
        async with client.aio.live.connect(
            model=model, config=_live_config(config)
        ) as live:
            await client_ws.send_text(
                json.dumps({"type": "session.updated", "provider": "gemini"})
            )

            async def send_to_gemini() -> None:
                async def handle_client_event(item: str) -> None:
                    event = json.loads(item)
                    event_type = event.get("type")
                    if event_type == "kakarot.whisper_audio":
                        raw = base64.b64decode(
                            str(event.get("audio") or ""), validate=True
                        )
                        if len(raw) > MAX_AUDIO_CLIP_BYTES:
                            # An over-long recording must cost one turn, not the
                            # whole live session: reject it and keep listening.
                            await client_ws.send_text(
                                json.dumps(
                                    {
                                        "type": "error",
                                        "error": (
                                            "Audio clip exceeds the 30 second limit."
                                        ),
                                    }
                                )
                            )
                            return
                        pcm = _resample_pcm16(raw, CLIENT_RATE, INPUT_RATE)
                        await live.send_realtime_input(
                            audio=types.Blob(
                                data=pcm, mime_type="audio/pcm;rate=16000"
                            )
                        )
                        await live.send_realtime_input(audio_stream_end=True)
                    elif event_type == "conversation.item.create":
                        parts = (event.get("item") or {}).get("content") or []
                        text = " ".join(
                            str(part.get("text") or "").strip()
                            for part in parts
                            if isinstance(part, dict)
                            and part.get("type") == "input_text"
                        ).strip()
                        if text:
                            append_message("user", text)
                            await live.send_realtime_input(text=text)

                async def handle_injection(item: Any) -> None:
                    if isinstance(item, str):
                        append_message("user", item)
                        await live.send_realtime_input(text=item)
                    elif isinstance(item, dict):
                        await client_ws.send_text(json.dumps(item))

                while True:
                    receive_task = asyncio.create_task(client_ws.receive_text())
                    injection_task = asyncio.create_task(injection_queue.get())
                    done, pending = await asyncio.wait(
                        {receive_task, injection_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    # Both futures can become ready in the same tick. Handling
                    # only the first one silently drops the other utterance or
                    # the text typed in the Config chat.
                    if receive_task in done:
                        await handle_client_event(receive_task.result())
                    if injection_task in done:
                        await handle_injection(injection_task.result())

            async def receive_from_gemini() -> None:
                response_started = False
                input_parts: list[str] = []
                output_parts: list[str] = []
                turn_usage = UsageTotals(turns=1)

                async def flush_user_transcript() -> None:
                    text = "".join(input_parts).strip()
                    input_parts.clear()
                    if not text:
                        return
                    append_message("user", text)
                    await client_ws.send_text(
                        json.dumps(
                            {
                                "type": (
                                    "conversation.item."
                                    "input_audio_transcription.completed"
                                ),
                                "transcript": text,
                            },
                            ensure_ascii=False,
                        )
                    )

                async def finish_response(*, send_done: bool) -> None:
                    nonlocal response_started, turn_usage
                    text = "".join(output_parts).strip()
                    output_parts.clear()
                    if text:
                        append_message("assistant", text)
                    if turn_usage.requests:
                        turn_usage.successful_requests = max(
                            turn_usage.successful_requests, 1
                        )
                    if turn_usage.requests or turn_usage.total_tokens:
                        LOGGER.info("Live turn usage: %s", turn_usage.summary())
                    turn_usage = UsageTotals(turns=1)
                    response_started = False
                    if send_done:
                        await client_ws.send_text(
                            json.dumps({"type": "response.done"})
                        )

                # AsyncSession.receive() drains exactly one interaction and then
                # returns. A live conversation needs a new call per turn,
                # otherwise the whole session is torn down after the first one.
                while True:
                    received = False
                    async for response in live.receive():
                        received = True
                        if response.usage_metadata is not None:
                            turn_usage.add_usage(response.usage_metadata)
                        if response.tool_call:
                            function_responses = []
                            for call in response.tool_call.function_calls or []:
                                result = await _execute_memory_tool(
                                    str(call.name or ""), dict(call.args or {})
                                )
                                function_responses.append(
                                    types.FunctionResponse(
                                        name=call.name, id=call.id, response=result
                                    )
                                )
                            if function_responses:
                                await live.send_tool_response(
                                    function_responses=function_responses
                                )
                        content = response.server_content
                        # Input transcription is user-side audio; announcing a
                        # response there would make the pet look like it is
                        # answering while the user is still speaking.
                        starts_response = bool(
                            response.tool_call
                            or (
                                content
                                and (
                                    content.model_turn
                                    or content.output_transcription
                                )
                            )
                        )
                        if starts_response and not response_started:
                            response_started = True
                            turn_usage.begin_request()
                            await client_ws.send_text(
                                json.dumps({"type": "response.created"})
                            )
                        if (
                            content
                            and content.input_transcription
                            and content.input_transcription.text
                        ):
                            input_parts.append(content.input_transcription.text)
                        if (
                            content
                            and content.output_transcription
                            and content.output_transcription.text
                        ):
                            # Gemini streams both transcripts as fragments and
                            # the documented ``finished`` flag is not emitted, so
                            # the first model fragment is the reliable
                            # end-of-utterance marker for the user's audio.
                            await flush_user_transcript()
                            text = content.output_transcription.text
                            output_parts.append(text)
                            await client_ws.send_text(
                                json.dumps(
                                    {
                                        "type": (
                                            "response.output_audio_transcript.delta"
                                        ),
                                        "delta": text,
                                    },
                                    ensure_ascii=False,
                                )
                            )
                        if content and content.model_turn:
                            for part in content.model_turn.parts or []:
                                if part.inline_data and part.inline_data.data:
                                    await client_ws.send_text(
                                        json.dumps(
                                            {
                                                "type": "response.output_audio.delta",
                                                "delta": base64.b64encode(
                                                    part.inline_data.data
                                                ).decode("ascii"),
                                            }
                                        )
                                    )
                        if content and content.interrupted:
                            await flush_user_transcript()
                            await finish_response(send_done=False)
                            await client_ws.send_text(
                                json.dumps({"type": "kakarot.response_interrupted"})
                            )
                        if content and _turn_is_complete(content):
                            await flush_user_transcript()
                            await finish_response(send_done=True)
                    if not received:
                        # receive() ended without a message: the socket is gone.
                        break

            sender = asyncio.create_task(send_to_gemini())
            receiver = asyncio.create_task(receive_from_gemini())
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            await client_ws.send_text(json.dumps({"type": "error", "error": str(exc)}))
        except Exception:
            pass
    finally:
        unregister_session(session_id)
        await client.aio.aclose()
