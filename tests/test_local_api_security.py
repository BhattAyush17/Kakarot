from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
from core import security


def _gemini_config() -> dict:
    return {
        "provider": "gemini",
        "llm": {"provider": "gemini"},
        "gemini": {"live_model": "test-live"},
        "tts": {"enabled": True, "provider": "inworld"},
        "secrets": {"gemini_api_key_configured": False},
        "mcps": {"home_assistant": {"enabled": False, "url": ""}},
        "local": {},
    }


def test_memory_api_and_websocket_require_local_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KAKAROT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(security, "LOCAL_TOKEN_PATH", tmp_path / "local-token")
    monkeypatch.setattr(main, "get_config", _gemini_config)
    monkeypatch.setattr("services.llm_bridge.get_gemini_api_key", lambda: "")
    token = security.get_local_token()

    with TestClient(main.app) as client:
        assert client.get("/api/memory").status_code == 401

        headers = {security.HEADER_NAME: token}
        created = client.post(
            "/api/memory", headers=headers, json={"text": "Remember API security"}
        )
        assert created.status_code == 200
        memory_id = created.json()["memory"]["id"]
        assert (
            client.get("/api/memory", headers=headers).json()["memories"][0]["id"]
            == memory_id
        )

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/session"):
                pass

        with client.websocket_connect("/session", headers=headers) as websocket:
            event = websocket.receive_json()
            assert event["type"] == "error"
            assert event["code"] == "legacy_session_disabled"
            assert "disabled" in event["error"].lower()
