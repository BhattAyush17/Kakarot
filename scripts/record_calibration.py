#!/usr/bin/env python3
"""
record_calibration.py — record 16 kHz mono WAVs for wake-word training and calibration.

Produces the three data sets the Colab notebook and the promotion gate need
(see WAKEWORD.md):

  clicks    — recordings of your real keyboard/mouse while you type. These are
              uploaded to Colab as `my_clicks` (background audio) and belong in
              the local `negatives/` calibration folder.
  nearmiss  — adversarial negatives: "hey jarvis", "hey mycroft",
              "okay kakarot", "hey computer". Belong in `negatives/`.
  positive  — held-out "hey kakarot" recordings, DIFFERENT takes/speakers than
              anything used in training. Belong in `positives/`.

Usage:
    venv/bin/python3 scripts/record_calibration.py --kind clicks --count 6
    venv/bin/python3 scripts/record_calibration.py --kind nearmiss --count 5
    venv/bin/python3 scripts/record_calibration.py --kind positive --count 20

Files are written to ~/kakarot_calibration/{negatives,positives} by default
(override with --out). Every take has a 3-2-1 countdown; press Ctrl+C to stop
between takes and keep what was recorded.

The `clicks` takes are ALSO what notebooks/kakarot_training_prepared.ipynb
consumes as `my_clicks` background audio — record those before starting a
Colab training run, then bridge them across with:
    venv/bin/python3 scripts/prepare_click_corpus.py
"""

import argparse
import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
CHANNELS = 1
COUNTDOWN_SECONDS = 3

DEFAULTS = {
    # clicks: no voice, so the voice-tuned 0.15 gain would leave transients far
    # too quiet to act as background negatives. Full gain, longer takes, and
    # the user types continuously rather than speaking.
    "clicks": {"seconds": 10.0, "count": 6, "out": "negatives", "gain": 1.0},
    "nearmiss": {"seconds": 3.0, "count": 5, "out": "negatives", "gain": 0.15},
    "positive": {"seconds": 3.0, "count": 20, "out": "positives", "gain": 0.15},
}
KINDS = DEFAULTS

NEARMISS_PHRASES = ["hey jarvis", "hey mycroft", "okay kakarot", "hey computer"]


def prompt_for_take(kind: str, index: int, total: int) -> str:
    if kind == "clicks":
        return f"[{index}/{total}] TYPE / press keys / click the mouse naturally for {KINDS['clicks']['seconds']:.0f}s"
    if kind == "nearmiss":
        phrase = NEARMISS_PHRASES[(index - 1) % len(NEARMISS_PHRASES)]
        return f"[{index}/{total}] Say clearly: \"{phrase}\" ({KINDS['nearmiss']['seconds']:.0f}s)"
    return f"[{index}/{total}] Say clearly: \"hey kakarot\" ({KINDS['positive']['seconds']:.0f}s)"


def normalize(audio: np.ndarray, gain: float, kind: str) -> np.ndarray:
    """Scale down hot/clipping captures and report level.

    Analogue mics on this machine clip hard (40%+ of samples pinned at full
    scale), which turns a clean key tick into a buzz and poisons the corpus.
    Gain first, then normalise to a peak of ~0.7 only if the take is still hot.
    """
    scaled = np.clip(audio.astype(np.float32) * gain, -32768, 32767)
    peak = float(np.abs(scaled).max()) / 32768.0
    rms = float(np.sqrt((scaled.astype(np.float64) / 32768.0) ** 2).mean())
    clipped = float(np.mean(np.abs(audio.astype(np.float64) / 32768.0) > 0.99))

    if peak > 0.7:
        scaled = scaled * (0.7 / peak)
        rms *= 0.7 / peak
        peak = 0.7

    if clipped > 0.01:
        print(f"  WARNING: {clipped * 100:.0f}% of the raw capture was clipped. "
              f"Lower the mic volume in Settings > Sound, or pass a smaller --gain.", flush=True)
    low_hint = "  (LOW - speak louder / move closer)" if kind != "clicks" and rms < 0.005 else ""
    print(f"  level: peak {peak:.2f}  rms {rms:.3f}{low_hint}", flush=True)
    if kind == "clicks" and rms < 0.002:
        print("  WARNING: very quiet for clicks — check the mic is not muted, "
              "or try --gain 2.0", flush=True)
    return scaled.astype(np.int16)


def record_take(seconds: float, device: int | None, gain: float, kind: str) -> np.ndarray:
    for remaining in range(COUNTDOWN_SECONDS, 0, -1):
        print(f"  recording in {remaining}...", flush=True)
        sd.sleep(1000)
    print("  RECORDING — go!", flush=True)
    frames = int(seconds * SAMPLE_RATE)
    audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", device=device)
    sd.wait()
    audio = normalize(audio, gain, kind)
    print("  saved.", flush=True)
    return audio


def write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())


def next_index(out_dir: Path, prefix: str) -> int:
    existing = list(out_dir.glob(f"{prefix}_*.wav"))
    return len(existing) + 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", choices=sorted(KINDS), required=True,
                        help="clicks (keyboard/mouse), nearmiss (adversarial negatives), positive (hey kakarot)")
    parser.add_argument("--out-subdir", default=None,
                        help="override the destination subfolder (negatives|positives)")
    parser.add_argument("--count", type=int, default=None,
                        help="number of takes (default: 10 clicks, 5 nearmiss per full cycle, 20 positive)")
    parser.add_argument("--seconds", type=float, default=None, help="seconds per take")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: ~/kakarot_calibration/<negatives|positives>)")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input device index (default: system default)")
    parser.add_argument("--gain", type=float, default=None,
                        help="input gain after capture (default: per-kind — 1.0 for clicks, 0.15 for speech; "
                             "this machine's mic clips hard on voice, so lower it if a speech take reports clipping)")
    args = parser.parse_args()

    spec = KINDS[args.kind]
    seconds = args.seconds if args.seconds is not None else spec["seconds"]
    count = args.count if args.count is not None else spec["count"]
    gain = args.gain if args.gain is not None else spec["gain"]
    subdir = args.out_subdir if args.out_subdir is not None else spec["out"]
    out_dir = args.out if args.out is not None else Path.home() / "kakarot_calibration" / subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{args.kind}_{stamp}"
    index = next_index(out_dir, prefix)
    total = index + count - 1

    print(f"kind={args.kind} -> {out_dir} ({count} takes x {seconds:.0f}s, gain={gain}, "
          f"device={args.device or 'default'})")
    print("Speak/act at your normal distance from the mic. Ctrl+C keeps recorded takes.\n")
    try:
        for _ in range(count):
            print(prompt_for_take(args.kind, index, total), flush=True)
            audio = record_take(seconds, args.device, gain, args.kind)
            path = out_dir / f"{prefix}_{index:04d}.wav"
            write_wav(path, audio)
            index += 1
    except KeyboardInterrupt:
        print("\nstopped early — keeping recorded takes.")

    recorded = sorted(out_dir.glob(f"{prefix}_*.wav"))
    print(f"\n{len(recorded)} file(s) in {out_dir}:")
    for path in recorded:
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
