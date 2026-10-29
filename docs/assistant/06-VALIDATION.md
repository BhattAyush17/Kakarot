# Validation, RAM budget, and release gates

## Measurement contract

MB means decimal megabytes (1 MB = 1,000,000 bytes). Count all Kakarot-owned resident processes and temporary parser/helper processes, including the launcher. Primary conservative release metric: sum of process RSS sampled at 100 ms during bursts and one second during soak. On Linux also record PSS and cgroup peak/current memory where available to expose shared pages and short-lived spikes. RSS can double-count shared pages; report both, but do not switch metrics to hide a failure.

Unrelated existing apps and the OS are not included. A browser/renderer launched as a required Kakarot runtime component is included. The optional external settings browser is measured separately and the combined user-visible cost is reported; if it is required to keep core assistant functionality running, it becomes part of the budget. Include detached owned processes through explicit PID tracking.

The hard release ceiling is 500 MB for supported workflows, not just an average. Idle target is below 200 MB. A sampling watchdog cannot prove absence of microspikes; use OS peak counters/resource isolation in addition. If strict aggregate limiting is unavailable on a platform, disclose the limitation and do not claim enforced compliance.

## Proposed allocation, not measurements

| Component/work | Planning allowance |
|---|---:|
| Backend, SDK, DB connections and bounded context | 100 MB |
| Persona/tray, decoded sprites, audio I/O | 90 MB |
| Additional foreground media/network buffers | 40 MB |
| Temporary extraction worker | 120 MB |
| Contingency for allocator/native library spikes | 150 MB |
| Total envelope | 500 MB |

Idle has no parser and minimal buffers. If the separate launcher remains resident, its cost must be absorbed into these allocations; it is not free headroom. Allocation hypotheses are replaced by S0/S1 observations. Reject/defer a file if it cannot be processed within remaining capacity.

## Responsiveness targets

Measured on the S0 target laptop, with warm local caches and 100,000 indexed chunks unless specified. Record p50/p95 plus maxima and corpus size. These are proposed acceptance targets, not present capabilities.

| Operation | Target |
|---|---|
| UI acknowledgement of typed action | p95 under 100 ms |
| Local memory retrieval | p95 under 150 ms |
| Typed local timer create/cancel | p95 under 200 ms |
| Playback stop after locally detected interruption | p95 under 200 ms |
| Due reminder while awake and app running | p95 within 1 second, max 2 seconds |
| Restart/resume overdue reconciliation | within 5 seconds of service readiness |
| Warm voice: end of utterance to first audio | p95 under 2 seconds on documented network; report cloud/network contribution |
| Local overhead before cloud dispatch | p95 under 250 ms, excluding requested extraction |
| Warm app startup to local ready | under 5 seconds target |
| Idle CPU after stabilization | average below 1% of one logical core over 10 minutes |

Cloud latency failure is diagnosed separately from local blocking; it must still be disclosed as a user-experience limitation. Document ingestion has per-work-unit deadlines and immediate progress/cancel controls rather than a promise that every document completes in seconds.

## Test catalogue

| Test | Procedure and pass evidence |
|---|---|
| T01 | Fresh lightweight environment without ML/legacy SDKs; start UI/backend, use Gemini simulator; import/dependency inventory confirms isolation |
| T02 | Simulate and live-test voice/text, interruptions, slow audio consumer, reconnect, expired session; stale chunks never play after cancellation |
| T03 | Exercise HTTP and WebSocket auth, wrong origin, oversized payload, key errors; inspect public config/log fixtures for credentials and user content |
| T04 | Create/correct/conflict/delete memory; restart between steps; export matches durable state |
| T05 | At least 100 labelled queries over 10 projects: exact facts, paraphrases, conflicts, deleted sources, unanswerable questions; relevant evidence recall@5 at least 90%; zero stale/private evidence; citations resolve for all answered fixture claims |
| T06 | Seed distinctive private/excluded strings; intercept every cloud request including summary and vision routes; assert no leakage |
| T07 | Fake-clock unit tests plus real suspend/resume: deadlines, DST, clock jumps, restart during delivery, cancellation races, duplicate outbox retry; document observable duplicate behavior |
| T08 | Screenshot/image fixture with known content; verify selected capture, crop coordinates, cleanup, permission denial, and lock/pause behavior |
| T09 | Text PDF, scanned PDF, DOCX tables, XLSX formulas/cache, PPTX notes/figures, CSV, Unicode, corrupt and protected inputs; assertions include partial/unsupported states and valid locators |
| T10 | Modify/delete/rename sources during extraction; lose watcher events then reconcile; verify old revisions invalidated and symlink escapes excluded |
| T11 | Simulated observation session with repeated events, quiet hours, lock, pause, dismissed suggestions, quotas; no prohibited capture or notification |
| T12 | Baseline, 30-minute voice, 1-hour ingestion, and 8-hour mixed soak; peak below 500 MB, idle target recorded, final idle within 20 MB of stabilized initial idle |
| T13 | Slow consumer, burst events, parser allocation spike, large image/ZIP, full disk; verify bounded queues, worker termination, usable UI, and intact memory/timers |
| T14 | Benchmark local operations and cold/warm retrieval at 1k/10k/100k chunks with timing samples; use fixed corpus and publish environment |
| T15 | Disconnect internet; local search and typed timer operations still work; voice shows unavailable state; no endless retry loop |
| T16 | Documents containing malicious instructions, external paths, symlinks, macros, and archive bombs; no execution, boundary escape, or tool authority gained |
| T17 | Upgrade existing configs/DB copies, interrupt migration, restore backup; old provider choices preserved; unsupported downgrade blocked clearly |
| T18 | Linux display server/audio/notification/session tests; Windows existing-profile smoke tests where available; limitations listed |
| T19 | Retention expiry and forget purge active rows, FTS, derived summaries and temporary files; backup limitations visible; tombstones prevent reingestion |
| T20 | Duplicate Gemini tool calls/retries and interrupted responses; unique operation IDs prevent repeated side effects; no success statement without result |

## Corpus and release evidence

Use synthetic/non-sensitive fixtures plus user-approved representative local files. Include a 25 MB input, large compressed OOXML input, high-resolution image, source mutation mid-job, and long strings/cells. Maintain fixed expected locators and labelled answers; do not judge recall using only the model's own confidence.

Run foreground voice plus parser activity plus a due timer together. Background work must yield when foreground latency or memory pressure rises. Test database size on disk independently from RAM. Keep raw measurements, environment manifest, dependency lock, test summary, and known limitations in each release evidence directory.

Hardware is not known yet. Before asserting compliance, S0 must record the actual CPU/RAM/display server and S7 must reproduce the supported operating modes there. No benchmark in this document has been executed.
