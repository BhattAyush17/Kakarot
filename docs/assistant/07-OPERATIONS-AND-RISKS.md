# Operations, migration, and risk register

## Installation and startup

Provide a documented Linux virtual-environment install and existing Windows path. Resolve the interpreter using platform-aware paths, not an assumed venv/Scripts/python.exe. Add tested desktop shortcut/autostart instructions only after manual startup works. Store mutable state in the OS per-user data directory with restrictive permissions; do not write runtime data into the source tree by default.

First-run flow: choose Gemini → save key securely → check connectivity/model capability → test microphone/speaker → choose optional memory/folder settings → show sharing policy → start push-to-talk. Selecting folders is separate from enabling screen observation. Keyring absence produces an actionable error or explicitly selected session-only key input; never silently write plaintext secrets to JSON.

Separate readiness into local runtime, audio devices, Gemini connection, memory DB, notification provider, and optional indexer. A Gemini outage must not prevent timers from starting. One owned backend instance avoids duplicate scheduler delivery; use an instance lock and transactional outbox claims.

## Failure behavior

| Failure | Required behavior |
|---|---|
| Network/API/quota failure | Explain cloud unavailability; bounded backoff; keep local features live |
| Microphone/speaker missing | Offer typed interaction and visual notifications |
| Parser crash/timeout | Preserve job status, terminate worker, allow bounded retry/range selection |
| Memory pressure | Pause background work, drop reconstructible buffers, stop worker, record limit event |
| Disk full | Reject new persistence explicitly; preserve existing state; never claim a memory/timer was saved |
| DB corruption | Stop writes, keep original file, offer restore; do not silently replace it with an empty DB |
| Screen permission denied | Explain platform capture limitation; allow attached screenshot |
| Laptop sleep/power off | Reconcile due reminders when running again; do not promise off-device alerts |
| Keyring locked | Keep local features available and request unlock through normal UI |

## Linux and platform validation

Determine X11 versus Wayland in S0. Capture, global shortcuts, and active-window metadata may require different OS interfaces. On Wayland, prototype portal-based capture/shortcuts and validate permissions/session lifetime on the actual desktop; do not assume existing mss/pynput behavior carries over. Do not evade platform restrictions by escalating privileges. Native notifications need a tested adapter. Windows remains a compatibility track, not evidence that Linux works.

## Migration and rollback

1. Back up existing config and any local state before changing schema/version.
2. Add the new profile without silently changing existing users' selected providers.
3. Apply ordered transactional migrations; record version and refuse incompatible newer schemas.
4. Run lightweight startup smoke tests and preserve old profile selection where its extras are installed.
5. For rollback, stop the app, retain the failed new database for diagnosis, restore the matching config/database backup, and restart the previous version.

Do not run old code against a newer schema or overwrite user changes made after the backup. Document potential data loss when restoring older snapshots. Database backup must use SQLite's backup facilities or a quiesced consistent copy, not a lone copy of a live WAL database file.

## Local data and API policy

Local files and conversations remain the memory source of truth. Gemini receives current voice and selected evidence; this is not a fully offline system. Do not promise zero cloud retention from the app's local-memory design. Verify the selected API/account terms at implementation and expose a concise policy link. No cloud Files API upload is required by this baseline; introducing one needs lifecycle/deletion design and a visible setting.

Treat attached files and screen text as untrusted input. Parser processes do not execute macros, shell commands, or document instructions. The dispatcher uses allowlisted tools and validated arguments. Audit operation metadata, not full personal content. OS file permissions and keyring are baseline protection; database encryption is a separate decision, not implied by SQLite.

## Risk register

| Risk | Impact | Mitigation / gate | Owner role |
|---|---|---|---|
| Qt assets/launcher already exceed budget | 500 MB target infeasible without UI changes | S0/S1 profiling; lazy sprite decode, lifecycle changes | Runtime engineer |
| Native parser memory spike | App instability | OS worker limit, admission control, page/range limits | Ingestion engineer |
| Gemini model/SDK changes | Voice breakage | Configurable verified model IDs, pinned SDK, live smoke test | Provider engineer |
| Network latency/cost | Slow or expensive always-on use | Push-to-talk default, idle disconnect, usage counters, observation quotas | Product/runtime |
| Lexical retrieval misses paraphrases | Poor long-term recall | Labelled benchmark, aliases, bounded query expansion before embeddings | Memory engineer |
| Outdated/conflicting memories | Wrong assistance | Source revisions, evidence labels, correction UI | Memory engineer |
| Wayland limitations | Missing capture/shortcuts | Early target-desktop spike and attachment fallback | Platform engineer |
| Notification duplicate/crash race | Repeated or missed reminder | Durable outbox, deduplication and explicit delivery semantics | Runtime engineer |
| Existing eager imports | Minimal install fails | Fresh-env test with optional dependencies absent | Runtime engineer |
| Privacy filter applied only to chat | Context leaks via summaries/images | Central outbound policy and interception tests | Security reviewer |
| Broad “any input” scope | Unbounded implementation | Published support matrix and partial-result contract | Product owner |

## Decisions to validate during implementation

- Actual laptop hardware and display server; supported secondary platforms.
- Verified Gemini model IDs, account quota, and user-selected usage limits.
- Whether the existing animated pet fits the idle target or should offer a minimal tray mode.
- Parser libraries after memory/license testing.
- Whether optional wake word and camera are required for the first release.
- Quiet hours/timezone defaults and disk quota suitability.

None prevents preparing this plan. They are explicit S0/S5/S6 decisions; do not invent benchmarks or API costs to close them.
