# Product requirements document

## Product outcome

Kakarot is a small resident laptop companion that understands the user's requests and selected work, remembers useful facts across restarts, and helps with tasks at the right time. Gemini supplies remote understanding and speech; the laptop owns persistent state and deterministic actions.

## Primary user journeys

| Journey | Expected experience | Success evidence |
|---|---|---|
| Talk naturally | Hold a shortcut, speak, hear a response, interrupt playback | Working microphone, captions, cancellation, and reconnection |
| Remember a preference | “Remember that project Atlas uses Python 3.12” | Saved locally, editable, recalled after restart with origin |
| Understand a screenshot | Drop an image or ask “What is wrong on my screen?” | Relevant explanation grounded in the actual image |
| Understand a document | Attach a report and ask for decisions or a comparison | Answer cites page/section/sheet references and notes omissions |
| Resume work | “What was I doing on Atlas?” | Current tasks, recent decisions, and relevant source links |
| Set a timer | “Remind me in 20 minutes to check the build” | Durable deadline, notification, optional speech, cancellation |
| Observe a focus session | Enable observation for selected work | Quiet, bounded observations and useful follow-ups |
| Inspect memory | “What do you remember about me?” | Searchable local memory with correction and deletion controls |

## Input scope

| Format | First-release behavior | Explicit limits |
|---|---|---|
| Text, Markdown, code, logs | Local text extraction; line-range citations | No execution of source or scripts |
| Voice | Stream to Gemini; transcripts where available | Internet required for natural-language understanding |
| PNG/JPEG/WebP, screenshots | Resize and send selected images to Gemini | Image details may be lost by resizing; permit a selected crop |
| Text PDF | Local page-level extraction, optional selected page images | No full-document completeness claim if partially processed |
| Scanned PDF | Render selected pages and use Gemini vision | Bounded pages per request, progress and cancellation |
| DOCX | Paragraph/table extraction and heading references | Floating objects and layout may be partial |
| XLSX/CSV/TSV | Read-only row batches with sheet/cell references | No macro execution or formula recalculation; cached values may be stale |
| PPTX | Slide text, notes, and slide references | Charts/graphics require an explicit visual fallback |
| Selected folders | Incremental index and project associations | No silent full-disk crawl; exclusion controls |
| Legacy DOC/XLS/PPT, protected/corrupt files | Explain unsupported state and request conversion/unlock | No silent external converter launch |
| Recorded audio/video and camera | Later bounded adapters | Not part of the first-release completeness claim |

## Memory experience

Explicitly saved facts are durable. Conversation summaries and observations are separate, labelled records. The assistant must distinguish “you told me,” “this file says,” and “I inferred.” A memory can be corrected, scoped to a project, pinned, forgotten, or marked private. “Private” means excluded from cloud context selection, not encrypted automatically.

Local-only memory means no hosted memory store or cloud retrieval service. Relevant excerpts can still leave the device through Gemini requests, as authorized by the user. The UI must display sharing mode and give a global pause switch. Provider-side processing and retention are governed separately by the chosen API/account terms.

## Attention and autonomy

Timers requested by the user are eligible to alert immediately when due, subject to the user's sound settings. Suggestions from observation obey quiet hours, deduplication, and a starting limit of one proactive suggestion per 30 minutes and four per day. These are configurable starting defaults, not validated optimal values. Dismissal lowers repetition. Screen text is evidence, never authority to execute a command.

App launch, memory changes, and timer operations use validated local tools. Destructive file actions, external sending, and arbitrary shell execution are outside the initial tool set. Existing Home Assistant behavior stays opt-in.

## Non-goals for release 1

No claim of universal file understanding, perfect semantic recall, offline Gemini-quality speech, continuous camera intelligence, or reminders while the laptop is powered off. No enterprise multi-user service. No Office automation suite. No full filesystem access solely because a document mentions a path.

## Product success

Release requires the tests in the validation document: sustained operation below the RAM ceiling, usable local retrieval, restart-safe state, accurate citations, bounded background work, and honest degraded modes. Cloud latency is measured separately from local overhead. A warm connection target is not a guarantee during network disruption.
