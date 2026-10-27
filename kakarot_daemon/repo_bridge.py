"""Bounded subprocess adapter that releases optional SDK memory after each call."""

import asyncio
import json
from pathlib import Path
import sys

LIMIT = 131072


class RepositoryBridge:
    def __init__(self):
        self.declarations = []
        self.character = {}
        self.error = ""
        self.settings_request = 0
        self._lock = asyncio.Lock()
        self._on_change = None

    def observe(self, callback) -> None:
        """Report concrete repository events to the state coordinator."""
        self._on_change = callback

    def _changed(self, **events) -> None:
        if self._on_change:
            self._on_change(**events)

    async def request(self, payload, timeout=45):
        data = json.dumps(payload).encode()
        if len(data) > LIMIT:
            return {"ok": False, "error": "Repository request exceeds 128 KiB."}
        async with self._lock:
            process = None
            try:
                async with asyncio.timeout(timeout):
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "kakarot_daemon.repo_worker",
                        cwd=str(Path(__file__).resolve().parents[1]),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    process.stdin.write(data)
                    await process.stdin.drain()
                    process.stdin.close()
                    output = bytearray()
                    while chunk := await process.stdout.read(8192):
                        output.extend(chunk)
                        if len(output) > LIMIT:
                            raise ValueError("oversized response")
                    await process.wait()
                    result = json.loads(output)
                    if process.returncode or not isinstance(result, dict):
                        raise ValueError("invalid worker result")
                    return result
            except (TimeoutError, OSError, ValueError):
                return {
                    "ok": False,
                    "error": "Repository worker unavailable or timed out. An action may have completed; verify before retrying.",
                }
            finally:
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()

    async def refresh(self):
        result = await self.request({"operation": "catalog"}, timeout=15)
        self.declarations = result.get("tools", []) if result.get("ok") else []
        self.character = result.get("character", {}) if result.get("ok") else {}
        self.error = result.get("error", "")
        self._changed(
            repository_error=self.error,
            repository_tools=[item["name"] for item in self.declarations],
        )

    async def execute(self, name, arguments):
        if name not in {item["name"] for item in self.declarations}:
            return {"ok": False, "error": "Unknown or disabled repository tool."}
        result = await self.request(
            {"operation": "execute", "name": name, "arguments": arguments}
        )
        if result.get("ok") and result.get("ui_action") == "open_settings":
            self.settings_request += 1
            self._changed(settings_request=self.settings_request)
        return result
