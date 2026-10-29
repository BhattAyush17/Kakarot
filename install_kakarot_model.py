#!/usr/bin/env python3
"""
install_kakarot_model.py — local promotion gate + atomic install for a
Colab-trained hey_kakarot.onnx.

Run this on the machine where kakarot_daemon is importable, NOT in Colab.
Colab has no visibility into data_dir, so directory-safety and calibration
must happen here, after download.

Usage:
    venv/bin/python3 install_kakarot_model.py \\
        --model ~/Downloads/hey_kakarot.onnx \\
        --negatives-dir ~/kakarot_calibration/negatives \\
        --positives-dir ~/kakarot_calibration/positives \\
        --threshold 0.5 \\
        --vad-threshold 0.5 \\
        --max-events-per-hour 1.0

negatives-dir should contain 16kHz mono WAVs of: mechanical keyboard/mouse
clicks, near-miss phrases ("hey jarvis", "hey mycroft", "okay kakarot",
"hey computer"), fan/HVAC hum, music, speech babble, and silence.

positives-dir should contain held-out "hey kakarot" clips NOT used in
training (different speakers/takes than the Colab training corpus).

Exit codes: 0 promoted, 1 environment/usage error, 2 gate failed,
3 install failed.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FRAME_SIZE = 1280        # 80 ms @ 16kHz, must match the listener
SAMPLE_RATE = 16000
REFRACTORY_SECONDS = 3.0  # matches the listener's post-wake cooldown
CONSECUTIVE_FRAMES = 3    # consecutive hits required before a wake fires

EXIT_ENV = 1
EXIT_GATE = 2
EXIT_INSTALL = 3


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)


def resolve_data_dir() -> Path:
    """Resolve data_dir the same way the daemon does — never hardcode ~."""
    try:
        from kakarot_daemon.config import KakarotDaemonConfig
    except ImportError:
        eprint("FATAL: kakarot_daemon is not importable in this environment.")
        eprint("Run this script with the daemon's own interpreter:")
        eprint("    venv/bin/python3 install_kakarot_model.py --help")
        sys.exit(EXIT_ENV)

    data_dir = Path(KakarotDaemonConfig.from_environment().data_dir)
    return data_dir.expanduser().resolve()


def read_wav_16k_mono(path: Path) -> np.ndarray:
    """Read a 16kHz/mono/16-bit WAV as an int16 numpy array.

    Model.predict() requires numpy (it reads x.shape); passing a list or
    array.array raises AttributeError.
    """
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if rate != SAMPLE_RATE:
            eprint(f"FAIL: {path.name} is {rate} Hz, expected {SAMPLE_RATE}.")
            eprint("      Convert first:  ffmpeg -i in.wav -ac 1 -ar 16000 out.wav")
            sys.exit(EXIT_ENV)
        if channels != 1:
            eprint(f"FAIL: {path.name} has {channels} channels, expected mono.")
            eprint("      Convert first:  ffmpeg -i in.wav -ac 1 -ar 16000 out.wav")
            sys.exit(EXIT_ENV)
        if width != 2:
            eprint(f"FAIL: {path.name} sample width {width * 8}-bit, expected 16-bit.")
            sys.exit(EXIT_ENV)
        raw = w.readframes(w.getnframes())

    return np.frombuffer(raw, dtype="<i2")


def load_model(onnx_path: Path, vad_threshold: float):
    """Instantiate openWakeWord exactly as the listener will.

    openwakeword 0.4.0 forwards unknown kwargs into AudioFeatures, so extra
    kwargs (e.g. inference_framework=) raise TypeError. Do not add them.
    """
    from openwakeword.model import Model
    return Model(wakeword_model_paths=[str(onnx_path)], vad_threshold=vad_threshold)


def frame_scores(model, samples: np.ndarray, model_key: str) -> np.ndarray:
    """Score a clip frame-by-frame, mirroring the listener's inference loop."""
    scores = []
    for i in range(0, len(samples) - FRAME_SIZE + 1, FRAME_SIZE):
        prediction = model.predict(samples[i:i + FRAME_SIZE])
        if isinstance(prediction, dict):
            if model_key not in prediction:
                raise SystemExit(
                    f"FAIL: prediction keys are {sorted(prediction)}, expected '{model_key}'.\n"
                    f"      The runtime score key is the model filename stem — rename the\n"
                    f"      file to hey_kakarot.onnx before promoting."
                )
            scores.append(float(prediction[model_key]))
        else:
            scores.append(float(prediction))
    return np.asarray(scores, dtype=np.float64)


