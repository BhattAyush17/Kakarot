# Requirements and acceptance matrix

MUST requirements gate the stage shown. SHOULD requirements can be deferred only with a recorded reason. Stages and test cases are defined in the delivery and validation documents.

| ID | Priority | Requirement / acceptance | Stage | Test |
|---|---|---|---|---|
| R01 | MUST | Lightweight install starts without WhisperX, Kokoro, Torch, or legacy provider SDKs installed | S1 | T01 |
| R02 | MUST | Gemini accepts voice/text, returns speech/captions, and handles interruption without stale playback | S2 | T02 |
| R03 | MUST | Gemini key remains in OS credential storage; API responses/logs never expose it | S1–S2 | T03 |
| R04 | MUST | Explicit memories persist across restart and support inspect/edit/delete/export | S3 | T04 |
| R05 | MUST | Every retrieved claim carries source ID, location where available, timestamp, and evidence type | S3–S5 | T05 |
| R06 | MUST | Selected local context only; private/excluded records cannot enter outbound requests | S3 | T06 |
| R07 | MUST | Timers/reminders survive restart and reconcile after sleep, with documented duplicate-delivery semantics | S4 | T07 |
| R08 | MUST | Notifications work without a live Gemini session; speech failure does not suppress the alert | S4 | T07 |
| R09 | MUST | Screenshot/image requests use actual selected capture and expose capture/sharing state | S5 | T08 |
| R10 | MUST | Format support matches the PRD; unsupported/partial extraction is explicit | S5 | T09 |
| R11 | MUST | Folder indexing is incremental, cancellable, scoped, and handles source changes/deletions | S5 | T10 |
| R12 | MUST | Observation is opt-in, paused on lock, bounded, and independently disableable | S6 | T11 |
| R13 | MUST | Proactive messages obey quiet hours, rate limits, and deduplication | S6 | T11 |
| R14 | MUST | App-wide RAM ceiling is 500,000,000 bytes in supported release workloads | S0–S7 | T12 |
| R15 | SHOULD | Idle RAM stays below 200,000,000 bytes and local idle CPU below 1% of one core | S1–S7 | T12 |
| R16 | MUST | Audio, event, image, and job queues have explicit bounds and overload behavior | S1–S5 | T13 |
| R17 | MUST | Local command/retrieval responsiveness meets the validation targets | S3–S7 | T14 |
| R18 | MUST | Offline mode preserves local search, typed deterministic commands, and notifications | S4 | T15 |
| R19 | MUST | New sensitive HTTP routes and WebSocket handshakes authenticate local clients | S1 | T03 |
| R20 | MUST | Document/screen content cannot grant tool authority or escape selected roots | S5 | T16 |
| R21 | MUST | Config/schema migrations preserve supported existing provider configuration and have backup/recovery instructions | S1–S7 | T17 |
| R22 | MUST | Linux packaging and capture/shortcut behavior are tested on the target display server | S0–S7 | T18 |
| R23 | SHOULD | Existing Windows launcher/provider flows retain smoke-test coverage | S7 | T18 |
| R24 | MUST | Resource pressure pauses/kills optional work and reports degradation without losing committed memory/timers | S5–S7 | T13 |
| R25 | MUST | Retention, forgetting, FTS cleanup, cache removal, and backup limitations are documented and tested | S3–S7 | T19 |
| R26 | MUST | Tool call retries use stable operation IDs; reported success requires a successful execution result | S2–S4 | T20 |
| R27 | SHOULD | Wake word fits the same budget, can unload when disabled, and never silently replaces push-to-talk | S6 | T12 |
| R28 | MUST | Resource and error diagnostics omit raw document/audio/transcript content by default | S1–S7 | T03 |

## Proposed configuration additions

Use typed models, versioned defaults, and migration tests. These are fields to implement, not currently accepted configuration.

| Section | Fields and initial policy |
|---|---|
| runtime | profile=lightweight; soft_memory_mb=400; critical_memory_mb=450; hard_memory_mb=500 |
| gemini | live_model, document_model, voice, connect_timeout, request_timeout; explicit verified model IDs at implementation time |
| memory | enabled; local data_dir; share_relevant_context=true; raw_transcript_days=7; activity_days=7; summaries_days=90 |
| indexing | selected_roots; exclusions; enabled=false until roots selected; worker_concurrency=1; disk_quota_mb=1024 |
| observation | enabled=false; camera_enabled=false; interval_seconds=30; capture_on_change=true; paused_on_lock=true |
| notifications | enabled=true; speak_alerts=false; quiet_hours; proactive_daily_limit=4 |
| audio | activation=push_to_talk; optional wake_word; bounded buffers; idle disconnect timeout |

Retention defaults apply to automatically generated history. Explicit memories and active tasks remain until deleted; source extracts remain while their source is included and disk quota permits. Validate directory permissions and prevent selecting the memory database/cache as an ingestion source.

## Dependency policy

Create a lightweight requirement set containing only the runtime/UI, audio I/O, Gemini adapter, credential storage, and basic image dependencies actually used. Put legacy providers/local ML in optional extras and document parsers in a separate extra. Pin a tested lock/constraints set for releases. Measure imports rather than assuming lazy imports save enough memory. Confirm SQLite FTS5 availability at startup and fail the indexing feature clearly if absent.
