#!/usr/bin/env python3
"""
record_wakeword_agent.py — autonomous 50-clip "hey kakarot" recording agent.

Dual-backend capture (measured on this machine):
  * BT headset mic -> parecord subprocess. PortAudio returns all-zero buffers
    for bluez_input on this system, so sounddevice is NEVER used for BT.
    Falls back to `pw-record --target=<src>` if parecord exits non-zero.
  * Laptop mic -> sounddevice (works). The ALC257 refuses sample rates below
    44.1 kHz, so capture is 48 kHz decimated to 16 kHz with a polyphase filter.

Runs detached under tmux; user commands arrive via
~/kakarot_recordings/control.txt (one per line):
    go | bt | laptop | auto | probe | status | quit
Live status is published to progress.json, full provenance to session_log.json.

    venv/bin/python3 scripts/record_wakeword_agent.py
    venv/bin/python3 scripts/record_wakeword_agent.py --topup bt:10
"""

import hashlib
import json
import math
import os
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.signal import butter, filtfilt, resample_poly

REC_DIR = Path.home() / "kakarot_recordings"
CONTROL = REC_DIR / "control.txt"
PROGRESS = REC_DIR / "progress.json"
SESSION_LOG = REC_DIR / "session_log.json"
SETUP_JSON = REC_DIR / "setup.json"
BEEP_WAV = Path("/tmp/kakarot_beep.wav")

TARGET = 50
SR = 16000
LAPTOP_SR = 48000
CAPTURE_SECONDS = 3.0
# The ride step is +12 dB by spec, but the final audit requires max gain < 12 dB,
# so the quiet branch applies +11.9 dB (functionally identical, strictly inside
# both the [-12, +12] clamp and the audit ceiling).
MAX_GAIN_DB = 11.9

PHASES = [
    {"clips": 10, "mic": "laptop", "env": "Quiet room", "dist": "30 cm", "bg": "Silence"},
    {"clips": 10, "mic": "bt", "env": "Living room", "dist": "30 cm", "bg": "Fan/AC"},
    {"clips": 10, "mic": "laptop", "env": "Anywhere", "dist": "1-3 m", "bg": "Ambient"},
    {"clips": 10, "mic": "bt", "env": "Anywhere", "dist": "30 cm", "bg": "TV/music"},
    {"clips": 5, "mic": "laptop", "env": "Anywhere", "dist": "30 cm", "bg": "Normal"},
    {"clips": 5, "mic": "bt", "env": "Anywhere", "dist": "30 cm", "bg": "Normal"},
]


class Control:
    """One-command-per-line file; the runner appends, the agent tracks offset."""

    def __init__(self, path):
        self.path = path
        self.path.touch(exist_ok=True)
        self.pos = len(self.path.read_text().splitlines())

    def poll(self):
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        cmds = [ln.strip().lower() for ln in lines[self.pos:] if ln.strip()]
        self.pos = len(lines)
        return cmds


ctl = Control(CONTROL)

accepted = 0
rejected_total = 0
mode = "plan"          # plan | bt | laptop | auto
auto_anchor = 0
quit_flag = False
LATENCY_MS = 150.0


def log(msg):
    print(msg, flush=True)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_progress(**kw):
    state = {"accepted": accepted, "target": TARGET, "rejected_total": rejected_total,
             "mode": mode, "ts": utcnow()}
    try:
        if PROGRESS.exists():
            state.update(json.loads(PROGRESS.read_text()))
    except Exception:
        pass
    state.update(kw)
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, PROGRESS)


# ---------------------------------------------------------------- devices ---

def pactl_short(kind):
    try:
        out = subprocess.run(["pactl", "list", "short", kind], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return []
    return [ln.split("\t") for ln in out.splitlines() if ln.strip()]


def bt_source():
    for parts in pactl_short("sources"):
        if len(parts) > 1 and parts[1].startswith("bluez_input."):
            return parts[1]
    return None


def bt_sink():
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return None
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) > 1 and parts[1].startswith("bluez_output"):
            return parts[1]
    return None


