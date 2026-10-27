"""Local endpointing and echo-safe playback regressions.

These run without a microphone, speaker, or network.  They pin the two failures
that made the daemon unusable without a wake model: a turn never ended, and the
assistant's own voice reopened the microphone during playback.
"""

from __future__ import annotations

import asyncio
import math
import struct
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.genai import types

import kakarot_daemon.live as live_module
from core.config import settings
from kakarot_daemon.live import (
    KakarotLiveService,
    _build_system_instruction,
    _scrub_tool_result,
)
from kakarot_daemon.tools import tool_declarations


def _frame(amplitude: float, samples: int = 640) -> bytes:
    values = [
        int(max(-1.0, min(1.0, amplitude)) * 32767 * math.sin(2 * math.pi * 220 * i / 16000))
        for i in range(samples)
    ]
    return struct.pack("<" + "h" * samples, *values)


class FakeAudio:
    def __init__(self, frames: list[bytes] | None = None) -> None:
        self.frames = deque(frames or [])
        self.muted = False
        self.wake_word_active = False
        self.audio_available = True
        self.barge_in_active = False
        self.events: list[tuple] = []
        self.output = bytearray()
        self.monitor_armed = False
        self.pending_output_bytes = 0

    async def next_input(self) -> bytes:
        if self.frames:
            return self.frames.popleft()
        # Silence keeps the loop alive; the follower stops on the EOF task.
        await asyncio.sleep(0.005)
        return b""

    def discard_input(self) -> None:
        self.events.append(("discard",))

    def set_muted(self, muted: bool, *, preserve_input: bool = False) -> None:
        self.muted = bool(muted)
        self.events.append(("muted", self.muted))

    def reset_barge_in(self) -> None:
        self.events.append(("reset_barge_in",))

    def start_playback_monitor(self) -> None:
        self.monitor_armed = True
        self.events.append(("monitor", True))

    def stop_playback_monitor(self) -> None:
        self.monitor_armed = False
        self.events.append(("monitor", False))

    async def queue_output(self, pcm: bytes) -> None:
        self.events.append(("queue", len(pcm)))
        self.output.extend(pcm)
        self.pending_output_bytes = 0

    def clear_playback(self) -> None:
        self.events.append(("clear",))
        self.output.clear()

    def set_barge_in_listener(self, _listener) -> None:
        pass


def _service(
    audio: FakeAudio, *, follow_up_listening: bool = False
) -> KakarotLiveService:
    service = KakarotLiveService(
        api_key="test",
        model="test-model",
        audio=audio,
        tools=SimpleNamespace(),
        tts_model="test-tts",
        follow_up_listening=follow_up_listening,
    )
    # Synthetic tones are not speech; use the deterministic energy fallback for
    # the endpointing tests instead of loading the neural VAD.
    service._vad_ready = True
    service._vad = None
    return service


def test_generate_content_config_enforces_spoken_english() -> None:
    instruction = _build_system_instruction(
        {"name": "Kakarot"},
        {"stt_language": "en", "tts_language": "en"},
    )
    config = types.GenerateContentConfig(system_instruction=instruction)

    assert settings.SYSTEM_PROMPT_CORE in config.system_instruction
    assert "NO markdown" in config.system_instruction
    assert "NO emojis" in config.system_instruction
    assert "NO line breaks" in config.system_instruction
    assert "MANDATORY LANGUAGE" in config.system_instruction
    assert "Answer only in English (en)" in config.system_instruction


def test_local_endpointing_ends_on_trailing_silence() -> None:
    audio = FakeAudio(
        [_frame(0.0001)] * 3
        + [_frame(0.10)] * 10
        + [_frame(0.0001)] * 30
    )
    service = _service(audio)

    pcm = asyncio.run(service._capture_voice_turn())

    assert pcm
    # The utterance, not the leading silence, is what reaches Gemini.  The
    # 0.25 s pre-roll plus speech plus the 0.9 s endpoint tail is bounded well
    # below the input queue; the three leading silence frames are the only
    # pre-roll that can be attached before the detector fires.
    assert 10 * 1280 < len(pcm) <= 40 * 1280


def test_endpointing_rejects_a_silent_room() -> None:
    audio = FakeAudio([_frame(0.0001)] * 200)
    service = _service(audio)

    async def run() -> bytes:
        task = asyncio.create_task(service._capture_voice_turn())
        await asyncio.sleep(0.05)
        service.stop()
        return await asyncio.wait_for(task, timeout=1.0)

    assert asyncio.run(run()) == b""


