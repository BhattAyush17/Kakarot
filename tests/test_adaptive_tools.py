"""Adaptive tool loading, context compaction, and tool-result caps."""

from __future__ import annotations

import asyncio
import shutil
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from google.genai import types

from kakarot_daemon import tools as tools_module
from kakarot_daemon.live import (
    FIND_TOOLS_DECLARATION,
    KakarotLiveService,
    _is_trivial_utterance,
    _scrub_tool_result,
)
from kakarot_daemon.sandbox import (
    OUTPUT_TRUNCATION_MARKER,
    _StreamCapture,
    _truncate_output,
    run_sandboxed_command,
)
from kakarot_daemon.tools import (
    ToolRegistry,
    _truncate_middle,
    tool_declarations,
)


class _Audio:
    muted = True

    def set_barge_in_listener(self, listener: Any) -> None:
        self.listener = listener


class _Tools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"ok": True, "name": name}


def _declarations() -> list[types.FunctionDeclaration]:
    fields = set(types.FunctionDeclaration.model_fields)
    return [
        types.FunctionDeclaration(
            **{key: value for key, value in declaration.items() if key in fields}
        )
        for declaration in tool_declarations() + [FIND_TOOLS_DECLARATION]
    ]


def _service() -> KakarotLiveService:
    service = KakarotLiveService(
        api_key="k", model="m", audio=_Audio(), tools=_Tools()
    )
    service._types = types
    service._system_instruction = "test system"
    service._all_declarations = _declarations()
    service._declaration_by_name = {
        declaration.name: declaration
        for declaration in service._all_declarations
        if declaration.name
    }
    service._tool_config = service._build_tool_config(service._base_tool_names)
    service._turn_config = service._tool_config
    return service


def _declared_names(config: Any) -> set[str]:
    if config is None or not getattr(config, "tools", None):
        return set()
    tool = config.tools[0]
    return {
        declaration.name
        for declaration in (tool.function_declarations or [])
        if declaration.name
    }


def test_core_config_stays_small_and_keeps_discovery_tool() -> None:
    service = _service()
    service._prepare_turn()

    names = _declared_names(service._turn_config)
    assert "find_tools" in names
    assert "get_system_status" in names
    assert "execute_command" in names
    assert "schedule_reminder" not in names
    assert "play_media" not in names
    assert len(names) == 3


def test_text_hint_preloads_matching_tools_without_an_extra_round() -> None:
    service = _service()
    service._prepare_turn("please play some music")

    names = _declared_names(service._turn_config)
    assert "play_media" in names
    assert "find_tools" in names


def test_find_tools_adds_declarations_for_the_next_request() -> None:
    service = _service()
    service._prepare_turn()

    result = service._handle_find_tools({"query": "set a reminder"})

    assert result["ok"] is True
    assert "schedule_reminder" in result["added"]
    assert "list_due_reminders" in result["added"]
    names = _declared_names(service._turn_config)
    assert {"schedule_reminder", "list_due_reminders"} <= names
    assert "schedule_reminder" in service._session_tool_names


