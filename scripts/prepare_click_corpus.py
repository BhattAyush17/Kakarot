#!/usr/bin/env python3
"""
prepare_click_corpus.py — bridge recorded click WAVs into the Colab training
notebook's `my_clicks` background-audio folder.

The keyboard/mouse false positives that motivated the custom `hey_kakarot`
model cannot be fixed by phrases: clicks are non-speech, so they must enter
training as *background audio* via `config["background_paths"] = [..., './my_clicks']`
in notebooks/kakarot_training_prepared.ipynb.

This script takes what scripts/record_calibration.py captured and packages it
so the notebook can consume it without manual per-file fiddling:

    ~/kakarot_calibration/negatives/clicks_*.wav
      -> ~/kakarot_calibration/colab/my_clicks/*.wav
      -> ~/kakarot_calibration/colab/my_clicks.zip
      -> ~/kakarot_calibration/colab/manifest.json

It validates every take (16 kHz mono, peak/RMS, clipping), because a clipped
key tick is a buzz and teaches the model the wrong thing.

Usage:
    venv/bin/python3 scripts/prepare_click_corpus.py
    venv/bin/python3 scripts/prepare_click_corpus.py --min-seconds 30
    venv/bin/python3 scripts/prepare_click_corpus.py --zip-only
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SAMPLE_RATE = 16000
CHANNELS = 1
DEFAULT_SRC = Path.home() / "kakarot_calibration" / "negatives"
DEFAULT_OUT = Path.home() / "kakarot_calibration" / "colab"
CLICKS_DIRNAME = "my_clicks"


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)


def probe(path: Path) -> dict:
    with wave.open(str(path), "rb") as w:
        rate, channels, width, frames = (
            w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        )
        raw = w.readframes(frames)
    import array
    samples = array.array("h", raw)
    peak = max((abs(s) for s in samples), default=0) / 32768.0
    if samples:
        acc = sum(float(s) * s for s in samples)
        rms = (acc / len(samples)) ** 0.5 / 32768.0
        clipped = sum(1 for s in samples if abs(s) >= 32700) / len(samples)
    else:
        rms = clipped = 0.0
    return {
        "rate": rate,
        "channels": channels,
        "width_bits": width * 8,
        "frames": frames,
        "seconds": frames / float(rate or 1),
        "peak": round(peak, 4),
        "rms": round(rms, 5),
        "clipped_frac": round(clipped, 5),
    }


def try_convert(path: Path, dest: Path) -> bool:
    """Re-encode to 16 kHz mono with ffmpeg when the take is off-spec."""
    if not shutil.which("ffmpeg"):
        return False
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(path),
         "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dest)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and dest.is_file()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC,
                        help=f"folder holding clicks_*.wav (default: {DEFAULT_SRC})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"staging folder (default: {DEFAULT_OUT})")
    parser.add_argument("--pattern", default="clicks_*.wav",
                        help="glob for click takes (default: clicks_*.wav)")
    parser.add_argument("--min-seconds", type=float, default=20.0,
                        help="refuse to package less than this much audio (default: 20s)")
    parser.add_argument("--max-rms", type=float, default=0.35,
                        help="warn when a take exceeds this RMS (too hot / clipping)")
    parser.add_argument("--min-rms", type=float, default=0.0005,
                        help="warn when a take falls below this RMS (too quiet to matter)")
    parser.add_argument("--convert", action="store_true",
                        help="re-encode off-spec takes with ffmpeg instead of skipping them")
    parser.add_argument("--zip-only", action="store_true",
                        help="reuse the existing staged folder, only rebuild the zip")
    args = parser.parse_args()

    clicks_out = args.out / CLICKS_DIRNAME
    args.out.mkdir(parents=True, exist_ok=True)
    clicks_out.mkdir(parents=True, exist_ok=True)

    if args.zip_only:
        takes = sorted(clicks_out.glob("*.wav"))
    else:
        takes = sorted(args.src.expanduser().glob(args.pattern))
        if not takes:
            eprint(f"FAIL: no takes matching {args.pattern} in {args.src}")
            eprint("      Record them first:")
            eprint("        venv/bin/python3 scripts/record_calibration.py --kind clicks --count 6")
            return 1
        for stale in clicks_out.glob("*.wav"):
            stale.unlink()

    if not takes:
        eprint(f"FAIL: no WAVs staged in {clicks_out}")
        return 1

    manifest_files = []
    total_seconds = 0.0
    problems = []

    for take in takes:
        try:
            info = probe(take)
        except Exception as exc:
            problems.append(f"{take.name}: unreadable ({exc})")
            continue

        off_spec = (info["rate"] != SAMPLE_RATE or info["channels"] != CHANNELS
                    or info["width_bits"] != 16)
        if off_spec:
            converted = clicks_out / take.name
            if args.convert and try_convert(take, converted):
                eprint(f"  converted {take.name}: "
                       f"{info['rate']}Hz/{info['channels']}ch -> 16kHz/mono")
                info = probe(converted)
            else:
                problems.append(
                    f"{take.name}: {info['rate']}Hz/{info['channels']}ch/{info['width_bits']}bit "
                    f"-> need 16kHz mono 16-bit"
                    + ("" if args.convert else " (pass --convert if ffmpeg is installed)")
                )
                continue
        else:
            dest = clicks_out / take.name
            if take.resolve() != dest.resolve():
                shutil.copy2(take, dest)

        flags = []
        if info["clipped_frac"] > 0.01:
            flags.append(f"CLIPPED {info['clipped_frac'] * 100:.0f}%")
        if info["rms"] > args.max_rms:
            flags.append(f"hot rms {info['rms']}")
        if info["rms"] < args.min_rms:
            flags.append(f"quiet rms {info['rms']}")
        if flags:
            problems.append(f"{take.name}: {', '.join(flags)}")

        total_seconds += info["seconds"]
        manifest_files.append({
            "name": take.name,
            "sha256": hashlib.sha256((clicks_out / take.name).read_bytes()).hexdigest(),
            **info,
            "warnings": flags,
        })
        print(f"  {take.name:38s} {info['seconds']:6.1f}s  peak {info['peak']:.2f}  "
              f"rms {info['rms']:.4f}  {'  <-- ' + ', '.join(flags) if flags else ''}")

    if total_seconds < args.min_seconds:
        eprint(f"\nFAIL: only {total_seconds:.1f}s staged, need >= {args.min_seconds:.0f}s.")
        eprint("      More click audio = better rejection. Record more takes.")
        return 1

    zip_path = args.out / f"{CLICKS_DIRNAME}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in sorted(clicks_out.glob("*.wav")):
            zf.write(entry, arcname=f"{CLICKS_DIRNAME}/{entry.name}")

    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "background audio for notebooks/kakarot_training_prepared.ipynb "
                   "(config['background_paths'] -> './my_clicks')",
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "total_seconds": round(total_seconds, 2),
        "n_files": len(manifest_files),
        "problems": problems,
        "files": manifest_files,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    print(f"\nstaged : {clicks_out}  ({len(manifest_files)} takes, {total_seconds:.1f}s)")
    print(f"zip    : {zip_path}  ({zip_path.stat().st_size / 1024:.0f} KiB)")
    print(f"manifest: {args.out / 'manifest.json'}")

    if problems:
        eprint("\nWARNINGS (packaged anyway, but these takes are degraded):")
        for problem in problems:
            eprint(f"  - {problem}")
        eprint("  Re-record the flagged takes for a cleaner corpus.")

    print("\nNext: in the Colab notebook, stage this folder (cell 'Upload YOUR keyboard/mouse")
    print("click recordings'), then run PRE-FLIGHT and the three training cells.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