@pytest.mark.parametrize(
    ("follow_up_listening", "expected_muted"),
    [(False, True), (True, False)],
)
def test_speak_keeps_the_microphone_closed_during_playback(
    follow_up_listening: bool, expected_muted: bool
) -> None:
    audio = FakeAudio()
    service = _service(audio, follow_up_listening=follow_up_listening)
    service._types = types

    class Models:
        async def generate_content(self, **_kwargs):
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(
                                    inline_data=types.Blob(
                                        data=_frame(0.1) * 2,
                                        mime_type="audio/pcm;rate=24000",
                                    )
                                )
                            ]
                        )
                    )
                ]
            )

    service._client = SimpleNamespace(models=Models())
    asyncio.run(service.speak("Hello there."))

    assert audio.output
    # Without explicit follow-up listening, stay muted until the user asks for
    # the next turn. With it enabled, restore the listening intent only after
    # playback and the tail.
    assert audio.muted is expected_muted
    # The gate must close before synthesis and stay closed through queueing.
    queue_index = audio.events.index(("queue", len(audio.output)))
    assert audio.events[0] == ("reset_barge_in",)
    assert audio.events[1] == ("muted", True)
    assert queue_index > 1
    assert not audio.monitor_armed
    assert ("muted", False) not in audio.events[:queue_index]


def test_submit_content_does_not_reopen_the_microphone() -> None:
    """The answer path owns the gate; the turn wrapper must not unmute it."""
    audio = FakeAudio()
    service = _service(audio)
    service._initialized.set()
    service._types = types
    service._client = SimpleNamespace()

    async def fake_agent_loop(_content):
        return "answer"

    service.agent_loop = fake_agent_loop
    content = types.Content(role="user", parts=[types.Part(text="hello")])
    asyncio.run(service._submit_content(content))

    assert audio.muted is True
    assert audio.events[0] == ("muted", True)
    assert ("muted", False) not in audio.events


def test_tool_declarations_match_the_gemini_function_schema() -> None:
    """Local metadata must never be sent as an unknown Gemini field.

    ``requires_confirmation`` is an execution detail, not part of Google's
    FunctionDeclaration schema.  Passing it through made the whole voice
    session fail before it could open the microphone.
    """
    for declaration in tool_declarations():
        types.FunctionDeclaration(**declaration)
        assert "requires_confirmation" not in declaration


def test_generate_moves_to_fallback_when_primary_hangs() -> None:
    """One stuck model must not leave the turn in PROCESSING forever."""
    audio = FakeAudio()
    service = _service(audio)
    service._fallback_models = ("fallback-model",)
    calls = []

    class Models:
        async def generate_content(self, *, model, contents, config):
            calls.append(model)
            if model == "test-model":
                await asyncio.sleep(1.0)
            return f"answer from {model}"

    service._client = SimpleNamespace(models=Models())

    with patch.object(live_module, "GENERATE_ATTEMPT_TIMEOUT_SECONDS", 0.02):
        answer = asyncio.run(service._generate([], None))
        calls.clear()
        second = asyncio.run(service._generate([], None))

    assert answer == "answer from fallback-model"
    assert calls == ["fallback-model"]
    assert second == "answer from fallback-model"


def test_tts_quota_moves_to_fallback_and_cools_down() -> None:
    """A daily TTS quota must not be paid again on every answer."""
    service = _service(FakeAudio())
    service._tts_models = ("quota-tts", "fallback-tts")
    calls = []

    class QuotaError(Exception):
        code = 429

    class Models:
        async def generate_content(self, *, model, contents, config):
            calls.append(model)
            if model == "quota-tts":
                raise QuotaError("daily quota")
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(
                                    inline_data=SimpleNamespace(
                                        data=b"\x00\x00" * 480,
                                        mime_type="audio/L16;codec=pcm;rate=24000",
                                    )
                                )
                            ]
                        )
                    )
                ]
            )

    service._types = types
    service._client = SimpleNamespace(models=Models())

    first = asyncio.run(service._synthesize_speech("hello"))
    assert first
    assert calls == ["quota-tts", "fallback-tts"]

    calls.clear()
    second = asyncio.run(service._synthesize_speech("again"))
    assert second
    assert calls == ["fallback-tts"]


def test_tool_results_are_scrubbed_before_the_model_sees_them() -> None:
    result = _scrub_tool_result(
        {
            "ok": True,
            "stdout": "GEMINI_API_KEY=AIzaSyD-XYZ1234567890abcdef_A",
            "nested": ["AQ.AbCdEfGhIjKlMnOpQrStUvWxYz1234567"],
        }
    )
    assert "AIza" not in str(result)
    assert "AQ." not in str(result)
    assert "[REDACTED" in str(result)