def test_find_tools_for_command_loads_the_system_command_runner() -> None:
    service = _service()
    service._prepare_turn()

    result = service._handle_find_tools({"query": "run a shell command"})

    assert result["ok"] is True
    assert "execute_command" in result["already_loaded"]
    assert "execute_system_command" in result["added"]


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/important",
        "sudo mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "echo compromised > /dev/sda",
        "chown -R root:root /home/user",
        "chmod -R 777 /home/user",
    ],
)
def test_destructive_system_commands_are_blocked_before_hitl(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
    approvals: list[tuple[str, dict[str, Any]]] = []
    runner_calls: list[tuple[str, int]] = []

    async def authorize(*args: Any, **kwargs: Any) -> bool:
        approvals.append((args[0], kwargs))
        return True

    def runner(value: str, timeout: int, **_kwargs: Any) -> dict[str, Any]:
        runner_calls.append((value, timeout))
        return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

    registry._authorize_command = authorize
    monkeypatch.setattr(tools_module, "_run_sandboxed_command", runner)

    result = asyncio.run(
        registry._execute_system_command(
            {"command": command, "explanation": "Test destructive command."}
        )
    )

    assert isinstance(result, str)
    assert "blocked" in result.lower()
    assert approvals == []
    assert runner_calls == []


def test_system_command_uses_action_tier_and_returns_bounded_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
    approvals: list[tuple[str, dict[str, Any]]] = []
    runner_calls: list[tuple[str, int]] = []

    async def authorize(*args: Any, **kwargs: Any) -> bool:
        approvals.append((args[0], kwargs))
        return True

    def runner(value: str, timeout: int, **_kwargs: Any) -> dict[str, Any]:
        runner_calls.append((value, timeout))
        return {
            "ok": True,
            "stdout": "HEAD" + ("x" * 5000) + "TAIL",
            "stderr": "",
            "returncode": 0,
        }

    registry._authorize_command = authorize
    monkeypatch.setattr(tools_module, "_run_sandboxed_command", runner)

    result = asyncio.run(
        registry._execute_system_command(
            {
                "command": "touch /tmp/kakarot-marker",
                "explanation": "Create a temporary marker file.",
            }
        )
    )

    assert approvals == [
        (
            "touch /tmp/kakarot-marker",
            {
                "level": "action",
                "label": "System Command Execution",
                "reason": "Create a temporary marker file.",
                "prompt": "touch /tmp/kakarot-marker",
                "options": ["✓ Execute", "✕ Cancel"],
            },
        )
    ]
    assert runner_calls == [
        ("touch /tmp/kakarot-marker", tools_module.APPROVED_COMMAND_TIMEOUT_SECONDS)
    ]
    assert "exit_code: 0" in result
    assert result.startswith("exit_code: 0")
    assert "HEAD" in result
    assert "TAIL" in result
    assert OUTPUT_TRUNCATION_MARKER in result
    assert len(result) < 2100


def test_sudo_system_command_uses_critical_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
    approvals: list[dict[str, Any]] = []

    async def authorize(*_args: Any, **kwargs: Any) -> bool:
        approvals.append(kwargs)
        return False

    def runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("denied command must not execute")

    registry._authorize_command = authorize
    monkeypatch.setattr(tools_module, "_run_sandboxed_command", runner)

    result = asyncio.run(
        registry._execute_system_command(
            {
                "command": "sudo dnf install -y example",
                "explanation": "Install an example package.",
            }
        )
    )

    assert result == "User denied execution"
    assert approvals[0]["level"] == "critical"
    assert approvals[0]["label"] == "System Command Execution"


def test_system_action_verbs_load_the_execute_system_command_tool() -> None:
    service = _service()
    for query in (
        "run dnf update",
        "execute the backup script",
        "open firefox",
        "kill the process",
        "set the brightness",
    ):
        assert "execute_system_command" in service._match_tool_names(query)


def test_os_wrapper_keywords_preload_specific_tools_not_the_general_runner() -> None:
    service = _service()
    cases = {
        "volume": "manage_audio",
        "audio": "manage_audio",
        "mute": "manage_audio",
        "change the volume": "manage_audio",
        "wifi": "manage_network",
        "network": "manage_network",
        "set wifi off": "manage_network",
        "dark mode": "manage_desktop_settings",
        "switch to dark mode": "manage_desktop_settings",
        "theme": "manage_desktop_settings",
    }
    for query, expected in cases.items():
        names = service._match_tool_names(query)
        assert expected in names, query
        assert "execute_system_command" not in names, query


def test_os_wrapper_declarations_are_minified() -> None:
    declarations = {item["name"]: item for item in tool_declarations()}

    assert declarations["manage_audio"]["description"] == (
        "Controls system audio volume and mute state via PipeWire."
    )
    assert declarations["manage_network"]["description"] == (
        "Checks network status and toggles Wi-Fi via NetworkManager."
    )
    assert declarations["manage_desktop_settings"]["description"] == (
        "Toggles Wayland desktop settings like dark mode."
    )
    assert declarations["manage_audio"]["parameters"]["properties"]["action"][
        "enum"
    ] == ["mute", "unmute", "set_volume", "status"]
    assert declarations["manage_network"]["parameters"]["properties"]["action"][
        "enum"
    ] == ["status", "toggle_wifi"]
    assert declarations["manage_desktop_settings"]["parameters"]["properties"][
        "setting"
    ]["enum"] == ["dark_mode", "light_mode"]
    for name in ("manage_audio", "manage_network", "manage_desktop_settings"):
        for property_schema in declarations[name]["parameters"]["properties"].values():
            assert "description" not in property_schema


def test_manage_audio_status_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
    approvals: list[Any] = []
    calls: list[tuple[str, int, dict[str, Any]]] = []

    async def authorize(*_args: Any, **_kwargs: Any) -> bool:
        approvals.append(True)
        return True

    def runner(command: str, timeout: int, **kwargs: Any) -> dict[str, Any]:
        calls.append((command, timeout, kwargs))
        return {"ok": True, "stdout": "Volume: 0.50\n", "stderr": "", "returncode": 0}

    registry._authorize_command = authorize
    monkeypatch.setattr(tools_module, "_run_sandboxed_command", runner)

    result = asyncio.run(registry._manage_audio({"action": "status"}))

    assert approvals == []
    assert calls == [
        (
            "wpctl get-volume @DEFAULT_AUDIO_SINK@",
            tools_module.DESKTOP_TOOL_TIMEOUT_SECONDS,
            {"host": True},
        )
    ]
    assert "Volume: 0.50" in result


def test_manage_audio_mutations_use_the_action_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
    approvals: list[tuple[str, dict[str, Any]]] = []

    async def authorize(*args: Any, **kwargs: Any) -> bool:
        approvals.append((args[0], kwargs))
        return True

    def runner(command: str, timeout: int, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

    registry._authorize_command = authorize
    monkeypatch.setattr(tools_module, "_run_sandboxed_command", runner)

    asyncio.run(
        registry._manage_audio({"action": "set_volume", "volume_percent": 35})
    )
    asyncio.run(registry._manage_audio({"action": "mute"}))
    asyncio.run(registry._manage_audio({"action": "unmute"}))

    assert [command for command, _payload in approvals] == [
        "wpctl set-volume @DEFAULT_AUDIO_SINK@ 35%",
        "wpctl set-mute @DEFAULT_AUDIO_SINK@ 1",
        "wpctl set-mute @DEFAULT_AUDIO_SINK@ 0",
    ]
    for _command, payload in approvals:
        assert payload["level"] == "action"
        assert payload["label"] == "Audio Control"
        assert payload["options"] == ["✓ Apply", "✕ Cancel"]


def test_manage_network_status_and_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
    approvals: list[tuple[str, dict[str, Any]]] = []
    calls: list[str] = []

    async def authorize(*args: Any, **kwargs: Any) -> bool:
        approvals.append((args[0], kwargs))
        return True

    def runner(command: str, timeout: int, **kwargs: Any) -> dict[str, Any]:
        calls.append(command)
        return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

    registry._authorize_command = authorize
    monkeypatch.setattr(tools_module, "_run_sandboxed_command", runner)

    asyncio.run(registry._manage_network({"action": "status"}))
    assert calls == ["nmcli general status"]
    assert approvals == []

    asyncio.run(registry._manage_network({"action": "toggle_wifi", "state": True}))
    asyncio.run(registry._manage_network({"action": "toggle_wifi", "state": False}))

    assert calls[-2:] == ["nmcli radio wifi on", "nmcli radio wifi off"]
    assert [command for command, _payload in approvals] == [
        "nmcli radio wifi on",
        "nmcli radio wifi off",
    ]
    for _command, payload in approvals:
        assert payload["level"] == "action"
        assert payload["label"] == "Network Control"


def test_manage_network_toggle_without_state_inverts_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
    approvals: list[str] = []
    calls: list[str] = []

    async def authorize(command: str, **_kwargs: Any) -> bool:
        approvals.append(command)
        return True

    def runner(command: str, timeout: int, **kwargs: Any) -> dict[str, Any]:
        calls.append(command)
        if command == "nmcli radio wifi":
            return {"ok": True, "stdout": "enabled\n", "stderr": "", "returncode": 0}
        return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

    registry._authorize_command = authorize
    monkeypatch.setattr(tools_module, "_run_sandboxed_command", runner)

    asyncio.run(registry._manage_network({"action": "toggle_wifi"}))

    assert calls == ["nmcli radio wifi", "nmcli radio wifi off"]
    assert approvals == ["nmcli radio wifi off"]


@pytest.mark.parametrize(
    ("setting", "scheme"),
    [("dark_mode", "prefer-dark"), ("light_mode", "default")],
)
def test_manage_desktop_settings_uses_action_tier(
    monkeypatch: pytest.MonkeyPatch, setting: str, scheme: str
) -> None:
    registry = ToolRegistry(reminders=object())  # type: ignore[arg-type]
    approvals: list[tuple[str, dict[str, Any]]] = []

    async def authorize(*args: Any, **kwargs: Any) -> bool:
        approvals.append((args[0], kwargs))
        return True

    def runner(command: str, timeout: int, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

    registry._authorize_command = authorize
    monkeypatch.setattr(tools_module, "_run_sandboxed_command", runner)

    asyncio.run(registry._manage_desktop_settings({"setting": setting}))

    assert len(approvals) == 1
    command, payload = approvals[0]
    assert command == (
        "gsettings set org.gnome.desktop.interface "
        f"color-scheme '{scheme}'"
    )
    assert payload["level"] == "action"
    assert payload["label"] == "Desktop Settings"
    assert payload["prompt"] == command


def test_execute_system_command_declaration_is_host_facing() -> None:
    declarations = {item["name"]: item for item in tool_declarations()}

    declaration = declarations["execute_system_command"]
    assert declaration["description"] == (
        "Executes a shell command on the host OS to manage files, networks, "
        "processes, or system settings."
    )
    assert declaration["parameters"]["required"] == ["command", "explanation"]
    assert set(declaration["parameters"]["properties"]) == {
        "command",
        "explanation",
    }


def test_discovery_round_then_action_round_uses_new_declaration() -> None:
    class Call:
        def __init__(self, name: str, args: dict[str, Any]) -> None:
            self.name = name
            self.args = args

    class Response:
        def __init__(
            self,
            *,
            calls: list[Call] | None = None,
            text: str | None = None,
        ) -> None:
            self.function_calls = calls or []
            self.text = text
            parts = (
                [types.Part(function_call=types.FunctionCall(name=c.name, args=c.args)) for c in self.function_calls]
                if self.function_calls
                else [types.Part(text=text or "")]
            )
            self.candidates = [
                SimpleNamespace(
                    content=SimpleNamespace(role="model", parts=parts)
                )
            ]
            self.usage_metadata = SimpleNamespace(
                prompt_token_count=10,
                candidates_token_count=2,
                total_token_count=12,
            )

    class Models:
        def __init__(self) -> None:
            self.configs: list[Any] = []
            self.responses = [
                Response(calls=[Call("find_tools", {"query": "reminder"})]),
                Response(
                    calls=[
                        Call(
                            "schedule_reminder",
                            {"task": "stand up", "relative_days": 0, "time_str": "09:00"},
                        )
                    ]
                ),
                Response(text="Reminder set."),
            ]

        async def generate_content(self, *, config: Any, **_: Any) -> Response:
            self.configs.append(config)
            return self.responses.pop(0)

    audio = _Audio()
    service = KakarotLiveService(
        api_key="k", model="m", audio=audio, tools=_Tools()
    )
    service._types = types
    service._system_instruction = "test system"
    service._all_declarations = _declarations()
    service._declaration_by_name = {
        declaration.name: declaration
        for declaration in service._all_declarations
        if declaration.name
    }
    service._tool_config = service._build_tool_config(service._base_tool_names)
    service._turn_config = service._tool_config
    models = Models()
    service._client = SimpleNamespace(models=models)

    answer = asyncio.run(
        service.agent_loop(types.Content(role="user", parts=[types.Part(text="hi")]))
    )

    assert answer == "Reminder set."
    assert len(models.configs) == 3
    assert "schedule_reminder" not in _declared_names(models.configs[0])
    assert "schedule_reminder" in _declared_names(models.configs[1])
    assert "schedule_reminder" in _declared_names(models.configs[2])
    assert set(service._session_tool_names) == {
        "schedule_reminder",
        "list_due_reminders",
    }


def test_compaction_drops_tool_traffic_and_keeps_the_newest_audio_turn() -> None:
    service = _service()
    old_audio = types.Content(
        role="user",
        parts=[types.Part(inline_data=types.Blob(data=b"a" * 1_500_000, mime_type="audio/wav"))],
    )
    new_audio = types.Content(
        role="user",
        parts=[types.Part(inline_data=types.Blob(data=b"b" * 1_500_000, mime_type="audio/wav"))],
    )
    service._history = [
        old_audio,
        types.Content(role="model", parts=[types.Part(text="old answer")]),
        types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name="get_system_status", args={}))],
        ),
        types.Content(
            role="user",
            parts=[types.Part(function_response=types.FunctionResponse(name="get_system_status", response={"ok": True}))],
        ),
        new_audio,
        types.Content(role="model", parts=[types.Part(text="new answer")]),
    ]

    service._compact_history()

    assert len(service._history) == 2
    assert service._history[0] is new_audio
    assert service._history[1].parts[0].text == "new answer"
    assert not any(
        getattr(part, "function_call", None) is not None
        or getattr(part, "function_response", None) is not None
        for entry in service._history
        for part in entry.parts
    )


