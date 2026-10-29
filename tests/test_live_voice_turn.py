"""Local endpointing and echo-safe playback regressions.

These run without a microphone, speaker, or network.  They pin the two failures
that made the daemon unusable without a wake model: a turn never ended, and the
assistant's own voice reopened the microphone during playback.
"""

from __future__ import annotations

import asyncio
import math
import time
import struct
from collections import deque
from types import SimpleNamespace
from typing import Any
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
        self.mute_epoch = 0
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
        if muted:
            self.mute_epoch += 1
        self.events.append(("muted", self.muted))

    def sleep_microphone(self) -> None:
        self.muted = True
        self.audio_available = False
        self.events.append(("sleep_mic",))

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


def test_rolling_summary_is_appended_to_the_system_instruction() -> None:
    instruction = _build_system_instruction(
        {"name": "Kakarot"},
        {"stt_language": "en"},
        "The user fixed the audio path and prefers concise answers.",
    )

    assert instruction.endswith(
        "Prior Context: The user fixed the audio path and prefers concise answers."
    )


def test_desktop_telemetry_is_appended_to_the_system_instruction() -> None:
    instruction = _build_system_instruction(
        {"name": "Kakarot"},
        {"stt_language": "en"},
        "The user is working on the voice pipeline.",
        "Volume 40%, Dark Mode active, Wi-Fi connected to HomeNetwork",
    )

    assert "Prior Context: The user is working on the voice pipeline." in instruction
    assert (
        "Current State: Volume 40%, Dark Mode active, "
        "Wi-Fi connected to HomeNetwork"
    ) in instruction


def test_desktop_telemetry_uses_fast_bounded_probes() -> None:
    def fake_run(argv, **_kwargs):
        if argv[0] == "wpctl":
            return SimpleNamespace(stdout="Volume: 0.40 [MUTED]\n")
        if argv[0] == "gsettings":
            return SimpleNamespace(stdout="'prefer-dark'\n")
        if argv[0] == "nmcli":
            return SimpleNamespace(stdout="yes:HomeNetwork\nno:Guest\n")
        raise AssertionError(argv)

    with patch.object(live_module.subprocess, "run", side_effect=fake_run) as run:
        telemetry = live_module._collect_desktop_telemetry()

    assert telemetry == (
        "Volume 40% (muted), Dark Mode active, Wi-Fi connected to HomeNetwork"
    )
    assert {call.kwargs["timeout"] for call in run.call_args_list} == {
        live_module.DESKTOP_TELEMETRY_TIMEOUT_SECONDS
    }
    assert run.call_args_list[2].args[0] == [
        "nmcli",
        "-t",
        "-f",
        "active,ssid",
        "dev",
        "wifi",
    ]


def test_desktop_telemetry_is_cached_briefly() -> None:
    service = _service(FakeAudio())

    with patch.object(
        live_module,
        "_collect_desktop_telemetry",
        return_value="Volume 40%, Dark Mode active",
    ) as collect:
        first = service._get_desktop_telemetry()
        second = service._get_desktop_telemetry()

    assert first == second == "Volume 40%, Dark Mode active"
    assert collect.call_count == 1


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


def test_speak_caps_tts_text_at_one_thousand_characters() -> None:
    audio = FakeAudio()
    service = _service(audio)
    service._tts_models = ("test-tts",)
    captured: list[str] = []

    async def fake_synthesize(text: str) -> bytes:
        captured.append(text)
        return _frame(0.1) * 2

    service._synthesize_speech = fake_synthesize
    asyncio.run(service.speak("x" * 1500))

    assert captured == ["x" * 1000 + live_module.TTS_TRUNCATION_MARKER]


def test_manual_mute_discards_an_in_progress_utterance() -> None:
    audio = FakeAudio([_frame(0.1)] * 200)
    service = _service(audio)

    async def run() -> bytes:
        task = asyncio.create_task(service._capture_voice_turn())
        await asyncio.sleep(0.05)
        audio.set_muted(True)
        return await asyncio.wait_for(task, timeout=1.0)

    assert asyncio.run(run()) == b""


