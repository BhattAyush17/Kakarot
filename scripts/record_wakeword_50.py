#!/usr/bin/env python3
"""
record_wakeword_50.py — guided 50-clip "hey kakarot" capture with automatic
validation, balanced across the laptop mic and the Bluetooth headset.

Designed for real rooms (no soundproof booth). Produces 16 kHz mono PCM16 WAVs
plus a session_log.json provenance record, then verifies the corpus and zips it.

    venv/bin/python3 scripts/record_wakeword_50.py                 # interactive
    venv/bin/python3 scripts/record_wakeword_50.py --list-devices
    venv/bin/python3 scripts/record_wakeword_50.py --selftest      # no mic needed

Why the two mics need different plumbing (measured on this machine):
  * Laptop mic (ALC257) refuses to open below 44.1 kHz -> PortAudioError
    'Invalid sample rate'. Capture happens at 48 kHz and is decimated to
    16 kHz with a polyphase filter.
  * The BT headset (OnePlus Nord Buds 3 Pro) only appears as a PipeWire
    node (bluez_input.*), never in PortAudio's ALSA enumeration. It is
    reached by making it the PipeWire default source and streaming the
    'pipewire' virtual device.
  Both paths converge on 16 kHz before any analysis, so the validator sees
  identical audio regardless of which mic produced it.

Interactive commands (type during the session):
    skip     discard the take, advance without saving
    review   play the last saved clip back
    switch mic        toggle laptop <-> bluetooth
    switch environment  cycle the environment/noise label for this session
    quit     stop; keeps everything recorded so far
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.signal import butter, resample_poly, sosfiltfilt

TARGET_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2

WINDOW_SECONDS = 3.0
COUNTDOWN_SECONDS = 2
LEAD_SILENCE = 0.25

VAD_DB = -35.0                 # trim threshold from the capture protocol
VAD_AMPLITUDE = 10 ** (VAD_DB / 20.0)
TRIM_FRAME_MS = 10

NORMALISE_DBFS = -3.0
NORMALISE_PEAK = 10 ** (NORMALISE_DBFS / 20.0)

MIN_DURATION = 0.5
MAX_DURATION = 2.0
MIN_PEAK = 0.05
MAX_CLIPPED_FRAC = 0.02        # tolerate a little BT clipping, reject real distortion
MIN_RMS = 0.005
MIN_SPEECH_SECONDS = 0.4
MAX_BOOST_DB = 12.0            # warn when normalising up more than this

SPEECH_BAND = (300, 3000)
NOISE_BAND = (3000, 8000)

SESSIONS = [
    {"n": 1, "env": "bedroom_quiet",     "distance_cm": 30, "noise": "silence"},
    {"n": 2, "env": "living_room",       "distance_cm": 30, "noise": "tv_fan"},
    {"n": 3, "env": "kitchen",           "distance_cm": 200, "noise": "cooking_water"},
    {"n": 4, "env": "music",             "distance_cm": 30, "noise": "music_playing"},
    {"n": 5, "env": "normal_ambient",    "distance_cm": 30, "noise": "ambient"},
]
CLIPS_PER_SESSION = 10
TARGET_CLIPS = CLIPS_PER_SESSION * len(SESSIONS)

LAPTOP_DEVICE = 1              # HD-Audio Generic: ALC257 Analog (hw:1,0)
PIPEWIRE_DEVICE = 3            # virtual device fronting PipeWire nodes


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)


# --------------------------------------------------------------------------
# device plumbing
# --------------------------------------------------------------------------

def list_devices() -> None:
    print("=== INPUT DEVICES ===")
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] < 1:
            continue
        host = sd.query_hostapis(device["hostapi"])["name"]
        role = ""
        if index == LAPTOP_DEVICE:
            role = "   <== laptop mic (capture @48k, resample)"
        elif index == PIPEWIRE_DEVICE:
            role = "   <== pipewire router (carries the BT headset)"
        print(f"  [{index}] {device['name']}  ({device['max_input_channels']}ch, {host}){role}")
    bt = bluez_source_id()
    print(f"\n  BT headset source: {bt if bt else 'NOT FOUND (connect it in Settings > Bluetooth)'}")
    print(f"  default source   : {default_source()}")


def pactl(*args) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def bluez_source_id() -> str | None:
    for line in pactl("list", "short", "sources").splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and parts[1].startswith("bluez_input"):
            return parts[0]
    return None


def default_source() -> str:
    return pactl("get-default-source") or "unknown"


def set_default_source(source_id: str) -> None:
    subprocess.run(["pactl", "set-default-source", str(source_id)],
                   capture_output=True, timeout=10)


def bt_available() -> bool:
    return bluez_source_id() is not None


def capture_window(device: int, rate: int, seconds: float, mic: str) -> np.ndarray:
    """Capture one window at `rate`, then decimate to 16 kHz float32."""
    frames = int(seconds * rate)
    with sd.InputStream(device=device, channels=CHANNELS, samplerate=rate, dtype="int16") as stream:
        time.sleep(LEAD_SILENCE)
        buffer = np.empty(frames, dtype=np.int16)
        written = 0
        while written < frames:
            block = stream.read(min(4096, frames - written))[0]
            buffer[written:written + len(block)] = block
            written += len(block)

    audio = buffer.astype(np.float32) / 32768.0
    if rate != TARGET_RATE:
        factor = TARGET_RATE // gcd(rate, TARGET_RATE)
        audio = resample_poly(audio, factor, rate // TARGET_RATE).astype(np.float32)
    return audio


def gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def frame_rms(audio: np.ndarray, frame_ms: int = TRIM_FRAME_MS) -> np.ndarray:
    size = max(1, int(TARGET_RATE * frame_ms / 1000))
    usable = len(audio) // size * size
    if usable == 0:
        return np.asarray([], dtype=np.float32)
    return np.sqrt((audio[:usable].reshape(-1, size) ** 2).mean(axis=1))


def trim_silence(audio: np.ndarray) -> tuple[np.ndarray, float]:
    """Trim both ends at the VAD threshold. Returns (trimmed, speech_seconds)."""
    size = max(1, int(TARGET_RATE * TRIM_FRAME_MS / 1000))
    usable = len(audio) // size * size
    if usable == 0:
        return audio, 0.0
    frames = audio[:usable].reshape(-1, size)
    levels = np.sqrt((frames ** 2).mean(axis=1))
    voiced = np.where(levels > VAD_AMPLITUDE)[0]
    if voiced.size == 0:
        return audio, 0.0
    start = max(0, voiced[0] * size - TRIM_PAD_SAMPLES)
    end = min(len(audio), (voiced[-1] + 1) * size + TRIM_PAD_SAMPLES)
    return audio[start:end], float(voiced.size * size / TARGET_RATE)


TRIM_PAD_SAMPLES = int(0.05 * TARGET_RATE)


def measure(audio: np.ndarray) -> dict:
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    rms = float(np.sqrt((audio.astype(np.float64) ** 2).mean())) if audio.size else 0.0
    clipped = float(np.mean(np.abs(audio) > 0.99)) if audio.size else 0.0
    return {"peak": peak, "rms": rms, "clipped_frac": clipped}


def band_energy_db(audio: np.ndarray, low: int, high: int) -> float:
    sos = butter(4, [low / (TARGET_RATE / 2), high / (TARGET_RATE / 2)],
                 btype="band", output="sos")
    filtered = sosfiltfilt(sos, audio)
    return float(10 * np.log10(max(float((filtered.astype(np.float64) ** 2).mean()), 1e-12)))


def estimate_snr_db(window: np.ndarray, trimmed: np.ndarray) -> float:
    """Speech-band energy of the phrase vs speech-band energy of surrounding noise."""
    noise = window
    if len(trimmed) and len(window) > len(trimmed):
        mask = np.ones(len(window), dtype=bool)
        offset = int(np.argmax(np.abs(window) > VAD_AMPLITUDE))
        mask[max(0, offset):max(0, offset) + len(trimmed)] = False
        noise = window[mask]
    if noise.size < TARGET_RATE // 10 or trimmed.size < TARGET_RATE // 10:
        return float("nan")
    speech_db = band_energy_db(trimmed, *SPEECH_BAND)
    noise_db = band_energy_db(noise, *NOISE_BAND)
    return speech_db - noise_db


def validate(trimmed: np.ndarray, stats: dict) -> tuple[bool, str]:
    duration = len(trimmed) / TARGET_RATE
    if duration < MIN_DURATION:
        return False, f"too short after trim ({duration:.2f}s) — did you miss the start?"
    if duration > MAX_DURATION:
        return False, f"too long after trim ({duration:.2f}s) — extra words or noise?"
    if stats["peak"] < MIN_PEAK:
        return False, f"too quiet (peak {stats['peak']:.3f} < {MIN_PEAK}) — move closer"
    if stats["clipped_frac"] > MAX_CLIPPED_FRAC:
        return False, (f"clipping ({stats['clipped_frac'] * 100:.0f}% of samples) — "
                       "lower the mic/slider volume")
    if stats["rms"] < MIN_RMS:
        return False, f"mostly silence (rms {stats['rms']:.4f} < {MIN_RMS})"
    if duration and stats["peak"] > 0:
        return True, ""
    return False, "unusable take"


def normalise(audio: np.ndarray) -> tuple[np.ndarray, float]:
    peak = float(np.abs(audio).max())
    if peak <= 0:
        return audio, 0.0
    gain = NORMALISE_PEAK / peak
    return (audio * gain).astype(np.float32), 20 * np.log10(gain)


def write_wav(path: Path, audio: np.ndarray) -> None:
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(TARGET_RATE)
        handle.writeframes(pcm.tobytes())


# --------------------------------------------------------------------------
# capture loop
# --------------------------------------------------------------------------

def prompt_clip(index: int, total: int) -> None:
    print(f"  Say 'hey kakarot' — recording starts in {COUNTDOWN_SECONDS} seconds. "
          f"Speak now.  [{index}/{total}]")


def record_once(mic: str) -> np.ndarray:
    """Capture one window on the requested mic. BT needs the PipeWire default swap."""
    previous = None
    if mic == "bt":
        source = bluez_source_id()
        if not source:
            raise RuntimeError("Bluetooth headset is not connected")
        previous = pactl("get-default-source").split(".")[-1]
        set_default_source(source)
        time.sleep(0.6)
        try:
            return capture_window(PIPEWIRE_DEVICE, 48000, WINDOW_SECONDS, mic)
        finally:
            restored = pactl("list", "short", "sources")
            for line in restored.splitlines():
                parts = line.split("\t")
                if len(parts) > 1 and parts[1].startswith("alsa_input"):
                    set_default_source(parts[0])
                    break
    return capture_window(LAPTOP_DEVICE, 48000, WINDOW_SECONDS, mic)


def play(path: Path) -> None:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    data = (np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0)
    sd.play(data, rate)
    sd.wait()


def run(out_dir: Path, log_path: Path, balance: bool, target: int) -> int:
    log = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "target_phrase": "hey kakarot",
        "sample_rate": TARGET_RATE,
        "channels": CHANNELS,
        "sample_width_bits": SAMPLE_WIDTH * 8,
        "vad_db": VAD_DB,
        "normalise_dbfs": NORMALISE_DBFS,
        "devices": {
            "laptop": sd.query_devices(LAPTOP_DEVICE)["name"],
            "bluetooth": bluez_source_id() or "not connected",
        },
        "clips": [],
    }
    log_path.write_text(json.dumps(log, indent=2))

    saved = 0
    rejected = 0
    last_saved: Path | None = None
    mic_override = None

    for session in SESSIONS:
        print(f"\n{'=' * 68}")
        print(f"SESSION {session['n']}/5 — {session['env']}  "
              f"({session['distance_cm']}cm, background: {session['noise']})")
        print(f"{'=' * 68}")
        if balance:
            print("  mic alternates laptop/bt each clip (balanced corpus)")
        else:
            print("  press 'switch mic' at any time to change microphones")
        input("  Press Enter when you are ready... ")

        session_mics = []
        for position in range(CLIPS_PER_SESSION):
            if mic_override:
                mic = mic_override
            elif balance:
                mic = "laptop" if position % 2 == 0 else "bt"
            else:
                mic = "laptop"

            if mic == "bt" and not bt_available():
                print("  ! Bluetooth not connected — using laptop mic for this clip")
                mic = "laptop"

            index = session["n"] * CLIPS_PER_SESSION - CLIPS_PER_SESSION + position + 1
            total = TARGET_CLIPS
            saved_this_session = position

            while True:
                prompt_clip(index, total)
                for remaining in range(COUNTDOWN_SECONDS, 0, -1):
                    print(f"    {remaining}...", end="\r", flush=True)
                    time.sleep(1)
                print("    RECORDING          ", flush=True)
                try:
                    window = record_once(mic)
                except Exception as exc:
                    print(f"  ! capture failed: {exc}")
                    time.sleep(1.5)
                    continue

                trimmed, speech_seconds = trim_silence(window)
                stats = measure(trimmed)
                ok, reason = validate(trimmed, stats)

                if not ok:
                    rejected += 1
                    print(f"  X Rejected ({reason}) — let's try again.")
                    time.sleep(0.6)
                    continue

                normalised, boost_db = normalise(trimmed)
                if boost_db > MAX_BOOST_DB:
                    print(f"  ! normalised up {boost_db:+.1f} dB — noise floor lifted too")

                path = out_dir / f"hey_kakarot_{saved + 1:03d}.wav"
                write_wav(path, normalised)
                last_saved = path
                saved += 1
                session_mics.append(mic)

                duration = len(normalised) / TARGET_RATE
                print(f"  V Clip {saved}/{target} saved ({path.name})  "
                      f"{duration:.2f}s  peak {stats['peak']:.3f}  rms {stats['rms']:.4f}  "
                      f"mic={mic}  env={session['env']}")
                log["clips"].append({
                    "file": path.name,
                    "index": saved,
                    "session": session["n"],
                    "environment": session["env"],
                    "noise": session["noise"],
                    "distance_cm": session["distance_cm"],
                    "mic": mic,
                    "mic_device": log["devices"]["laptop" if mic == "laptop" else "bluetooth"],
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "duration_s": round(duration, 3),
                    "speech_seconds": round(speech_seconds, 3),
                    "pre_normalise": {k: round(v, 5) for k, v in stats.items()},
                    "normalise_gain_db": round(boost_db, 2),
                    "snr_db": round(estimate_snr_db(window, trimmed), 2),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })
                log_path.write_text(json.dumps(log, indent=2))
                break

            command = input("    [Enter=continue, skip / review / switch mic / "
                            "switch environment / quit] ").strip().lower()
            if command == "quit":
                log_path.write_text(json.dumps(log, indent=2))
                print(f"\n  stopped early — {saved} clip(s) kept.")
                return saved
            if command == "skip":
                saved -= 1
                if last_saved and last_saved.exists():
                    last_saved.unlink()
                log["clips"] = [c for c in log["clips"] if c["file"] != last_saved.name] \
                    if last_saved else log["clips"]
                print("    skipped (previous clip removed)")
                log_path.write_text(json.dumps(log, indent=2))
                continue
            if command == "review" and last_saved and last_saved.exists():
                print("    playing back...")
                play(last_saved)
                if input("    acceptable? [y/n] ").strip().lower().startswith("n"):
                    saved -= 1
                    last_saved.unlink(missing_ok=True)
                    log["clips"] = [c for c in log["clips"] if c["file"] != last_saved.name]
                    print("    removed; re-record this slot")
                    log_path.write_text(json.dumps(log, indent=2))
                continue
            if command == "switch mic":
                mic_override = "bt" if (mic_override or mic) == "laptop" else "laptop"
                print(f"    mic pinned to {mic_override}")
            elif command == "switch environment":
                order = [s["env"] for s in SESSIONS]
                session["env"] = order[(order.index(session["env"]) + 1) % len(order)]
                print(f"    environment now {session['env']}")

        print(f"  session {session['n']} done: {len(session_mics)} clips "
              f"({session_mics.count('laptop')} laptop / {session_mics.count('bt')} bt)")

    print(f"\n  {rejected} take(s) rejected during the run.")
    log_path.write_text(json.dumps(log, indent=2))
    return saved


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify(out_dir: Path, log_path: Path, zip_name: str) -> bool:
    log = json.loads(log_path.read_text())
    entries = log["clips"]
    files = sorted(out_dir.glob("hey_kakarot_*.wav"))
    print("\n" + "=" * 68)
    print("QUALITY CHECKS")
    print("=" * 68)

    problems = []

    expected = [f"hey_kakarot_{i:03d}.wav" for i in range(1, len(entries) + 1)]
    actual = [f.name for f in files]
    naming_ok = actual == expected
    print(f"  naming sequential 001..{len(entries):03d} : "
          f"{'OK' if naming_ok else 'MISMATCH'}")
    if not naming_ok:
        problems.append("file naming does not match the log")

    format_bad = []
    durations = []
    for path in files:
        try:
            with wave.open(str(path), "rb") as handle:
                if (handle.getframerate() != TARGET_RATE or handle.getnchannels() != CHANNELS
                        or handle.getsampwidth() != SAMPLE_WIDTH):
                    format_bad.append(path.name)
                durations.append(handle.getnframes() / handle.getframerate())
        except Exception as exc:
            format_bad.append(f"{path.name} ({exc})")
    print(f"  all {TARGET_RATE} Hz mono PCM16           : "
          f"{'OK' if not format_bad else 'BAD: ' + ', '.join(format_bad[:5])}")
    if format_bad:
        problems.append("format violations present")

    if durations:
        buckets = {"<0.8s": 0, "0.8-1.5s": 0, ">1.5s": 0}
        for d in durations:
            if d < 0.8:
                buckets["<0.8s"] += 1
            elif d <= 1.5:
                buckets["0.8-1.5s"] += 1
            else:
                buckets[">1.5s"] += 1
        print(f"  duration histogram                 : {buckets}")
        print(f"  min {min(durations):.2f}s  max {max(durations):.2f}s  "
              f"avg {sum(durations) / len(durations):.2f}s")

    digests = [e["sha256"] for e in entries]
    dupes = len(digests) - len(set(digests))
    print(f"  unique hashes                      : "
          f"{'OK' if dupes == 0 else f'{dupes} DUPLICATE(S)'}")
    if dupes:
        problems.append("duplicate clips detected")

    mics = {}
    for entry in entries:
        mics[entry["mic"]] = mics.get(entry["mic"], 0) + 1
    print(f"  mic distribution                   : {mics}")
    balanced = len(mics) == 2 and min(mics.values()) >= max(5, len(entries) // 4)
    print(f"  >=5 clips from each mic            : {'OK' if balanced else 'NOT MET'}")
    if not balanced:
        problems.append("mic distribution unbalanced")

    envs = {}
    for entry in entries:
        envs[entry["environment"]] = envs.get(entry["environment"], 0) + 1
    print(f"  environment distribution           : {envs}")
    if len(envs) < 3:
        problems.append("fewer than 3 environments recorded")

    snrs = [e["snr_db"] for e in entries if e.get("snr_db") == e.get("snr_db")]
    if snrs:
        sample = snrs[:10]
        print(f"  SNR estimate (first 10)            : "
              f"min {min(sample):.1f} dB  avg {sum(sample) / len(sample):.1f} dB  "
              f"max {max(sample):.1f} dB")

    zip_path = out_dir / zip_name
    with __import__("zipfile").ZipFile(zip_path, "w", __import__("zipfile").ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=f"positives/{path.name}")
        zf.write(log_path, arcname="session_log.json")
    print(f"\n  zipped -> {zip_path} ({zip_path.stat().st_size / 1024:.0f} KiB)")

    print(f"\n  {'=' * 68}")
    if saved_ok := (not problems):
        print(f"  V {len(entries)} clips recorded. Ready to upload to "
              f"/content/drive/MyDrive/kakarot-training/positives.zip")
    else:
        print(f"  X issues: {'; '.join(problems)}")
    print("=" * 68)

    summary = {
        "total_clips": len(entries),
        "avg_duration_s": round(sum(durations) / len(durations), 3) if durations else 0,
        "min_duration_s": round(min(durations), 3) if durations else 0,
        "max_duration_s": round(max(durations), 3) if durations else 0,
        "mic_distribution": mics,
        "environment_distribution": envs,
    }
    print("\n  SUMMARY")
    for key, value in summary.items():
        print(f"    {key:26s} {value}")
    return saved_ok


def selftest() -> None:
    """Exercise trim/validate/normalise/write without touching a microphone."""
    import tempfile
    print("=== SELFTEST (no microphone) ===")
    rng = np.random.default_rng(11)
    frame = int(TARGET_RATE * 0.1)

    tone = np.zeros(frame, dtype=np.float32)
    t = np.arange(frame) / TARGET_RATE
    tone += (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    good = np.concatenate([np.zeros(frame, dtype=np.float32), tone,
                           np.zeros(int(0.3 * TARGET_RATE), dtype=np.float32)])
    trimmed, speech = trim_silence(good)
    ok, reason = validate(trimmed, measure(trimmed))
    print(f"  clean tone       trim {len(trimmed) / TARGET_RATE:.2f}s "
          f"speech {speech:.2f}s -> {'valid' if ok else 'reject: ' + reason}")

    norm, boost = normalise(trimmed)
    print(f"  normalise        peak {np.abs(norm).max():.4f} "
          f"({20 * np.log10(np.abs(norm).max()):.1f} dBFS) gain {boost:+.1f} dB")

    quiet = (good * 0.01).astype(np.float32)
    ok, reason = validate(trim_silence(quiet)[0], measure(trim_silence(quiet)[0]))
    print(f"  too quiet        -> {'valid' if ok else 'reject: ' + reason}")

    clipped = np.clip(good * 6.0, -1.0, 1.0).astype(np.float32)
    ok, reason = validate(trim_silence(clipped)[0], measure(trim_silence(clipped)[0]))
    print(f"  clipping         -> {'valid' if ok else 'reject: ' + reason}")

    noise = rng.normal(0, 0.002, frame * 20).astype(np.float32)
    ok, reason = validate(trim_silence(noise)[0], measure(trim_silence(noise)[0]))
    print(f"  noise only       -> {'valid' if ok else 'reject: ' + reason}")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "hey_kakarot_001.wav"
        write_wav(path, norm)
        with wave.open(str(path), "rb") as handle:
            print(f"  wav roundtrip    {handle.getframerate()} Hz "
                  f"{handle.getnchannels()}ch {handle.getsampwidth() * 8}-bit "
                  f"{handle.getnframes() / handle.getframerate():.2f}s")

    window = np.concatenate([rng.normal(0, 0.004, TARGET_RATE).astype(np.float32),
                             tone[:TARGET_RATE]])
    print(f"  snr estimate     {estimate_snr_db(window, tone[:TARGET_RATE]):.1f} dB")
    print("=== SELFTEST DONE ===")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path.home() / "kakarot_recordings")
    parser.add_argument("--target", type=int, default=TARGET_CLIPS,
                        help=f"clips to aim for (default {TARGET_CLIPS})")
    parser.add_argument("--balance", action="store_true", default=True,
                        help="alternate laptop/bt every clip (default on)")
    parser.add_argument("--no-balance", dest="balance", action="store_false",
                        help="keep one mic per session; use 'switch mic' manually")
    parser.add_argument("--zip-name", default="hey_kakarot_recordings.zip")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return 0
    if args.selftest:
        selftest()
        return 0

    out_dir = args.out.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "session_log.json"

    if not args.verify_only:
        print("=" * 68)
        print("  'hey kakarot' — 50-clip guided capture")
        print("=" * 68)
        list_devices()
        if not bt_available():
            eprint("\n  ! No Bluetooth headset found. Sessions needing bt will fall back")
            eprint("    to the laptop mic. Connect it and re-run --list-devices.")
        print()
        print("  Commands during recording: skip | review | switch mic | "
              "switch environment | quit")
        print("  Speak naturally — vary pitch, pace and volume between takes.")

        saved = run(out_dir, log_path, args.balance, args.target)
        if saved < args.target:
            print(f"\n  captured {saved}/{args.target}. "
                  f"Re-run to append more clips.")
            if saved == 0:
                return 1

    return 0 if verify(out_dir, log_path, args.zip_name) else 1


if __name__ == "__main__":
    sys.exit(main())