def test_tool_results_are_capped_before_they_enter_history() -> None:
    result = _scrub_tool_result("x" * 5000, limit=120)
    assert len(result) < 160
    assert result.endswith("...[truncated]")
    nested = _scrub_tool_result({"stdout": "y" * 5000}, limit=80)
    assert "truncated" in nested["stdout"]


def test_os_output_keeps_head_and_tail_with_an_explicit_marker() -> None:
    output = "HEAD" + ("x" * 5000) + "TAIL"
    for bounded in (_truncate_middle(output), _truncate_output(output)):
        assert bounded.startswith("HEAD")
        assert bounded.endswith("TAIL")
        assert OUTPUT_TRUNCATION_MARKER in bounded
        assert "memory/token safety limits" in bounded
        assert len(bounded) < 2100


def test_stream_capture_returns_small_output_unchanged() -> None:
    capture = _StreamCapture()
    capture.feed("hello\nworld\n")
    capture.finish()
    assert capture.render() == "hello\nworld\n"
    assert not capture.truncated


def test_stream_capture_keeps_head_and_rolling_tail_only() -> None:
    capture = _StreamCapture()
    capture.feed("HEAD\n")
    for index in range(500):
        capture.feed(f"line-{index:03d}\n")
    capture.feed("TAIL\n")
    capture.finish()

    bounded = capture.render()
    assert bounded.startswith("HEAD\n")
    assert bounded.endswith("TAIL\n")
    assert OUTPUT_TRUNCATION_MARKER in bounded
    assert "line-499\n" in bounded
    assert "line-250\n" not in bounded
    assert len(bounded) < 2200