def test_unmute_during_playback_cancels_a_temporary_pause() -> None:
    audio = FakeAudio()
    service = _service(audio, follow_up_listening=True)
    service._types = types

    async def fake_synthesize(_text: str) -> bytes:
        audio.set_muted(True)
        audio.set_muted(False)
        return _frame(0.1) * 2

    service._synthesize_speech = fake_synthesize
    with patch.object(live_module, "PLAYBACK_TAIL_SECONDS", 0.0):
        asyncio.run(service.speak("Answer after a temporary pause."))

    assert audio.muted is False
    assert audio.events[-1] == ("muted", False)


def test_sleep_during_playback_keeps_the_microphone_shut() -> None:
    audio = FakeAudio()
    service = _service(audio, follow_up_listening=True)
    service._types = types

    async def fake_synthesize(_text: str) -> bytes:
        audio.sleep_microphone()
        return _frame(0.1) * 2

    service._synthesize_speech = fake_synthesize
    with patch.object(live_module, "PLAYBACK_TAIL_SECONDS", 0.0):
        asyncio.run(service.speak("This answer should not reopen the mic."))

    assert audio.audio_available is False
    assert audio.muted is True
    assert ("muted", False) not in audio.events


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


def test_generate_falls_back_after_timeout_with_a_fresh_six_second_budget() -> None:
    """A primary timeout must not consume the fallback's attempt budget."""
    service = _service(FakeAudio())
    service._fallback_models = ("fallback-model",)
    calls: list[str] = []
    budgets: list[float] = []
    expected = SimpleNamespace(usage_metadata=None)

    class Models:
        async def generate_content(self, *, model, contents, config):
            calls.append(model)
            if model == "test-model":
                raise TimeoutError("primary timed out")
            return expected

    service._client = SimpleNamespace(models=Models())
    real_wait_for = asyncio.wait_for

    async def record_wait_for(awaitable, *, timeout):
        budgets.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    with patch.object(live_module.asyncio, "wait_for", side_effect=record_wait_for):
        answer = asyncio.run(service._generate([], None))

    assert answer is expected
    assert calls == ["test-model", "fallback-model"]
    assert budgets == [12.0, 12.0]


def test_submit_content_returns_the_fallback_answer_after_primary_timeout() -> None:
    """The full voice entry point must survive a TimeoutError from the primary."""
    service = _service(FakeAudio())
    service._fallback_models = ("fallback-model",)
    service._initialized.set()
    service._types = types
    calls: list[str] = []
    expected = SimpleNamespace(
        function_calls=None,
        text="fallback answer",
        candidates=[],
        usage_metadata=None,
    )

    class Models:
        async def generate_content(self, *, model, contents, config):
            calls.append(model)
            if model == "test-model":
                raise TimeoutError("primary timed out")
            return expected

    service._client = SimpleNamespace(models=Models())
    content = types.Content(role="user", parts=[types.Part(text="hello")])

    async def no_speak(_text: str) -> None:
        return None

    service.speak = no_speak

    with patch.object(live_module, "append_message", return_value=None):
        answer = asyncio.run(service._submit_content(content))

    assert answer == "fallback answer"
    assert calls == ["test-model", "fallback-model"]


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


def test_agent_loop_executes_all_function_calls_in_order() -> None:
    """One macro response must run every call and return one combined turn."""
    service = _service(FakeAudio())
    service._types = types
    service._system_instruction = "test"
    executed: list[tuple[str, dict[str, Any]]] = []

    class Tools:
        async def execute(self, name, arguments):
            executed.append((name, arguments))
            return {"ok": True, "tool": name}

    service._tools = Tools()

    calls = [
        types.FunctionCall(name="manage_audio", args={"action": "mute"}),
        types.FunctionCall(
            name="manage_desktop_settings", args={"setting": "dark_mode"}
        ),
    ]
    responses = [
        SimpleNamespace(
            function_calls=calls,
            text=None,
            candidates=[
                SimpleNamespace(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(function_call=call) for call in calls],
                    )
                )
            ],
            usage_metadata=None,
        ),
        SimpleNamespace(
            function_calls=None,
            text="Both actions are complete.",
            candidates=[
                SimpleNamespace(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text="Both actions are complete.")],
                    )
                )
            ],
            usage_metadata=None,
        ),
    ]

    class Models:
        def __init__(self) -> None:
            self.index = 0

        async def generate_content(self, *, model, contents, config):
            response = responses[self.index]
            self.index += 1
            return response

    service._client = SimpleNamespace(models=Models())

    async def no_speak(_text: str) -> None:
        return None

    service.speak = no_speak
    content = types.Content(
        role="user", parts=[types.Part(text="mute and switch to dark mode")]
    )

    with patch.object(live_module, "append_message", return_value=None):
        answer = asyncio.run(service.agent_loop(content))

    assert answer == "Both actions are complete."
    assert executed == [
        ("manage_audio", {"action": "mute"}),
        ("manage_desktop_settings", {"setting": "dark_mode"}),
    ]

    tool_turns = [
        entry
        for entry in service._history
        if any(
            getattr(part, "function_response", None) is not None
            for part in (entry.parts or [])
        )
    ]
    assert len(tool_turns) == 1
    function_responses = [
        part.function_response
        for part in tool_turns[0].parts
        if getattr(part, "function_response", None) is not None
    ]
    assert [response.name for response in function_responses] == [
        "manage_audio",
        "manage_desktop_settings",
    ]
    assert all(response.response["result"]["ok"] for response in function_responses)


