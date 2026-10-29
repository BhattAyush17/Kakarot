# Kakarot Wake Word — `hey_kakarot.onnx`

Replaces the false-positive-prone `hey_mycroft` demo model with a locally trained
custom phrase. The Silero VAD gate (`vad_threshold`) stays enabled throughout; the
custom model is what removes the residual keyboard-click triggers.

## What the runtime expects (verified against openwakeword 0.4.0)

| Requirement | Why |
|---|---|
| File must be named **`hey_kakarot.onnx`** | openWakeWord keys `predict()` output by the **filename stem** (`hey_jarvis_v0.1.onnx` → `hey_jarvis_v0.1`). The listener reads that key. |
| Loaded as `Model(wakeword_model_paths=[path], vad_threshold=0.5)` | 0.4.0 forwards unknown kwargs into `AudioFeatures`; e.g. `inference_framework=` raises `TypeError`. Do not add kwargs. |
| 16 kHz, mono, int16, 1280-sample (80 ms) frames | Must match the listener's inference loop. |
| CPU ONNX opset | This machine only exposes `CPUExecutionProvider` (no CUDA EP). |
| Installed to `~/.local/share/kakarot/models/` | Resolved through `kakarot_daemon.config`, never hardcoded `~`. A `hey_kakarot.json` sidecar records threshold, VAD, FPR, recall, sha256 and the real ONNX graph signature. |

---

## Step 1 — Train in Colab (driven from VS Code)

The official extension is **`Google.colab`** (Marketplace id `google.colab`), which
exposes Colab servers inside VS Code on top of the Jupyter extension.

1. Install extensions `Google.colab` and `ms-toolsai.jupyter`.
2. Get the notebook. Two options:
   - **Prepared (recommended):** repo file `notebooks/kakarot_training_prepared.ipynb` —
     the upstream notebook with every Kakarot edit below already applied, plus a
     `my_clicks` staging cell, a PRE-FLIGHT validation cell, and a final download
     cell. Open it in VS Code with the Colab kernel, or upload it to Colab directly.
   - **Upstream:** `https://colab.research.google.com/github/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb`
     (then apply steps 3-5 by hand).
   Open it in VS Code, then **`Select Kernel` → `Colab` → `Auto Connect`** and pick a
   GPU runtime (T4).
3. **Environment cell (cell 4) — keep it as-is.** It does the git clone + editable
   install plus the TTS and training deps:

   ```python
   !git clone https://github.com/dscripka/openWakeword
   !pip install -e ./openwakeword
   !pip install speechbrain==0.5.14 audiomentations==0.33.0 acoustics==0.2.6 ...
   ```

   Do **not** replace this with `pip install openwakeword`: the 0.4.0 wheel ships no
   `openwakeword/train.py` training entry point, so the training cells fail.
   Positives are TTS-generated with piper, which is why that install is mandatory.

4. **Config cell (cell 14) — this is the only cell you must edit.** It loads
   `openwakeword/examples/custom_model.yml` into a `config` dict, so you change dict
   keys, not standalone variables:

   ```python
   config["target_phrase"] = ["hey kakarot"]          # a LIST, not a bare string
   config["model_name"] = config["target_phrase"][0].replace(" ", "_")  # -> hey_kakarot
   config["custom_negative_phrases"] = ["hey jarvis", "hey mycroft",
                                        "okay kakarot", "hey computer"]
   config["n_samples"] = 1000        # demo value; 20k minimum, 100k+ is better
   ```

   `model_name` is derived from the phrase, so the export is **already** named
   `hey_kakarot.onnx` — do not rename anything.

5. **Keyboard clicks are not a phrase and cannot go in `custom_negative_phrases`.**
   They must be added as *background* audio, alongside the AudioSet download:

   ```python
   config["background_paths"] = ['./audioset_16k', './fma', './my_clicks']
   ```

   Put 16 kHz mono recordings of your actual keyboard/mouse into `./my_clicks`.
   Record and stage them locally:

   ```bash
   venv/bin/python3 scripts/record_calibration.py --kind clicks   # 6 x 10s, type naturally
   venv/bin/python3 scripts/prepare_click_corpus.py               # validate + stage + zip
   ```

   `prepare_click_corpus.py` refuses to package under 20 s, flags clipped or
   off-spec takes, re-encodes with ffmpeg under `--convert`, and writes
   `~/kakarot_calibration/colab/{my_clicks/, my_clicks.zip, manifest.json}`.
   The `manifest.json` peak/RMS/sha256 data is **provenance only** — `train.py`
   consumes the WAV files themselves, so the audio is what matters.

   The prepared notebook's staging cell (right after the background-audio
   downloads) pulls `my_clicks` in from Google Drive or a `my_clicks.zip`, and
   **hard-fails if no click audio is staged**. Without it the model has never
   heard a click and cannot be expected to reject one.

   Optional, for the Step 2 promotion gate only:

   ```bash
   venv/bin/python3 scripts/record_calibration.py --kind positive   # held-out "hey kakarot"
   venv/bin/python3 scripts/record_calibration.py --kind nearmiss
   ```