def count_wake_events(scores: np.ndarray, threshold: float,
                      consecutive: int = CONSECUTIVE_FRAMES) -> int:
    """Count wake events the way the listener fires them.

    Requires `consecutive` frames at/above threshold, then ignores
    everything inside a refractory window. A single noisy second can trip
    dozens of raw frames; one human utterance should be one event.
    """
    if scores.size == 0:
        return 0
    above = scores >= threshold
    refractory_frames = int(REFRACTORY_SECONDS * SAMPLE_RATE / FRAME_SIZE)

    events = 0
    run = 0
    cooldown = 0
    for hit in above:
        if cooldown > 0:
            cooldown -= 1
            run = 0
            continue
        run = run + 1 if hit else 0
        if run >= consecutive:
            events += 1
            cooldown = refractory_frames
            run = 0
    return events


def synth_keyboard_clicks() -> np.ndarray:
    """Synthetic mechanical-key transient train for the VAD gate self-test.

    Sharp exponential-decay impulses are acoustically crude but they are
    exactly the class of non-speech transient the reported false positives
    came from, so the VAD gate must suppress them.
    """
    rng = np.random.default_rng(0xC0FFEE)
    samples = rng.normal(0, 0.002, SAMPLE_RATE * 3).astype(np.float64)
    for onset in range(0, len(samples) - 400, 4800):  # ~12 clicks/sec
        decay = np.exp(-np.arange(400) / 40.0)
        samples[onset:onset + 400] += decay * rng.uniform(0.5, 0.95)
    peak = np.abs(samples).max() or 1.0
    return (samples / peak * 0.9 * 32767).astype(np.int16)


def run_promotion_gate(onnx_path: Path, positives_dir: Path, negatives_dir: Path,
                       threshold: float, vad_threshold: float,
                       max_events_per_hour: float, min_recall: float,
                       force: bool):
    """Sweep held-out positives + negatives. Returns (results, failures)."""
    model_key = onnx_path.stem  # runtime score key == filename stem

    gated = load_model(onnx_path, vad_threshold)

    results = {
        "model_key": model_key,
        "threshold": threshold,
        "vad_threshold": vad_threshold,
        "frame_size": FRAME_SIZE,
        "sample_rate": SAMPLE_RATE,
        "refractory_seconds": REFRACTORY_SECONDS,
        "consecutive_frames": CONSECUTIVE_FRAMES,
    }
    failures = []

    # ---- silence sanity, VAD OFF ---------------------------------------
    # With VAD on this is vacuous (the gate returns 0 for non-speech), so
    # validate the raw model against true digital silence.
    ungated = load_model(onnx_path, 0)
    silence = np.zeros(FRAME_SIZE * 20, dtype=np.int16)
    silence_scores = frame_scores(ungated, silence, model_key)
    silence_score = float(silence_scores.max()) if silence_scores.size else 1.0
    results["silence_score_max_vad_off"] = silence_score
    if silence_score >= 0.05:
        failures.append(
            f"silence score {silence_score:.4f} (VAD off) is not ~0 — model is "
            f"not behaving; do not promote"
        )

    # ---- VAD gate must suppress mechanical transients -------------------
    click_scores = frame_scores(gated, synth_keyboard_clicks(), model_key)
    click_peak = float(click_scores.max()) if click_scores.size else 1.0
    click_events = count_wake_events(click_scores, threshold)
    results["synthetic_click_score_max"] = click_peak
    results["synthetic_click_events"] = click_events
    if click_events > 0:
        failures.append(
            f"VAD gate leaked: synthetic keyboard-click train produced "
            f"{click_events} wake event(s) (peak {click_peak:.3f}) — raise "
            f"vad_threshold or retrain with click negatives"
        )

    # ---- recall on held-out positives ----------------------------------
    pos_files = sorted(positives_dir.glob("*.wav"))
    if not pos_files:
        eprint(f"FAIL: no .wav files in positives-dir: {positives_dir}")
        sys.exit(EXIT_ENV)

    hits = 0
    for f in pos_files:
        gated.reset()
        scores = frame_scores(gated, read_wav_16k_mono(f), model_key)
        if scores.size and count_wake_events(scores, threshold) > 0:
            hits += 1
    recall = hits / len(pos_files)
    results["recall_on_holdout"] = recall
    results["n_positive_clips"] = len(pos_files)
    if recall < min_recall:
        failures.append(
            f"recall {recall:.3f} below required {min_recall:.2f} on "
            f"{len(pos_files)} held-out clips — the phrase will not wake reliably"
        )

    # ---- false positives on the negative corpus -------------------------
    neg_files = sorted(negatives_dir.glob("*.wav"))
    if not neg_files:
        eprint(f"FAIL: no .wav files in negatives-dir: {negatives_dir}")
        eprint("      This corpus is what fixes the keyboard-click false positive;")
        eprint("      it cannot be empty.")
        sys.exit(EXIT_ENV)

    total_negative_seconds = 0.0
    events = 0
    peak = 0.0
    for f in neg_files:
        samples = read_wav_16k_mono(f)
        total_negative_seconds += len(samples) / SAMPLE_RATE
        gated.reset()
        scores = frame_scores(gated, samples, model_key)
        if scores.size:
            peak = max(peak, float(scores.max()))
            events += count_wake_events(scores, threshold)

    hours = max(total_negative_seconds / 3600.0, 1e-9)
    events_per_hour = events / hours
    results["false_wakes_per_hour"] = events_per_hour
    results["negative_peak_score"] = peak
    results["n_negative_clips"] = len(neg_files)
    results["negative_corpus_hours"] = round(hours, 3)
    if events_per_hour > max_events_per_hour:
        failures.append(
            f"{events_per_hour:.2f} false wakes/hr exceeds bound "
            f"{max_events_per_hour}/hr over {hours:.2f}h of negatives — retrain "
            f"with a larger negative corpus / higher negative weight"
        )

    return results, failures