# -- granular phases, connection watchdog, recovery, follow-up window ------

from kakarot_daemon.state import DaemonState


def _tracked_service(
    audio: FakeAudio, *, follow_up_listening: bool = False
) -> tuple[KakarotLiveService, list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    service = KakarotLiveService(
        api_key="test",
        model="test-model",
        audio=audio,
        tools=SimpleNamespace(),
        tts_model="test-tts",
        follow_up_listening=follow_up_listening,
        on_change=lambda **change: changes.append(change),
    )
    service._vad_ready = True
    service._vad = None
    return service, changes


def test_derive_phase_reports_granular_activity() -> None:
    state = DaemonState()
    state.apply(connected=True)
    assert state.snapshot()["phase"] == "IDLE_MUTED"

    state.apply(muted=False)
    assert state.snapshot()["phase"] == "LISTENING"

    state.apply(processing=True)
    assert state.snapshot()["phase"] == "THINKING"

    state.apply(tool="manage_network")
    assert state.snapshot()["phase"] == "TOOL_CALL"
    assert state.snapshot()["tool"] == "manage_network"

    state.apply(speaking=True)
    assert state.snapshot()["phase"] == "SPEAKING"

    state.apply(unstable=True)
    assert state.snapshot()["phase"] == "UNSTABLE"

    state.apply(error="boom")
    assert state.snapshot()["phase"] == "ERROR"


def test_watchdog_flags_a_stalled_request_and_pauses_the_pipeline() -> None:
    audio = FakeAudio()
    service, changes = _tracked_service(audio)
    spoken: list[str] = []

    async def fake_speak(text: str) -> None:
        spoken.append(text)

    service.speak = fake_speak
    service._request_active_since = (
        time.monotonic() - live_module.STALL_WATCHDOG_SECONDS - 1.0
    )

    assert asyncio.run(service._check_stall()) is True
    assert {"unstable": True} in changes
    assert audio.muted is True
    assert {"muted": True} in changes
    assert spoken == [live_module.STALL_NOTICE]

    # The notice is spoken once per turn, not once per watchdog tick.
    assert asyncio.run(service._check_stall()) is False
    assert spoken == [live_module.STALL_NOTICE]


def test_watchdog_ignores_a_fast_request() -> None:
    service, changes = _tracked_service(FakeAudio())
    service._request_active_since = time.monotonic()

    assert asyncio.run(service._check_stall()) is False
    assert changes == []


def test_recovery_retries_once_after_reinitializing_the_client() -> None:
    audio = FakeAudio()
    service, changes = _tracked_service(audio)
    attempts: list[int] = []
    recovered: list[bool] = []

    async def flaky_generate(_contents: list[Any], _config: Any) -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("stalled")
        return "response"

    async def fake_recover() -> None:
        recovered.append(True)
        service._changed(unstable=True, error="")

    service._generate = flaky_generate
    service._recover_connection = fake_recover

    result = asyncio.run(service._generate_with_recovery([], None))

    assert result == "response"
    assert len(attempts) == 2
    assert recovered == [True]
    assert {"unstable": True, "error": ""} in changes
    assert {"unstable": False, "error": ""} in changes


def test_recovery_failure_speaks_and_reports_the_error_state() -> None:
    service, changes = _tracked_service(FakeAudio())
    spoken: list[str] = []

    async def always_fails(_contents: list[Any], _config: Any) -> str:
        raise TimeoutError("down")

    async def fake_recover() -> None:
        service._changed(unstable=True, error="")

    async def fake_speak(text: str) -> None:
        spoken.append(text)

    service._generate = always_fails
    service._recover_connection = fake_recover
    service.speak = fake_speak

    with pytest.raises(TimeoutError):
        asyncio.run(service._generate_with_recovery([], None))

    assert spoken == [live_module.RECONNECT_FAILED_NOTICE]
    assert {
        "unstable": False,
        "error": live_module.RECONNECT_FAILED_NOTICE,
    } in changes


def test_non_connection_errors_are_not_retried() -> None:
    service, changes = _tracked_service(FakeAudio())
    attempts: list[int] = []

    async def bad_request(_contents: list[Any], _config: Any) -> str:
        attempts.append(1)
        raise ValueError("bad request")

    service._generate = bad_request

    with pytest.raises(ValueError):
        asyncio.run(service._generate_with_recovery([], None))

    assert len(attempts) == 1
    assert changes == []


def test_recover_connection_rebuilds_the_client_and_clears_cooldowns() -> None:
    service, changes = _tracked_service(FakeAudio())
    original = service._client = object()
    service._model_cooldowns["m"] = time.monotonic() + 60
    service._tts_model_cooldowns["t"] = time.monotonic() + 60
    spoken: list[str] = []

    async def fake_speak(text: str) -> None:
        spoken.append(text)

    service.speak = fake_speak
    asyncio.run(service._recover_connection())

    assert service._client is not original
    assert service._model_cooldowns == {}
    assert service._tts_model_cooldowns == {}
    assert service._request_active_since is None
    assert spoken == [live_module.STALL_NOTICE]
    assert {"unstable": True, "error": ""} in changes


def test_submit_content_publishes_a_fresh_thinking_phase() -> None:
    service, changes = _tracked_service(FakeAudio())
    service._initialized.set()
    service._types = types
    service._client = SimpleNamespace()

    async def fake_agent_loop(_content: Any) -> str:
        return "answer"

    service.agent_loop = fake_agent_loop
    content = types.Content(role="user", parts=[types.Part(text="hello")])
    asyncio.run(service._submit_content(content))

    assert {"processing": True, "muted": True, "tool": ""} in changes
    assert {"processing": False} in changes


def test_follow_up_window_auto_mutes_without_speech() -> None:
    audio = FakeAudio([_frame(0.0001)] * 200)
    service, changes = _tracked_service(audio, follow_up_listening=True)

    async def run() -> bytes:
        service._follow_up_deadline = asyncio.get_running_loop().time() - 0.1
        return await asyncio.wait_for(service._capture_voice_turn(), timeout=1.0)

    assert asyncio.run(run()) == b""
    assert audio.muted is True
    assert {"muted": True} in changes


def test_speech_inside_follow_up_window_reopens_a_turn() -> None:
    audio = FakeAudio([_frame(0.10)] * 10 + [_frame(0.0001)] * 30)
    service, _ = _tracked_service(audio, follow_up_listening=True)

    async def run() -> bytes:
        service._follow_up_deadline = asyncio.get_running_loop().time() + 4.0
        return await asyncio.wait_for(service._capture_voice_turn(), timeout=2.0)

    pcm = asyncio.run(run())

    assert pcm
    assert service._follow_up_deadline is None
    assert audio.muted is False


def test_speak_arms_the_follow_up_window_and_broadcasts_speaking() -> None:
    audio = FakeAudio()
    service, changes = _tracked_service(audio, follow_up_listening=True)
    service._types = types

    async def fake_synthesize(_text: str) -> bytes:
        return _frame(0.1) * 2

    service._synthesize_speech = fake_synthesize
    with patch.object(live_module, "PLAYBACK_TAIL_SECONDS", 0.0):
        asyncio.run(service.speak("Hello."))

    assert audio.muted is False
    assert service._follow_up_deadline is not None
    assert {"speaking": True} in changes
    assert {"speaking": False} in changes