def test_stream_capture_bounds_a_single_pathological_line() -> None:
    capture = _StreamCapture()
    capture.feed("x" * 100_000)
    capture.finish()

    bounded = capture.render()
    assert OUTPUT_TRUNCATION_MARKER in bounded
    assert bounded.startswith("x" * 100)
    assert bounded.endswith("x" * 100)
    assert len(bounded) < 2200


def test_streaming_runner_kills_output_over_two_megabytes() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not installed")

    result = run_sandboxed_command(
        "python3 -c \"print('A' * 3_000_000)\"",
        timeout=20,
    )
    assert result["ok"] is False
    assert "2 MB safety limit" in result["error"]
    assert OUTPUT_TRUNCATION_MARKER in result["stdout"]
    assert len(result["stdout"]) < 2200


def test_streaming_runner_preserves_partial_output_on_timeout() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not installed")

    result = run_sandboxed_command("echo before; sleep 5", timeout=1)
    assert result["ok"] is False
    assert "timed out after 1 seconds" in result["error"]
    assert result["stdout"] == "before\n"


def test_tool_declarations_drop_inferable_parameter_descriptions() -> None:
    for declaration in tool_declarations():
        assert declaration["description"]
        for property_schema in declaration["parameters"].get(
            "properties", {}
        ).values():
            assert "description" not in property_schema


