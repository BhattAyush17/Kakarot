# Stage S0 baseline

Status: IN_PROGRESS

Recorded 27 September 2026 at commit `dcf1b69ba736745fde092c72bd28786b12e3e70c` before implementation changes.

## Environment

- Fedora Linux 44 Workstation, Wayland session.
- Python system interpreter: 3.14.7. Lightweight test environment: CPython 3.12.13.
- Physical memory reported by the OS: 6,031,597,568 bytes; swap: 6,031,405,056 bytes.
- Existing repository was clean before this work began.
- Graph generation used for discovery was current and reported no parse-partial or skipped source files. Generated environments and binary assets are intentionally excluded.

## Confirmed baseline risks

- Existing cloud startup preloaded Whisper; local startup preloaded Whisper, Kokoro and LM Studio.
- The original core dependency set installed local ML and all provider SDKs together.
- Existing provider dispatch imported legacy adapters eagerly.
- The launcher and process control assumed a Windows virtual-environment layout.
- The target desktop uses Wayland, so capture and global-shortcut behavior still needs a live platform test.
- Existing session injection and playback buffers needed explicit bounds.

## Measurement state

The repository now includes `scripts/measure_memory.py` to aggregate RSS and PSS across an owned process tree. Import/startup measurements are recorded during S1 verification. Full launcher/persona, microphone, Gemini live, screen capture, and mixed-workload measurements remain open because they require an interactive desktop/audio device and a configured Gemini API key.

The 200 MB idle target and 500 MB peak ceiling remain release gates; this document does not claim they have passed.

### First lightweight import sample

The backend import-only workload was sampled every 100 ms for about four seconds in the isolated Python 3.12 environment. Peak aggregate RSS was 61,554,688 bytes and peak PSS was 52,581,376 bytes. This is useful evidence that the backend module graph can stay small, but it is not an idle application result because no server, persona, audio stream, or Gemini connection was active.

### Lightweight combined sample after sprite work

An eight-second offscreen run with the FastAPI backend and animated persona, cloud connections disabled, peaked at 227,930,112 bytes aggregate RSS and 197,423,104 bytes aggregate PSS. The backend accounted for about 74.8 MB RSS and the persona about 149.5 MB RSS. This passes the 500 MB ceiling and the 200 MB PSS target in this synthetic run; it remains above the proposed 200 MB RSS idle target.

This sample does not include microphone activity, a live Gemini SDK connection, desktop compositor costs, document extraction, or the launcher. A real Wayland desktop soak remains required before S0/S1 can be marked VERIFIED.
