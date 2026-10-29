# Inspection evidence and references

## Baseline

Repository: /home/tensorttx/Projects/Personal/Kakarot-custom

Inspected commit: dcf1b69ba736745fde092c72bd28786b12e3e70c

Working tree was clean when inspected. Graph project: home-tensorttx-Projects-Personal-Kakarot-custom. Recorded graph generation: 2026-09-26T18:11:52Z; index status ready, 1,080 nodes and 4,626 edges. Task-directed Verify evidence was used. This was not a full code/security audit or runtime benchmark.

## Confirmed source observations

| Existing file / symbol | Observation | Planning consequence |
|---|---|---|
| main.py lifespan, lines 171–201 | Non-local startup calls preload_configured_whisper; local path calls preload_local_provider | Gemini needs capability-based startup; adding provider enum alone is insufficient |
| main.py session, lines 837–851 | Routes local to local_session_proxy and other providers to chat_session_proxy | Add explicit native-audio branch and protocol compatibility |
| services/llm_bridge.py | Provider map contains inworld/openai/anthropic/local and imports their adapters | Introduce Gemini and lazy optional provider loading |
| core/config_models.py | Provider literals and typed provider models require extension | Coordinate configuration validation, defaults, migration and UI |
| services/chat_session.py | Cloud flow imports Whisper transcription and TTS paths | Native audio bypasses this pipeline rather than retaining local STT |
| services/chat_bus.py | MAX_MESSAGES=200; list-backed history and session queues; history not persisted | Durable memory is a new storage responsibility; bound event queues |
| services/local_preload.py | Preloads Whisper, Kokoro and LM Studio for local provider | Keep as optional legacy profile |
| services/whisper_service.py, tts_service.py | Models/pipelines cached after loading | Exclude those paths from lightweight startup |
| persona/personagem.py SessionBridge | Local websocket client, control queue and audio-clip interface | Inventory and extend event/audio flow; do not assume native streaming already works |
| core/security.py | Sensitive API path matching is explicit; HTTP token helpers provided | New route families need auth coverage; audit WebSocket handshake separately |
| services/process_control.py | Windows venv path and PowerShell fallback; process matching by script name | Platform-aware owned-process lifecycle work |
| tools/screen_tools.py | Existing screenshot analysis routes use current provider selection/fallbacks | Add Gemini image route and portable capture behavior |
| requirements-core.txt | WhisperX, Kokoro, openwakeword, provider SDKs and PySide6 are in core dependencies | Split tested installation profiles |
| README.md, docs/architecture.md | Document Windows setup, overlay/backend and launcher | Reuse structure; validate Linux and refresh documentation |

Graph search for session/memory/timer/Gemini/notification symbols returned 21 results without pagination remaining; this was a discovery aid, not proof that every possible related feature was absent. The /session outgoing call trace matched its inspected source. Specific snippets for lifecycle/session were read via graph tools; supporting source files were read directly.

Coverage checks found no recorded issue for the relevant indexed Python paths. tools/ and docs/ are excluded from this graph; referenced content there was read directly. Requirements text was read directly because freshness was not tracked. No recorded gap is not proof of complete graph coverage. The plan avoids asserting a repository-wide absence of every timer or notification mechanism; it specifies the durable assistant contracts required.

## Primary technical references

Reviewed 26 September 2026. Model names, quotas, SDK interfaces and platform behavior must be rechecked when implementation begins.

- [Gemini Live API overview](https://ai.google.dev/gemini-api/docs/live-api): native audio/vision and tool-use capability; input/output audio boundary.
- [Gemini Live SDK guide](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk): implementation starting point for the adapter.
- [Gemini document processing](https://ai.google.dev/gemini-api/docs/document-processing): PDF understanding; non-PDF input fidelity differs. The proposed app extracts Office formats locally rather than claiming automatic native Office layout support.
- [SQLite FTS5](https://www.sqlite.org/fts5.html): local full-text search/indexing implementation reference.

RAM allocations, latency goals, retention periods, job limits, and stage estimates are proposed engineering choices in this pack, not claims from these references or measured application behavior.
