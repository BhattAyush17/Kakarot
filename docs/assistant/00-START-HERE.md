# Kakarot personal assistant — implementation pack

Version 1.0 · 26 September 2026 · Status: proposed implementation baseline

Build on the existing Python/FastAPI/PySide6 Kakarot project. Use Gemini for conversation and multimodal understanding; keep the authoritative memory, file index, tasks, and reminders on the laptop. Target idle memory below 200 MB and a total application ceiling of 500 MB.

This is a planning deliverable, not an implemented or performance-verified release. No application source was modified. Requirements marked MUST are proposed release gates; estimates and component memory allocations are engineering hypotheses.

## Reading order

1. [PRD](01-PRD.md): experience, scope, supported inputs, and product boundaries.
2. [Requirements](02-REQUIREMENTS.md): traceable functional and operational requirements.
3. [Architecture](03-ARCHITECTURE.md): components, flows, repository integration, and decisions.
4. [Memory and ingestion](04-MEMORY-AND-INGESTION.md): data model, retrieval, retention, and file processing.
5. [Delivery stages](05-DELIVERY-PLAN.md): implementation sequence, work packages, dependencies, and completion stamps.
6. [Verification and performance](06-VALIDATION.md): RAM/latency budgets and test protocols.
7. [Operations and risks](07-OPERATIONS-AND-RISKS.md): installation, failure behavior, migration, and open decisions.
8. [Inspection evidence](08-BASELINE-AND-SOURCES.md): observed source behavior and external references.

## Agreed product constraints

- Broad input: voice, text, screenshots, documents, and selected work folders.
- Effective long-term memory sourced from the user's local work and interactions.
- Local authoritative storage; relevant retrieved context and images may be sent to Gemini.
- Timers, notifications, spoken interaction, and optional task observation.
- 200–500 MB RAM envelope across Kakarot-owned processes; 500 MB is the release ceiling.
- Extend the existing project rather than replacing it wholesale.

## Planning assumptions

Linux on the current laptop is the first validation target; its distribution, display server, CPU, and available RAM must be recorded in Stage S0. Preserve existing Windows behavior through regression testing. Push-to-talk is the first supported activation mode. Wake word, camera, and proactive observation are separately enabled capabilities and must fit the same resource ceiling when enabled.

The first release supports understanding and discussing files. Arbitrary editing of Office documents, unrestricted computer control, full video transcription, and autonomous external messaging are separate future scopes. No cloud vector database, local large language model, or local embedding model is required.

## Recommended delivery decision

Prove the runtime envelope before building broad ingestion. Deliver a walking skeleton with Gemini voice, one persistent memory, and a restart-safe timer, then add source-grounded document retrieval and observation. If the existing UI cannot fit the envelope, optimize its asset loading and launcher lifecycle first; evaluate a simpler native tray UI only after measured evidence.

## Using this pack

Use requirement IDs in issues and PR descriptions. Advance a stage only after its completion stamp links to actual checks and measurements. Update these documents when a decision changes; do not treat illustrative module names as existing code. Suggested eventual repository destination: docs/assistant/.
