# Implementation stages and completion stamps

## Sequence and effort

Estimates assume one developer familiar with Python/asyncio and are planning ranges, not deadlines. Hardware/platform discoveries may change them. Critical path: S0 → S1 → S2 → S3 → S4 → S5 → S6 → S7. Total indicative effort: 31–48 working days. S6 can be deferred for an earlier core release, but the full requested observation scope is not complete until it passes.

| Stage | Estimate | Result | Requirements |
|---|---|---|---|
| S0 — Baseline | 1–2 days | Reproducible environment and resource evidence | R14, R22 |
| S1 — Lightweight foundation | 3–5 days | Minimal install, safe startup, bounded runtime | R01, R03, R14–R16, R19, R21, R28 |
| S2 — Gemini conversation | 4–6 days | Interruptible voice/text with tools | R02, R03, R16, R26 |
| S3 — Durable memory | 4–6 days | Local memory and grounded retrieval | R04–R06, R17, R25 |
| S4 — Tasks and reminders | 3–4 days | Durable local actions and notification delivery | R07, R08, R18, R26 |
| S5 — Documents and screenshots | 6–9 days | Broad bounded ingestion and citations | R05, R09–R11, R16, R20, R24 |
| S6 — Observation and attention | 4–6 days | Optional awareness and restrained follow-ups | R12, R13, R27 |
| S7 — Release hardening | 6–10 days | Tested install, migration, recovery, performance | All release requirements |

## S0: Establish the baseline

- Record commit, clean/dirty state, OS/display server, Python version, CPU, RAM, audio devices, and install profile.
- Capture process tree: launcher, backend, persona, helpers; measure startup, idle, voice turn, and screen request separately.
- Inventory existing /session messages and API auth behavior with test fixtures.
- Create performance harness and a small representative file fixture set.
- Record supported Gemini models and SDK version available to the account; do not hard-code a model from an old example.

Exit: baseline report with raw samples and actual integration risks. If current baseline exceeds 500 MB, do not normalize that as acceptable; S1 must remove the cause.

## S1: Make the runtime genuinely lightweight

- Split requirements into lightweight, document, and optional legacy/local profiles.
- Move legacy provider imports behind adapters and make main.py startup capability-based; Gemini mode must skip all local model preloads.
- Replace Whisper-specific readiness with per-capability state (ready/degraded/unavailable).
- Add Gemini configuration/secret storage and migration fixtures; no key in public config.
- Bound persona/chat queues, decoded asset caches, and diagnostic retention.
- Authenticate new APIs and /session; add origin validation and payload bounds.
- Add platform-aware owned-PID lifecycle control; avoid process-name-wide killing.
- Measure launcher and sprite costs, then consolidate the tray/supervisor if needed.

Exit: T01/T03 pass, clean lightweight startup, representative idle measurement, and demonstrated budget headroom. Dependencies absent from the lightweight environment must not cause import failures.

## S2: Add the Gemini transport

- Implement Gemini client plus native-audio session adapter; preserve legacy session branch.
- Add explicit sample-rate conversion and bounded capture/playback buffers.
- Handle interruption, disconnect, session renewal, tool cancellation, and stale chunks.
- Define validated tool schemas and operation IDs; integrate successful results before claiming completion.
- Show connection/capture/cloud-sharing state; expose text fallback and actionable credential errors.

Exit: live microphone demo plus deterministic mocked transport tests; 30-minute conversation soak; no local speech model loaded. API-key-free CI uses a simulator, while a real account smoke test remains necessary.

## S3: Build memory before broad indexing

- Implement SQLite schema/migrations, FTS maintenance, retention, and memory CRUD.
- Add explicit memory tools, correction/conflict semantics, project scopes, and source evidence.
- Implement bounded context builder and private-record filtering.
- Replace RAM-only chat persistence through a storage interface without making UI event delivery depend on slow DB operations.
- Add memory viewer, export/forget controls, and retrieval benchmark.

Exit: remember/restart/recall/correct/forget demonstration; private records absent from captured outbound payloads; benchmark meets T05/T14.

## S4: Implement durable actions

- Add task CRUD and reminder create/list/cancel/snooze tools.
- Implement UTC deadlines, timezone handling, occurrence outbox, resume reconciliation, and retry/deduplication.
- Add native desktop notifications independent of cloud availability.
- Deliver optional speech through a dedicated notification event; do not synthesize fake user turns.
- Add offline typed forms/commands for timers and memory search.

Exit: restart, resume, clock-jump, cancellation, retry, and no-network tests pass. A reminder created twice with the same operation ID remains one reminder.

## S5: Implement multimodal work understanding

- Add file picker/drop ingestion; stream uploads with bounded size checks.
- Introduce isolated parser worker, cancellation, resumable jobs, quotas, and source revisions.
- Implement text/PDF/OOXML/tabular adapters in that order; test format-specific limitations.
- Add capture abstraction and selected-image/page Gemini requests.
- Add selected-folder watchers with reconciliation and exclusion settings.
- Return citations and partial-processing notices through the UI; make related-project retrieval usable.

Exit: format matrix fixtures pass, old revisions never masquerade as current, malicious/oversized files remain bounded, and voice/timers stay responsive during ingestion.

## S6: Add ambient behavior carefully

- Add visible opt-in observation session, pause, lock detection, and selected-app/root scope.
- Use low-frequency change detection and bounded summaries; never persist every frame.
- Add quiet hours, suggestion quotas, duplicate suppression, and user dismissal feedback.
- Profile optional wake-word engine; allow unload and retain push-to-talk.
- Camera is a separate later substage S6C: single-frame requests first, visible indicator, same budget and retention checks. Do not mark camera complete with screen support alone.

Exit: T11 and combined-mode RAM tests pass; no network/capture activity after pause or lock. S6C is optional unless the user chooses literal camera-based awareness.

## S7: Release and regression gates

- Run mixed-workload soak, parser adversarial fixtures, auth/privacy checks, and migration/backup recovery.
- Validate Linux display/audio/notification integration; run available Windows legacy smoke checks.
- Pin tested dependencies, publish reproducible startup instructions and troubleshooting.
- Record unsupported modes honestly; benchmark enabled wake word/observation separately and together.
- Produce release evidence manifest and known limitations; ensure all MUST rows have results.

Exit: all required checks pass; no undocumented RAM overrun. Do not declare completion based on unit tests alone.

## Suggested PR boundaries

PR01 baseline harness; PR02 dependency/startup isolation; PR03 config/auth/runtime bounds; PR04 Gemini adapter/protocol; PR05 memory schema/retrieval; PR06 memory controls; PR07 reminders/notifications; PR08 extraction worker/text/PDF; PR09 Office/table adapters; PR10 source watcher/citations; PR11 observation/attention; PR12 release profiling/packaging. Keep each independently reviewable with migration and rollback notes where relevant.

## Completion stamp template

Status values: NOT_STARTED, IN_PROGRESS, BLOCKED, VERIFIED. All stages are currently NOT_STARTED; preparing this plan does not count as implementation.

```text
Stage: Sx
Status:
Implementation commit:
Environment / dependency lock:
Requirement IDs:
Test IDs and result paths:
RAM peak / idle / measurement method:
Latency and corpus:
Live integration checks:
Known limitations / accepted deferrals:
Rollback verified:
Reviewer / date:
Next stage authorized by passed gate:
```

## First implementation task

Run S0, then implement S1. Do not begin with full-folder ingestion or cosmetic UI work. The first reviewable code change should expose baseline measurements and remove unconditional speech-model loading from the new lightweight profile without breaking existing profiles.
