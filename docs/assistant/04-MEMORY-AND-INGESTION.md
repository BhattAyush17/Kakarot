# Long-term memory and input processing

## Storage design

Use one local SQLite database in a per-user application data directory, outside the repository and watched roots. Enable foreign keys, migrations, bounded page cache, busy timeout, and WAL where supported. Use short transactions and a serialized writer; readers have bounded connections. Do not retain whole database result sets in memory. SQLite FTS5 supplies local full-text indexing and ranking. [SQLite FTS5 reference](https://www.sqlite.org/fts5.html)

Suggested tables (schema to finalize in S3):

| Table | Essential fields and constraints |
|---|---|
| projects | id, name, root associations, created_at |
| sources | id, canonical_uri, kind, fingerprint, revision, status, modified_at, last_seen_at; unique canonical URI |
| chunks | id, source_id, source_revision, ordinal, locator_json, text, content_hash; indexed source/revision |
| memories | id, kind, text, project_id, confidence, confirmed, private, pinned, created_at, updated_at, expires_at, supersedes_id |
| evidence_links | memory_id, source_id or interaction_id, source_revision, locator_json; explicit provenance |
| interactions | id, role, text, session_id, created_at, expires_at |
| tasks | id, title, project_id, status, due_at, source_memory_id, updated_at |
| reminders | id, task_id, due_at_utc, timezone, recurrence_json, state, operation_id unique |
| notification_outbox | id, reminder_id, occurrence_key unique, state, attempt_count, next_attempt_at |
| jobs | id, source_id, kind, state, cursor, error_code, updated_at |
| operations | operation_id unique, tool_name, arguments_hash, result_json, state |
| schema_migrations | version, applied_at |

FTS indexes cover searchable memory and chunk text, maintained in the same transaction as content changes. Source revisions prevent old extracts being presented as current. A source deletion invalidates linked extracts and flags derived summaries; explicit user-confirmed memories are retained with a “source unavailable” label unless separately forgotten.

## Write lifecycle

1. Validate origin, schema, size, project scope, and sharing policy.
2. Explicit “remember” commands create confirmed facts. Automatic extraction creates candidates or labelled summaries, not confirmed personal truths.
3. Detect exact duplicates by normalized content hash. Similarity heuristics may suggest a merge but must not silently merge contradictory facts.
4. Store provenance and timestamps transactionally with the FTS entry.
5. Corrections supersede old records. Display conflicting current evidence rather than choosing solely by recency.
6. Consolidate recent interactions in bounded batches when idle; if cloud summarization is disabled/unavailable, preserve a small local extractive summary or postpone the job.

## Retrieval lifecycle

- Parse project, date, source type, and explicit entity filters locally.
- Query FTS with escaped user terms and parameterized SQL; support exact names and configured aliases.
- Rank by lexical relevance, project match, confirmation, and recency. Diversity limits prevent a single file crowding out all results.
- Return at most eight evidence items initially, with a default 4,000-token retrieved-context allowance and a 12,000-token total application context target. These are application budgets, independent of larger model limits.
- Exclude private, expired, superseded, invalid-revision, and out-of-scope items before building a cloud request.
- Gemini may request a second bounded lookup if evidence is insufficient. Allow at most two retrieval rounds per turn initially.
- Answers cite source locators. Missing evidence produces an explicit uncertainty statement, not an invented memory.

Lexical search misses paraphrases. Evaluate with a local benchmark before adding complexity: alias expansion and bounded Gemini query rewriting are possible next steps. Do not introduce a resident embedding model or send the whole index to a cloud embedding service without revisiting requirements.

## Ingestion pipeline

Select root/file → canonicalize and check exclusions → inspect type/size → enqueue one job → extract in bounded worker → attach locators → commit revision → make searchable.

Use file events plus a throttled reconciliation pass; do not assume watchers never lose events. Debounce changing files and retry only after stable metadata. Compare revision/fingerprint again before commit. Handle symlinks by resolving targets and enforcing selected-root boundaries. Keep job queue state on disk with bounded in-memory dispatch.

Initial policy limits, to tune through S5 tests:

| Resource | Initial bound | Behavior at limit |
|---|---|---|
| Queued job descriptors in RAM | 100 | Remaining jobs stay in SQLite |
| Concurrent extraction workers | 1 | Serialize document work |
| Individual file admitted | 25 MB | Offer explicit page/range selection or conversion |
| PDF work unit | 20 pages; one rendered page at a time | Continue through resumable jobs, never render all pages together |
| Text chunk | About 800 tokens, 100-token overlap | Stable page/section locators |
| Extracted text per source | 5 MB | Mark partial; allow targeted ranges |
| XLSX/CSV batch | 1,000 rows, bounded cell length | Stream batches; cap per-job work |
| Image input | 12 megapixels decoded; downscale to 1280-pixel long edge | Reject decompression bombs; allow targeted crop |
| Parser deadline | 30 seconds per work unit | Terminate worker, record retryable/partial state |
| Expanded ZIP/XML bytes | 50 MB and bounded entry count | Reject archive bomb before full decompression |
| Local index quota | 1 GB default | Evict reconstructible extracts or pause; never silently delete explicit memories |

Limits do not guarantee the RAM ceiling alone. Native parser allocations need OS-enforced worker limits, read-only file handles where practical, and total-process accounting. Parser candidates include a bounded PDF library, streaming XML for OOXML, and read-only spreadsheet iteration; selection requires license review and measured fixtures. Do not install LibreOffice or a browser engine as an invisible fallback.

## Visual and tabular fidelity

Text extraction cannot fully represent diagrams or charts. For a question about a figure, render/send only the relevant page or ask for a screenshot. DOCX references should use heading/paragraph/table IDs unless a verified renderer provides page numbers. XLSX references retain sheet and cell ranges; distinguish formula text, cached values, and missing cache. PPTX references identify slides and shapes; do not claim embedded charts were understood from slide text alone.

## Retention and forgetting

Default raw transcript retention: seven days; observations: seven days; automatic summaries: 90 days. Explicit memories and active tasks persist. Raw audio, screenshots, and rendered pages are temporary and deleted after processing/cancellation by default. Logs contain event metadata, not user content.

Forget removes active content, evidence-derived summaries as appropriate, FTS entries, and cached copies in a transaction/job sequence with visible completion status. Purge resumable jobs that could recreate deleted content; use tombstones for excluded sources. Checkpoint and database maintenance should be scheduled without blocking interaction. Logical deletion is not a guarantee of forensic erasure from SSDs or old backups. User-managed backups require separate deletion; explain this in the memory controls.

## Cloud boundary

Send selected excerpts or images directly for current reasoning; no cloud memory synchronization and no whole-folder upload. Keep an ephemeral outbound manifest (source IDs and sizes, not content) for diagnostics. A private memory can be displayed in local search but never passed to Gemini. Current voice input necessarily leaves the device when Gemini voice is used. Provider retention is separate from local retention.