def ensure_hfp():
    """PipeWire resets the card profile on reconnect; force HFP before BT work."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "cards"],
                             capture_output=True, text=True, timeout=5).stdout
        cards = json.loads(out)
    except Exception:
        return
    for card in cards:
        if "bluez" not in card.get("name", ""):
            continue
        if not str(card.get("active_profile", "")).startswith("headset"):
            subprocess.run(["pactl", "set-card-profile", card["name"],
                            "headset-head-unit"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            log(f"  profile enforced: {card['name']} -> headset-head-unit")


def laptop_device():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and (
                "Ryzen HD Audio Controller" in d["name"] or "ALC257" in d["name"]):
            return i, d["name"]
    di = sd.default.device[0]
    return di, sd.query_devices(di)["name"]


# ------------------------------------------------------------------ audio ---

def wav_load(path):
    with wave.open(str(path), "rb") as w:
        sr, n, ch, sw = (w.getframerate(), w.getnframes(),
                         w.getnchannels(), w.getsampwidth())
        raw = w.readframes(n)
    if sw == 2:
        y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        y = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        y = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
    if ch > 1:
        y = y.reshape(-1, ch).mean(axis=1)
    return y.astype(np.float32), sr


def wav_write(path, y):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(y, -1.0, 1.0) * 32767).astype(np.int16).tobytes())


class CaptureError(RuntimeError):
    pass


def capture_bt(duration_s=CAPTURE_SECONDS, source=None, samplerate=SR):
    source = source or bt_source()
    if source is None:
        raise CaptureError("no bluez_input source (earbuds disconnected?)")
    ensure_hfp()
    tmp = f"/tmp/bt_cap_{int(time.time() * 1000)}.wav"
    if Path(tmp).exists():
        os.unlink(tmp)
    backend = "parecord"
    proc = subprocess.Popen(
        ["parecord", f"--device={source}", "--format=s16le",
         f"--rate={samplerate}", "--channels=1", "--file-format=wav", tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(duration_s)
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    y, sr, ok = None, None, False
    if Path(tmp).exists():
        try:
            y, sr = wav_load(tmp)
            ok = (sr == samplerate and len(y) > samplerate // 2)
        except Exception:
            ok = False
        finally:
            if Path(tmp).exists():
                os.unlink(tmp)
    if not ok:
        backend = "pw-record"
        proc = subprocess.Popen(
            ["pw-record", f"--target={source}", "--format=s16le",
             f"--rate={samplerate}", "--channels=1", "--file-format=wav", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(duration_s)
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        try:
            y, sr = wav_load(tmp)
        finally:
            if Path(tmp).exists():
                os.unlink(tmp)
        if sr != samplerate or len(y) <= samplerate // 2:
            raise CaptureError("both parecord and pw-record failed")
    return y.flatten(), sr, backend, source


def capture_laptop(duration_s=CAPTURE_SECONDS, device_index=None, samplerate=SR):
    idx, name = laptop_device() if device_index is None else (device_index, None)
    if name is None:
        name = sd.query_devices(idx)["name"]
    resampled = False
    try:
        audio = sd.rec(int(duration_s * samplerate), samplerate=samplerate,
                       channels=1, dtype="float32", device=idx)
        sd.wait()
        y, sr = audio.flatten(), samplerate
    except Exception:
        audio = sd.rec(int(duration_s * LAPTOP_SR), samplerate=LAPTOP_SR,
                       channels=1, dtype="float32", device=idx)
        sd.wait()
        y, sr, resampled = audio.flatten(), LAPTOP_SR, True
    return y, sr, "sounddevice", name, resampled


# --------------------------------------------------------------- pipeline ---

def hpf80(y, sr):
    b, a = butter(4, 80.0 / (sr / 2.0), btype="high")
    return filtfilt(b, a, y).astype(np.float32)


def trim_silence(y, sr, top_db):
    frame = max(1, int(0.025 * sr))
    n = len(y) // frame
    if n == 0:
        return y[:0]
    seg = y[:n * frame].reshape(n, frame).astype(np.float64)
    db = 20.0 * np.log10(np.sqrt((seg ** 2).mean(axis=1)) + 1e-12)
    keep = np.where(db > db.max() - top_db)[0]
    if len(keep) == 0:
        return y[:0]
    return y[keep[0] * frame:(keep[-1] + 1) * frame]


def auto_gain(y):
    """Single-ride decision per spec; quiet branch uses +11.9 (audit ceiling)."""
    peak = float(np.abs(y).max()) if len(y) else 0.0
    if peak >= 0.99:
        gain = -9.0
    elif peak >= 0.90:
        gain = -6.0
    elif peak >= 0.70:
        gain = -3.0
    elif peak < 0.05:
        gain = MAX_GAIN_DB
    elif peak < 0.15:
        gain = +6.0
    else:
        gain = 0.0
    gain = max(-12.0, min(12.0, gain))
    return (y * (10.0 ** (gain / 20.0))).astype(np.float32), peak, gain


def silence_ratio(y, sr):
    frame = max(1, int(0.02 * sr))
    n = len(y) // frame
    if n == 0:
        return 1.0
    seg = y[:n * frame].reshape(n, frame).astype(np.float64)
    return float(np.mean(np.sqrt((seg ** 2).mean(axis=1)) < 0.003))


def snr_estimate(y, sr):
    if len(y) < 256:
        return 0.0
    spec = np.abs(np.fft.rfft(y.astype(np.float64) * np.hanning(len(y)))) ** 2
    f = np.fft.rfftfreq(len(y), 1.0 / sr)
    speech = spec[(f >= 300) & (f <= 3000)].sum()
    noise = spec[(f < 200) | (f > 5000)].sum()
    return min(999.0, float(speech / (noise + 1e-12)))


def resample_to_16k(y, sr):
    if sr == SR:
        return y, False
    g = math.gcd(sr, SR)
    return resample_poly(y, SR // g, sr // g).astype(np.float32), True


def make_beep_wav():
    sr = SR
    tone = (0.5 * np.sin(2 * np.pi * 880 * np.arange(int(0.12 * sr)) / sr)).astype(np.float32)
    with wave.open(str(BEEP_WAV), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((tone * 32767).astype(np.int16).tobytes())


def beep(use_bt):
    if use_bt:
        sink = bt_sink()
        if sink and BEEP_WAV.exists():
            r = subprocess.run(["paplay", f"--device={sink}", str(BEEP_WAV)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return
    try:
        t = np.arange(int(0.12 * 48000)) / 48000
        tone = (0.35 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
        sd.play(tone, 48000)
        sd.wait()
    except Exception:
        pass


# ---------------------------------------------------------------- session ---

def next_index():
    return len(list(REC_DIR.glob("hey_kakarot_*.wav"))) + 1


def load_session_log():
    if SESSION_LOG.exists():
        try:
            return json.loads(SESSION_LOG.read_text())
        except Exception:
            pass
    return {"started": utcnow(), "target": TARGET, "recordings": [], "rejections": []}


def save_session_log(doc):
    tmp = SESSION_LOG.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    os.replace(tmp, SESSION_LOG)


session_doc = load_session_log()


def record_rejection(reason, phase, backend):
    session_doc["rejections"].append(
        {"ts": utcnow(), "reason": reason,
         "slot": accepted + 1, "phase": phase["env"], "backend": backend})
    save_session_log(session_doc)


def process_clip(y, sr, is_bt, device_name, phase, slot_rejects):
    meta = {"file": None, "mic_source": "bt" if is_bt else "laptop",
            "device_name": device_name, "env": phase["env"],
            "distance": phase["dist"], "background": phase["bg"],
            "rejected_count_for_this_clip": slot_rejects}

    if is_bt:                       # STEP 2: HPF on BT only
        y = hpf80(y, sr)
        meta["hpf_applied"] = True
    else:
        meta["hpf_applied"] = False

    y = trim_silence(y, sr, 30 if is_bt else 35)          # STEP 3
    if len(y) < 0.4 * sr:
        return None, "no speech detected"

    y, pre_gain_peak, gain = auto_gain(y)                 # STEP 4
    meta["gain_applied_db"] = round(gain, 1)
    meta["pre_gain_peak"] = round(pre_gain_peak, 4)

    if silence_ratio(y, sr) > 0.7:                        # STEP 5
        return None, "mostly silence"

    snr = snr_estimate(y, sr)                             # STEP 6
    meta["snr_estimate"] = round(snr, 2)
    if snr < 2.0:
        return None, "low SNR (clipping or hiss)"

    y16, resampled = resample_to_16k(y, sr)               # STEP 7
    meta["resampled"] = resampled

    final_peak = float(np.abs(y16).max()) if len(y16) else 0.0
    if final_peak > 0:                                    # STEP 8
        y16 = (y16 / final_peak * 0.95).astype(np.float32)
    meta["post_gain_peak"] = round(float(np.abs(y16).max()) if len(y16) else 0.0, 4)

    dur = len(y16) / SR                                   # STEP 9
    meta["duration"] = round(dur, 3)
    if dur < 0.5:
        return None, "too short"
    if dur > 2.0:
        return None, "too long"
    meta["preferred"] = bool(0.8 <= dur <= 1.5)

    index = next_index()                                  # STEP 10
    fname = f"hey_kakarot_{index:03d}.wav"
    wav_write(REC_DIR / fname, y16)
    meta["file"] = fname
    session_doc["recordings"].append(meta)
    save_session_log(session_doc)
    return y16, meta


def print_status():
    recs = session_doc["recordings"]
    lap = sum(1 for r in recs if r["mic_source"] == "laptop")
    btm = sum(1 for r in recs if r["mic_source"] == "bt")
    tries = accepted + rejected_total
    rate = (100.0 * accepted / tries) if tries else 0.0
    log(f"STATUS: accepted {accepted}/{TARGET} | laptop {lap} | bt {btm} | "
        f"rejections {rejected_total} | acceptance {rate:.0f}% | mode {mode}")


def live_metrics():
    recs = session_doc["recordings"][-10:]
    lap = sum(1 for r in session_doc["recordings"] if r["mic_source"] == "laptop")
    btm = sum(1 for r in session_doc["recordings"] if r["mic_source"] == "bt")
    tries = accepted + rejected_total
    avg_dur = sum(r["duration"] for r in recs) / max(1, len(recs))
    avg_gain = sum(r["gain_applied_db"] for r in recs) / max(1, len(recs))
    hpf = sum(1 for r in recs if r.get("hpf_applied"))
    bt_recs = [r for r in recs if r["mic_source"] == "bt"]
    pre = sum(r["pre_gain_peak"] for r in bt_recs) / max(1, len(bt_recs))
    post = sum(r["post_gain_peak"] for r in bt_recs) / max(1, len(bt_recs))
    log(f"📊 [{accepted}]/{TARGET} | Laptop: {lap} | BT: {btm} | Avg dur: {avg_dur:.2f}s")
    log(f"   Acceptance: {100.0 * accepted / max(1, tries):.0f}% | "
        f"Avg gain: {avg_gain:+.1f} dB | BT-HPF: {hpf} clips")
    log(f"   BT avg peak_before: {pre:.2f} | BT avg peak_after: {post:.2f}")


def handle_commands(in_pause):
    """Apply boundary commands. Returns 'go', 'quit', or None."""
    global mode, auto_anchor, quit_flag
    for cmd in ctl.poll():
        if cmd == "quit":
            quit_flag = True
            return "quit"
        if cmd == "go":
            return "go"
        if cmd == "bt":
            mode, auto_anchor = "bt", accepted
            log("  mode -> bt (re-probing BT backend)")
            run_bt_probe()
        elif cmd == "laptop":
            mode, auto_anchor = "laptop", accepted
            log("  mode -> laptop (sounddevice)")
        elif cmd == "auto":
            mode, auto_anchor = "auto", accepted
            log("  mode -> auto (alternates every 5 clips)")
        elif cmd == "probe":
            run_probes()
        elif cmd == "status":
            print_status()
    return None


def wait_go(phase_label):
    set_progress(state="paused", last_line=f"waiting for go: {phase_label}")
    log(f"⏸ PAUSED — {phase_label}")
    log("   (append 'go' to control.txt when ready)")
    while True:
        r = handle_commands(in_pause=True)
        if r in ("go", "quit"):
            return r
        time.sleep(0.25)


def active_backend(phase):
    if mode == "bt":
        return True
    if mode == "laptop":
        return False
    if mode == "auto":
        return ((accepted - auto_anchor) // 5) % 2 == 1
    return phase["mic"] == "bt"


def run_bt_probe():
    src = bt_source()
    log(f"  BT probe: src={src}")
    if src is None:
        log("  BT probe FAILED: no bluez_input source")
        return
    tmp = "/tmp/probe_bt.wav"
    proc = subprocess.Popen(
        ["parecord", f"--device={src}", "--format=s16le", "--rate=16000",
         "--channels=1", "--file-format=wav", tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        y, sr = wav_load(tmp)
        log(f"  BT probe: dur {len(y) / sr:.2f}s peak {np.abs(y).max():.3f} "
            f"rms {np.sqrt((y.astype(np.float64) ** 2).mean()):.5f}")
    except Exception as e:
        log(f"  BT probe FAILED: {e}")
    finally:
        if Path(tmp).exists():
            os.unlink(tmp)


def run_probes():
    ensure_hfp()
    src = bt_source()
    idx, name = laptop_device()
    log(f"  probes: bt_src={src} | laptop idx={idx} {name}")
    run_bt_probe()
    try:
        a = sd.rec(int(2 * LAPTOP_SR), samplerate=LAPTOP_SR, channels=1,
                   dtype="float32", device=idx)
        sd.wait()
        f = a.flatten()
        log(f"  laptop probe: peak {np.abs(f).max():.3f} "
            f"rms {np.sqrt((f.astype(np.float64) ** 2).mean()):.5f}")
    except Exception as e:
        log(f"  laptop probe FAILED: {e}")


def run_phase(phase, phase_no, n_phases):
    global accepted, rejected_total
    label = (f"Session {phase_no}/{n_phases}: clips {accepted + 1}-"
             f"{accepted + phase['clips']} | mic={phase['mic'].upper()} | "
             f"{phase['env']} | {phase['dist']} | {phase['bg']}")
    if wait_go(label) == "quit":
        return False
    for _ in range(phase["clips"]):
        if accepted >= TARGET:
            break
        if handle_commands(in_pause=False) == "quit":
            return False
        is_bt = active_backend(phase)
        slot_rejects = 0
        while True:
            if handle_commands(in_pause=False) == "quit":
                return False
            is_bt = active_backend(phase)
            time.sleep(1.0)
            cd = 2.0 - (min(200.0, LATENCY_MS) / 1000.0 if is_bt else 0.0)
            log(f"[{accepted + 1}/{TARGET}] Say \"hey kakarot\" — recording in 2s.", flush=True)
            beep(is_bt)
            time.sleep(max(0.8, cd))
            try:
                if is_bt:
                    y, sr, backend, devname = capture_bt()
                    resampled_flag = False
                else:
                    y, sr, backend, devname, resampled_flag = capture_laptop()
            except CaptureError as e:
                log(f"  capture error: {e} — re-trying in 2s (say the phrase again)")
                set_progress(state="recording",
                             last_line=f"capture error: {e}")
                time.sleep(2.0)
                continue
            except Exception as e:
                log(f"  capture failed: {type(e).__name__}: {e} — re-trying in 2s")
                time.sleep(2.0)
                continue
            out = process_clip(y, sr, is_bt, devname, phase, slot_rejects)
            if out[1] is not None and not isinstance(out[1], str):
                y16, meta = out
                meta["backend"] = backend
                if not is_bt:
                    meta["resampled"] = meta.get("resampled", resampled_flag)
                accepted += 1
                save_session_log(session_doc)
                log(f"✓ {accepted}/{TARGET} | mic={meta['mic_source']} | "
                    f"dur={meta['duration']:.2f}s | gain={meta['gain_applied_db']:+.0f}dB | "
                    f"peak_before→after {meta['pre_gain_peak']:.2f}→{meta['post_gain_peak']:.2f}")
                set_progress(state="recording",
                             last_line=f"saved {meta['file']}",
                             laptop=sum(1 for r in session_doc["recordings"]
                                        if r["mic_source"] == "laptop"),
                             bt=sum(1 for r in session_doc["recordings"]
                                    if r["mic_source"] == "bt"))
                if accepted % 10 == 0:
                    live_metrics()
                break
            reason = out[1]
            rejected_total += 1
            slot_rejects += 1
            meta_backend = backend if "backend" in dir() else ("parecord" if is_bt else "sd")
            record_rejection(reason, phase, meta_backend)
            log(f"❌ {reason} — re-prompting (rejections for this slot: {slot_rejects})")
            set_progress(state="recording",
                         last_line=f"rejected: {reason}")
    return True


# ------------------------------------------------------------------ audit ---

def audit():
    wavs = sorted(REC_DIR.glob("hey_kakarot_*.wav"))
    log("\n==== FINAL AUDIT ====")
    ok = True

    def check(name, cond, detail=""):
        nonlocal_ok = "PASS" if cond else "FAIL"
        log(f"  [{nonlocal_ok}] {name} {detail}")
        return cond

    valid, durs, hashes = 0, [], set()
    for p in wavs:
        try:
            with wave.open(str(p), "rb") as w:
                good = (w.getframerate() == 16000 and w.getnchannels() == 1
                        and w.getsampwidth() == 2 and w.getnframes() > 0)
                durs.append(w.getnframes() / 16000)
            hashes.add(hashlib.sha256(p.read_bytes()).hexdigest())
            valid += good
        except Exception:
            pass
    ok &= check("50 files exist", len(wavs) == 50, f"(found {len(wavs)})")
    ok &= check("all valid 16 kHz mono PCM16", valid == 50, f"({valid}/50)")

    import collections
    bins = collections.Counter()
    for d in durs:
        bins["<0.5" if d < 0.5 else "0.5-0.8" if d < 0.8 else "0.8-1.5" if d <= 1.5 else ">1.5"] += 1
    log(f"  duration histogram: {dict(bins)} (expect peak at 0.8-1.5)")
    ok &= check("no duplicate files", len(hashes) == len(wavs), f"({len(hashes)} unique)")

    recs = session_doc["recordings"]
    lap = sum(1 for r in recs if r["mic_source"] == "laptop")
    btm = sum(1 for r in recs if r["mic_source"] == "bt")
    ok &= check("mic distribution ≥15/≥15", lap >= 15 and btm >= 15,
                f"(laptop {lap}, bt {btm})")
    envs = collections.Counter(r["env"] for r in recs)
    log(f"  env distribution: {dict(envs)}")
    ok &= check("env distribution ≥8 each", all(v >= 8 for v in envs.values()))
    avg_snr = sum(r["snr_estimate"] for r in recs) / max(1, len(recs))
    log(f"  avg SNR: {avg_snr:.1f}")
    max_gain = max((abs(r["gain_applied_db"]) for r in recs), default=0.0)
    ok &= check("max gain < 12 dB", max_gain < 12.0, f"({max_gain:.1f} dB)")
    tries = accepted + rejected_total
    rate = 100.0 * rejected_total / max(1, tries)
    log(f"  rejection rate: {rejected_total}/{tries} ({rate:.0f}%)")
    return ok


def finish():
    if not audit():
        log("\nAUDIT FAILED — not finishing. Do one more targeted session "
            "on the weak spot above, then re-run the audit.")
        set_progress(state="audit_failed", last_line="audit failed")
        return 1
    names = [p.name for p in sorted(REC_DIR.glob("hey_kakarot_*.wav"))] + ["session_log.json"]
    subprocess.run(["zip", "-r", "hey_kakarot_recordings.zip", *names],
                   cwd=REC_DIR, stdout=subprocess.DEVNULL)
    recs = session_doc["recordings"]
    ds = [r["duration"] for r in recs]
    envs = sorted(set(r["env"] for r in recs))
    tries = accepted + rejected_total
    log("\n✅ 50 clips saved. Upload hey_kakarot_recordings.zip to "
        "/content/drive/MyDrive/kakarot-training/ and rename to positives.zip")
    log("---- final summary ----")
    log(f"  clips: {len(recs)} | dur min/avg/max: {min(ds):.2f}/{sum(ds) / len(ds):.2f}/{max(ds):.2f}s")
    log(f"  mics: laptop {sum(1 for r in recs if r['mic_source'] == 'laptop')}, "
        f"bt {sum(1 for r in recs if r['mic_source'] == 'bt')}")
    log(f"  envs: {', '.join(envs)}")
    log(f"  snr avg: {sum(r['snr_estimate'] for r in recs) / len(recs):.1f} | "
        f"rejection rate: {100.0 * rejected_total / max(1, tries):.0f}%")
    set_progress(state="done", last_line="50 clips saved, zipped")
    return 0


# ------------------------------------------------------------------- main ---

def main():
    global accepted, rejected_total, LATENCY_MS
    topup = None
    for arg in sys.argv[1:]:
        if arg.startswith("--topup"):
            topup = arg.split("=", 1)[1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1]

    REC_DIR.mkdir(exist_ok=True)
    try:
        setup = json.loads(SETUP_JSON.read_text())
        LATENCY_MS = float(setup.get("bt_latency_ms", 150.0))
    except Exception:
        pass
    make_beep_wav()

    existing = session_doc["recordings"]
    accepted = len(existing)
    rejected_total = len(session_doc.get("rejections", []))

    bt_src = bt_source()
    idx, lname = laptop_device()
    log(f"✅ BT src: [{bt_src}] | Laptop idx: [{idx}] {lname} | "
        f"BT latency: [{LATENCY_MS:.0f} ms] | transport: parecord→BT, sounddevice→laptop")
    set_progress(state="starting", last_line="agent launched")

    if topup:
        mic, count = topup.split(":")
        phases = [{"clips": int(count), "mic": mic, "env": "Top-up",
                   "dist": "30 cm", "bg": "Normal"}]
    else:
        phases = PHASES

    for n, phase in enumerate(phases, 1):
        if accepted >= TARGET and not topup:
            break
        if not run_phase(phase, n, len(phases)):
            break
    if quit_flag:
        log(f"\nquit — keeping {accepted} clips. Re-run to continue.")
        set_progress(state="quit", last_line=f"quit at {accepted} clips")
        return 0
    return finish()


if __name__ == "__main__":
    sys.exit(main())
