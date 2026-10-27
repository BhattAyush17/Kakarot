from __future__ import annotations

import asyncio
import subprocess
import sys

import numpy as np

from core.config_models import validate_config
from services.chat_bus import (
    MAX_SESSION_EVENTS,
    inject_text,
    register_session,
    unregister_session,
)
from services.gemini_session import _live_config, _resample_pcm16
from services.llm_bridge import active_provider, select_llm


def test_gemini_config_is_valid_and_bounded() -> None:
    config = validate_config(
        {
            "provider": "gemini",
            "llm": {"provider": "gemini"},
            "gemini": {"live_model": "test-live", "voice": "Kore"},
        }
    )
    assert active_provider(config) == "gemini"
    assert config["runtime"]["hard_memory_mb"] == 500
    assert _live_config(config).response_modalities == ["AUDIO"]


def test_select_gemini_does_not_import_legacy_sdks(monkeypatch) -> None:
    monkeypatch.setattr("services.llm_bridge.get_gemini_api_key", lambda: "test-key")
    selection = select_llm(
        {"llm": {"provider": "gemini"}, "gemini": {"live_model": "test-live"}}
    )
    assert selection.model == "test-live"
    assert selection.api_key == "test-key"
    legacy_modules = {"torch", "whisperx", "kokoro", "anthropic", "openai"}
    assert not legacy_modules & sys.modules.keys()


def test_main_import_does_not_load_local_ml() -> None:
    code = (
        "import sys,main;"
        "print(','.join(x for x in "
        "('torch','whisperx','kokoro','anthropic','openai') if x in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""


def test_audio_resample_24k_to_16k() -> None:
    source = (np.sin(np.linspace(0, 10, 24_000)) * 16_000).astype("<i2").tobytes()
    converted = _resample_pcm16(source, 24_000, 16_000)
    assert len(converted) == 16_000 * 2


def test_session_injection_queue_is_bounded() -> None:
    async def exercise() -> None:
        session_id, queue = register_session()
        try:
            for index in range(MAX_SESSION_EVENTS + 10):
                assert inject_text(str(index))
            assert queue.qsize() == MAX_SESSION_EVENTS
            assert await queue.get() == "10"
        finally:
            unregister_session(session_id)

    asyncio.run(exercise())