def test_coding_hints_discover_the_opencode_delegate() -> None:
    service = _service()
    for query in ("code", "fix the bug", "repair this file", "self repair"):
        assert "delegate_to_opencode" in service._match_tool_names(query)


def test_auto_medic_hints_load_diagnostics_and_restart_tools() -> None:
    service = _service()
    for query in (
        "diagnostics",
        "check logs",
        "pytest",
        "run the tests",
        "journalctl",
    ):
        assert "run_self_diagnostics" in service._match_tool_names(query)
    for query in ("reboot", "restart the daemon", "apply the update"):
        assert "restart_daemon" in service._match_tool_names(query)


def test_short_acknowledgements_skip_tool_schemas() -> None:
    for text in ("OK", "got it", "stop", "thanks", "yes"):
        assert _is_trivial_utterance(text)
    for text in (
        "play music",
        "set a reminder",
        "system status",
        "what is my cpu",
        "fix the bug",
        "repair this code",
    ):
        assert not _is_trivial_utterance(text)

    service = _service()
    service._prepare_turn("thanks")
    assert service._turn_tool_names == set()
    assert service._turn_config.tools is None


def test_history_never_grows_past_three_completed_turns() -> None:
    service = _service()
    service._history = []
    for index in range(4):
        service._history.extend(
            [
                types.Content(
                    role="user", parts=[types.Part(text=f"question {index}")]
                ),
                types.Content(
                    role="model", parts=[types.Part(text=f"answer {index}")]
                ),
            ]
        )

    service._compact_history()

    assert len(service._history) == 6
    assert service._history[0].parts[0].text == "question 1"
    assert service._history[-1].parts[0].text == "answer 3"