def introspect_onnx(path: Path) -> dict:
    """Record the real ONNX graph signature instead of assuming it."""
    try:
        import onnxruntime as ort
    except ImportError:
        return {}

    try:
        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    except Exception as exc:
        return {"introspection_error": str(exc)}

    def describe(io):
        return [
            {
                "name": item.name,
                "shape": [d if isinstance(d, int) else str(d) for d in item.shape],
                "type": item.type,
            }
            for item in io
        ]

    return {"inputs": describe(sess.get_inputs()), "outputs": describe(sess.get_outputs())}


def atomic_install(src_onnx: Path, data_dir: Path, model_key: str, metadata: dict):
    """Stage → re-load → scrub → atomic rename. Never leave a partial model live."""
    models_dir = data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(models_dir, 0o700)  # mkdir's mode is umask-affected; force it

    final_path = models_dir / f"{model_key}.onnx"
    tmp_path = models_dir / f"{model_key}.onnx.tmp"
    sidecar = models_dir / f"{model_key}.json"
    tmp_sidecar = models_dir / f"{model_key}.json.tmp"

    blob = src_onnx.read_bytes()

    # Scrub BEFORE anything becomes live: a locally-trained model can embed
    # the trainer's home path in ONNX producer metadata.
    home = str(Path.home())
    if home != "/" and home.encode() in blob:
        tmp_path.write_bytes(blob)
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"ONNX contains the local home path ({home}); retrain in Colab or "
            f"strip producer metadata before promoting"
        )
    serialised = json.dumps(metadata)
    if home != "/" and home in serialised:
        raise RuntimeError("sidecar metadata contains an absolute home path")

    # Stage in the destination directory so os.replace() is a same-fs rename.
    tmp_path.write_bytes(blob)
    os.chmod(tmp_path, 0o600)

    try:
        load_model(tmp_path, metadata["vad_threshold"])
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"re-load of staged model failed, aborting install: {exc}")

    tmp_sidecar.write_text(json.dumps(metadata, indent=2, sort_keys=True))
    os.replace(tmp_path, final_path)
    os.replace(tmp_sidecar, sidecar)

    return final_path, sidecar


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--model", required=True, type=Path,
                    help="Colab-downloaded hey_kakarot.onnx")
    ap.add_argument("--positives-dir", required=True, type=Path,
                    help="held-out 'hey kakarot' WAVs, NOT used in training")
    ap.add_argument("--negatives-dir", required=True, type=Path,
                    help="keyboard clicks, near-miss phrases, hum, music, babble")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--vad-threshold", type=float, default=0.5)
    ap.add_argument("--max-events-per-hour", type=float, default=1.0,
                    help="measured with the listener's refractory window, not raw frames")
    ap.add_argument("--min-recall", type=float, default=0.8)
    ap.add_argument("--positive-corpus-note", default="",
                    help="free-text corpus note stored in the sidecar")
    ap.add_argument("--force", action="store_true",
                    help="install even if a gate fails (records gate_failures in sidecar)")
    ap.add_argument("--restart", action="store_true",
                    help="systemctl --user restart kakarot-ears.service after installing")
    args = ap.parse_args()

    model_path = args.model.expanduser().resolve()
    if not model_path.is_file():
        eprint(f"FAIL: no such model file: {model_path}")
        sys.exit(EXIT_ENV)
    if model_path.stem != "hey_kakarot":
        eprint(f"WARNING: filename stem is '{model_path.stem}', so the runtime score")
        eprint(f"         key will be '{model_path.stem}', not 'hey_kakarot'.")
        eprint("         Rename it if the listener expects hey_kakarot.onnx.")

    data_dir = resolve_data_dir()
    print(f"Resolved data_dir : {data_dir}")
    print(f"Model             : {model_path} ({model_path.stat().st_size / 1024:.0f} KiB)")

    print("\n=== Promotion gate (loads the model exactly as the listener will) ===")
    results, failures = run_promotion_gate(
        model_path,
        args.positives_dir.expanduser().resolve(),
        args.negatives_dir.expanduser().resolve(),
        args.threshold,
        args.vad_threshold,
        args.max_events_per_hour,
        args.min_recall,
        args.force,
    )

    print(f"  model_key                  : {results['model_key']}")
    print(f"  silence score (VAD off)    : {results['silence_score_max_vad_off']:.4f}")
    print(f"  synthetic click score      : {results['synthetic_click_score_max']:.4f} "
          f"({results['synthetic_click_events']} events)")
    print(f"  recall on held-out clips   : {results['recall_on_holdout']:.3f} "
          f"({results['n_positive_clips']} clips, need {args.min_recall:.2f})")
    print(f"  false wakes                : {results['false_wakes_per_hour']:.3f}/hr "
          f"(bound {args.max_events_per_hour}, "
          f"{results['negative_corpus_hours']:.2f}h / {results['n_negative_clips']} clips)")

    if failures:
        print("\n  GATE FAILURES:")
        for failure in failures:
            eprint(f"    - {failure}")
        if not args.force:
            eprint("\nRefusing to install. Nothing was written.")
            eprint("Re-run with --force only if you accept these risks.")
            sys.exit(EXIT_GATE)
        eprint("\n--force given: installing despite the failures above.")
    else:
        print("  PASSED all gates.")

    metadata = {
        **results,
        "onnx_graph": introspect_onnx(model_path),
        "sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "size_bytes": model_path.stat().st_size,
        "training_date_utc": datetime.now(timezone.utc).isoformat(),
        "corpus_note": args.positive_corpus_note,
        "gate_failures": failures,
    }

    print("\n=== Atomic install ===")
    try:
        final_path, sidecar_path = atomic_install(model_path, data_dir, model_path.stem, metadata)
    except Exception as exc:
        eprint(f"FAIL: install aborted: {exc}")
        sys.exit(EXIT_INSTALL)

    print(f"  model  : {final_path}")
    print(f"  sidecar: {sidecar_path}")
    print(f"  dir    : {oct(final_path.parent.stat().st_mode & 0o777)}")

    unit = Path.home() / ".config/systemd/user/kakarot-ears.service"
    if not unit.exists():
        print("\nDone. Model staged, but no kakarot-ears.service is installed yet —")
        print("nothing is listening for it. The ears service will pick it up on first run.")
        return

    if args.restart:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "kakarot-ears.service"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            eprint(f"WARN: restart failed: {result.stderr.strip()}")
            eprint("      Run manually: systemctl --user restart kakarot-ears.service")
        else:
            print("\nDone. kakarot-ears.service restarted with the new model.")
    else:
        print("\nDone. Load it with: systemctl --user restart kakarot-ears.service")
        print("      (or re-run with --restart)")


if __name__ == "__main__":
    main()