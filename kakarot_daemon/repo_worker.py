"""One trusted repo-tool operation per process. stdout is a private JSON protocol."""

import asyncio
import contextlib
import json
import sys

MAX_PAYLOAD = 131072


# The daemon has no OCR/screenshot backend and must not advertise a tool that
# can only fail. The legacy desktop app can still enable it for its own process.
DAEMON_UNSUPPORTED_TOOLS = frozenset({"screen_analyze"})


async def dispatch(request):
    from core.config_store import get_config
    from services.tool_runtime import execute_tool_call, get_session_tools

    config = get_config()
    declarations = [
        item
        for item in get_session_tools(config)
        if item.get("name") not in DAEMON_UNSUPPORTED_TOOLS
    ]
    if request.get("operation") == "catalog":
        character = config.get("character") or {}
        speech = config.get("speech") or {}
        return {
            "ok": True,
            "tools": [
                {key: item[key] for key in ("name", "description", "parameters")}
                for item in declarations
            ],
            "character": {
                "name": str(character.get("name") or "Kakarot")[:100],
                "personality": str(character.get("personality_prompt") or "")[:4000],
                "language": str(speech.get("tts_language") or "en")[:30],
            },
        }
    name = request.get("name")
    if name not in {item["name"] for item in declarations}:
        return {
            "ok": False,
            "error": "This repository capability is disabled or unconfigured.",
        }
    arguments = request.get("arguments", {})
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "Tool arguments must be an object."}
    if name == "open_configuration":
        return {
            "ok": True,
            "ui_action": "open_settings",
            "answer": "Requested the overlay settings drawer.",
        }
    if name == "external_mcp":
        from services.tool_catalog import _configured_external_mcps

        allowed = next(
            (
                server
                for server in _configured_external_mcps(config)
                if server["id"] == arguments.get("server_id")
            ),
            None,
        )
        if not allowed or arguments.get("tool_name") not in {
            item.get("name") for item in allowed["tools"]
        }:
            return {
                "ok": False,
                "error": "MCP tool must be present in the configured server tool list.",
            }
    return await execute_tool_call(name, arguments)


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
        if len(raw) > MAX_PAYLOAD:
            raise ValueError("Request too large")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("Expected object")
        # Existing helpers print diagnostics; keep them away from the wire and logs.
        with (
            open("/dev/null", "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            result = asyncio.run(dispatch(request))
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_PAYLOAD:
            result = {
                "ok": False,
                "error": "Tool result exceeded the output limit; the operation may have completed. Do not retry automatically.",
            }
    except ImportError as exc:
        missing = getattr(exc, "name", None) or "an optional integration"
        result = {
            "ok": False,
            "error": (
                f"Repository integration dependency unavailable ({missing}). "
                "Install requirements-gemini-integrations.txt if this capability is needed."
            ),
        }
    except Exception:
        result = {
            "ok": False,
            "error": "Repository tool failed. Check its configuration and credentials.",
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