def test_system_tools_are_consolidated_into_one_declaration() -> None:
    declarations = {item["name"]: item for item in tool_declarations()}

    assert "system_status" not in declarations
    assert "get_system_state" not in declarations
    assert declarations["get_system_status"]["description"] == (
        "Return CPU, RAM, disk, load, and uptime diagnostics."
    )


def test_compaction_summarizes_pruned_turns_on_the_cheap_model() -> None:
    service = _service()
    service._character = {}
    service._types = types
    calls: list[tuple[str, str]] = []

    class Models:
        async def generate_content(self, *, model: str, contents: str, **_):
            calls.append((model, contents))
            return SimpleNamespace(text="The user tested music playback.")

    service._client = SimpleNamespace(models=Models())
    service._history = []
    for index in range(4):
        service._history.extend(
            [
                types.Content(
                    role="user", parts=[types.Part(text=f"question {index}")]
                ),
                types.Content(
                    role="model", parts=[types.Part(text=f"answer {index}")]
                ),
            ]
        )

    async def run() -> None:
        service._compact_history()
        if service._summary_task is not None:
            await service._summary_task

    asyncio.run(run())

    assert calls
    assert calls[0][0] == "gemini-3.1-flash-lite"
    assert "question 0" in calls[0][1]
    assert service._rolling_summary == "The user tested music playback."
    assert "Prior Context: The user tested music playback." in service._system_instruction


def test_rolling_summary_failure_is_suppressed() -> None:
    service = _service()
    service._character = {}
    service._types = types

    class Models:
        async def generate_content(self, **_):
            raise RuntimeError("quota exhausted")

    service._client = SimpleNamespace(models=Models())
    service._history = []
    for index in range(4):
        service._history.extend(
            [
                types.Content(
                    role="user", parts=[types.Part(text=f"question {index}")]
                ),
                types.Content(
                    role="model", parts=[types.Part(text=f"answer {index}")]
                ),
            ]
        )

    async def run() -> None:
        service._compact_history()
        if service._summary_task is not None:
            await service._summary_task

    asyncio.run(run())

    assert service._rolling_summary == ""
    assert len(service._history) == 6