6. Run the three training cells in order (each is resumable — re-running continues
   where it stopped):

   ```
   train.py --training_config my_model.yaml --generate_clips
   train.py --training_config my_model.yaml --augment_clips
   train.py --training_config my_model.yaml --train_model
   ```

   Output: `my_custom_model/hey_kakarot.onnx` (plus a `.tflite` you don't need).
   A rough run is ~10 min for clip generation on a free T4; training is longer.

```python
# download from the cell (or use the Colab file panel)
from google.colab import files
files.download("my_custom_model/hey_kakarot.onnx")
```

### Corpus cheat-sheet

| Set | Where it goes | Notes |
|---|---|---|
| Positives "hey kakarot" | auto (piper TTS) | speaker variety comes from the TTS voices + MIT RIR augmentation |
| Near-miss phrases | `config["custom_negative_phrases"]` | TTS-generated as adversarial negatives |
| **Keyboard / mouse clicks** | `config["background_paths"]` | **the reported failure — record your real ones** |
| Music, hum, babble | `config["background_paths"]` | AudioSet + FMA downloads cover this |

Set `steps` and `max_negative_weight` higher if the gate later reports false wakes.

---

## Step 2 — Promote locally (in this repo)

Build two calibration folders of **16 kHz mono 16-bit WAVs** (convert with
`ffmpeg -i in.wav -ac 1 -ar 16000 out.wav`): `positives/` held out from training, and
`negatives/` covering the table above. Record the takes with
`scripts/record_calibration.py` (`--kind positive` → `positives/`,
`--kind nearmiss` / `--kind clicks` → `negatives/`); folders already exist at
`~/kakarot_calibration/`.

```bash
venv/bin/python3 install_kakarot_model.py \
    --model ~/Downloads/hey_kakarot.onnx \
    --positives-dir ~/kakarot_calibration/positives \
    --negatives-dir ~/kakarot_calibration/negatives \
    --threshold 0.5 --vad-threshold 0.5 \
    --max-events-per-hour 1.0 --min-recall 0.8
```

The gate loads the model exactly as the listener will and checks:

1. **silence** with VAD *off* (with VAD on the check is vacuous),
2. **VAD gate self-test** — a synthetic keyboard-click train must produce 0 wake events,
3. **recall** on held-out positives,
4. **false wakes/hour** measured with the listener's 3 s refractory window and
   3-consecutive-frame rule, not raw frames.

On success it stages to `models/hey_kakarot.onnx.tmp`, re-loads it, scrubs home paths,
writes the sidecar, then `os.replace()`s into place — so the listener's
existence-check can never observe a half-written ONNX.

Exit codes: `0` promoted · `1` environment/usage · `2` gate failed (nothing written) ·
`3` install failed. `--force` overrides a failed gate and records it in the sidecar;
`--restart` restarts the listener afterwards. Add `KAKAROT_APP_DATA=/tmp/...` to test
against a scratch directory instead of your real data dir.

---

## Step 3 — Activate

```bash
systemctl --user restart kakarot-ears.service
journalctl --user -u kakarot-ears.service -f
```

> **Current state:** no `kakarot-ears.service` or `kakarot_ears/` package is deployed
> yet, so a staged model has nothing listening for it. The installer says so instead of
> pretending. Until the ears service lands, the wake listener still runs in-process in
> `persona/wake_word.py`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Training cell fails to import `train.py` | You used `pip install openwakeword` (0.4.0, no training entry point) — use the notebook's `git clone` + `pip install -e` cell |
| Piper / phonemize install fails | Notebook cell 4 notes piper-phonemize is Linux-only; Colab is Linux, so re-run the cell from scratch if the runtime was reset |
| Model still fires on clicks | Clicks were never in `background_paths` — add them and retrain |
| `prediction keys are [...], expected 'hey_kakarot'` | Filename stem mismatch — `model_name` must derive from `target_phrase` |
| `AttributeError: 'list' object has no attribute 'shape'` | `Model.predict()` needs `numpy.int16`, not `list`/`array.array` |
| `is 44100 Hz, expected 16000` | `ffmpeg -i in.wav -ac 1 -ar 16000 out.wav` |
| `VAD gate leaked: ... wake event(s)` | Raise `vad_threshold`, or retrain with click background data |
| `false wakes/hr exceeds bound` | Enlarge `n_samples`, raise `max_negative_weight`, or add harder negatives |
| `ONNX contains the local home path` | Retrain in Colab, or strip producer metadata |