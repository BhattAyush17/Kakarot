#!/usr/bin/env python3
"""Build the reproducible Colab notebook for Kakarot wake-word training.

The notebook is intentionally generated from this compact source so that its
substantial code cells remain reviewable in a normal diff.  It never embeds
recordings or execution output.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "notebooks" / "kakarot_recorded_wakeword_training.ipynb"


def _source(value: str) -> list[str]:
    value = textwrap.dedent(value).lstrip("\n")
    return value.splitlines(keepends=True)


def markdown(value: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": _source(value)}


def code(value: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": _source(value),
    }


cells = [
    markdown(
        """
        # Kakarot recorded wake-word training

        This is a clean Colab workflow for **`hey kakarot`** that uses the two
        supplied recordings as real positive seeds, keeps a genuinely held-out
        group for evaluation, and mixes those seeds with synthetic examples.

        It fixes the earlier notebook's actual export failure: the upstream
        `train.py` currently gives `store_true` flags the string default
        `"False"`, so `if args.convert_to_tflite:` is truthy even when the flag
        was never passed.  That made a successful ONNX export continue into an
        unwanted TensorFlow/`onnx_tf` conversion and fail.  This notebook
        patches that local Colab clone to use Boolean defaults and never asks
        for TFLite.

        It also rejects a model that cannot detect held-out recordings.  Do not
        deploy an ONNX file merely because the training process exited zero.

        ## Required inputs

        Upload `kakarot_simple.mpeg` and `kakarot_noisy.mpeg`.  Also upload a
        ZIP named with `background`, `negative`, or `click` containing at least
        30 seconds of non-wake audio, including real keyboard/mouse sounds from
        the deployment room.  The background ZIP is split before training, so
        its evaluation part is never used as training background.

        Review the candidate clips before the split cell.  An energy detector
        finds likely utterances; it cannot prove that a noisy segment contains
        the phrase.
        """
    ),
    code(
        r'''
        # Cell 1 — fresh Colab setup, explicit dependency and source checks.
        # Start a GPU runtime before running this cell.
        import ast
        import importlib
        import json
        import os
        import shutil
        import subprocess
        import sys
        import urllib.request
        from pathlib import Path

        WORK = Path("/content/kakarot-recorded-wakeword")
        WORK.mkdir(parents=True, exist_ok=True)
        os.chdir(WORK)

        def run(args, *, cwd=None):
            print("+", " ".join(map(str, args)))
            return subprocess.run(list(map(str, args)), cwd=cwd, check=True)

        # System packages are only needed inside the disposable Colab VM.
        run(["apt-get", "update", "-qq"])
        run(["apt-get", "install", "-y", "-qq", "espeak-ng", "libespeak-ng1", "libsndfile1", "ffmpeg", "git"])

        def clone_if_missing(url, destination):
            destination = Path(destination)
            if not destination.exists():
                run(["git", "clone", "--depth", "1", url, str(destination)])
            return destination

        OWW = clone_if_missing("https://github.com/dscripka/openWakeWord.git", WORK / "openwakeword")
        PIPER = clone_if_missing("https://github.com/dscripka/piper-sample-generator.git", WORK / "piper-sample-generator")

        # Do not install or replace torch: Colab's preinstalled GPU build must stay intact.
        packages = [
            "piper-phonemize", "webrtcvad-wheels", "soundfile", "librosa",
            "onnx>=1.16", "onnxscript>=0.1", "onnxruntime>=1.18",
            "datasets>=2.21,<4", "scipy", "tqdm", "pyyaml", "requests",
            "mutagen==1.47.0", "torchinfo==1.8.0", "torchmetrics==1.2.0",
            "speechbrain==0.5.14", "audiomentations==0.33.0",
            "torch-audiomentations==0.11.0", "acoustics==0.2.6",
            "pronouncing==0.2.0",
        ]
        run([sys.executable, "-m", "pip", "install", "-q", "--upgrade", *packages])
        run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "-e", str(OWW)])

        model_path = PIPER / "models" / "en-us-libritts-high.pt"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        if not model_path.exists() or model_path.stat().st_size < 100_000_000:
            urllib.request.urlretrieve(
                "https://github.com/rhasspy/piper-sample-generator/releases/download/v1.0.0/en-us-libritts-high.pt",
                model_path,
            )

        resources = OWW / "openwakeword" / "resources" / "models"
        resources.mkdir(parents=True, exist_ok=True)
        for filename in ("embedding_model.onnx", "melspectrogram.onnx"):
            destination = resources / filename
            if not destination.exists() or destination.stat().st_size < 1000:
                urllib.request.urlretrieve(
                    f"https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/{filename}",
                    destination,
                )

        # Upstream repair: `default="False"` is a truthy string.  The unpatched
        # conversion conditional therefore imports onnx_tf even without its CLI flag.
        train_py = OWW / "openwakeword" / "train.py"
        train_src = train_py.read_text()
        patched = train_src.replace('default="False"', "default=False")
        patched = patched.replace("if args.convert_to_tflite:", "if args.convert_to_tflite is True:")
        patched = patched.replace(
            "model_to_save = copy.deepcopy(model)\n        torch.onnx.export(",
            "model_to_save = copy.deepcopy(model).eval()\n        torch.onnx.export(",
        )
        if patched == train_src:
            raise RuntimeError("Expected openWakeWord train.py patch points were not found; inspect this checkout.")
        ast.parse(patched)
        train_py.write_text(patched)
        assert 'default="False"' not in train_py.read_text()
        assert "if args.convert_to_tflite is True:" in train_py.read_text()

        # Kakarot is normally out-of-vocabulary for CMUdict.  Supply an explicit
        # ARPAbet pronunciation so train.py can generate phonetic near-misses
        # without downloading/loading the fragile DeepPhonemizer checkpoint.
        pronunciation_file = OWW / "openwakeword" / "resources" / "custom_pronunciations.json"
        pronunciation_file.parent.mkdir(parents=True, exist_ok=True)
        pronunciation_file.write_text(json.dumps({"kakarot": "K AE K ER AA T"}, indent=2))
        data_py = OWW / "openwakeword" / "data.py"
        data_src = data_py.read_text()
        marker = "# KAKAROT_CUSTOM_PRONUNCIATION"
        needle = "    input_text_phones = [pronouncing.phones_for_word(i) for i in input_text.split()]"
        replacement = """    # KAKAROT_CUSTOM_PRONUNCIATION\n    pronunciation_path = Path(__file__).parent / "resources" / "custom_pronunciations.json"\n    try:\n        import json\n        custom_pronunciations = json.loads(pronunciation_path.read_text()) if pronunciation_path.exists() else {}\n    except Exception:\n        custom_pronunciations = {}\n    input_text_phones = [\n        pronouncing.phones_for_word(i) or ([custom_pronunciations[i.casefold()]] if i.casefold() in custom_pronunciations else [])\n        for i in input_text.split()\n    ]"""
        if marker not in data_src:
            if needle not in data_src:
                raise RuntimeError("Expected openWakeWord data.py pronunciation hook was not found.")
            data_src = data_src.replace(needle, replacement)
            ast.parse(data_src)
            data_py.write_text(data_src)

        # Verify the exact interpreter that train.py will use; don't trust a
        # successful shell `pip` message from a different environment.
        run([sys.executable, "-c", "import onnx, onnxruntime, onnxscript; print('onnx export stack OK')"])
        run([sys.executable, str(train_py), "--help"])
        from openwakeword.data import generate_adversarial_texts
        assert generate_adversarial_texts("hey kakarot", 3), "custom pronunciation patch did not yield near-misses"
        import torch
        print("Python:", sys.version.split()[0])
        print("Torch:", torch.__version__, "CUDA:", torch.cuda.is_available())
        if not torch.cuda.is_available():
            raise RuntimeError("Select a Colab GPU runtime before continuing.")
        print("openWakeWord commit:", subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=OWW, text=True).strip())
        print("Setup complete.  The ONNX-only train path and Kakarot pronunciation are verified.")
        '''
    ),
    code(
        r'''
        # Cell 2 — upload the supplied positive recordings and a separate background ZIP.
        # In Colab's browser UI this opens a file picker.  Upload both MPEGs and,
        # separately, e.g. `click_background.zip` with click/room/noise WAVs.
        from google.colab import files

        RAW = WORK / "raw_uploads"
        RAW.mkdir(exist_ok=True)
        uploaded = files.upload()
        for name, payload in uploaded.items():
            (RAW / Path(name).name).write_bytes(payload)

        audio_extensions = {".wav", ".mp3", ".mpeg", ".m4a", ".flac", ".ogg"}
        positives = [
            p for p in RAW.iterdir()
            if p.suffix.lower() in audio_extensions and "kakarot" in p.stem.casefold()
        ]
        archives = [p for p in RAW.iterdir() if p.suffix.lower() == ".zip"]
        print("positive source files:", [p.name for p in positives])
        print("ZIP files:", [p.name for p in archives])
        if len(positives) < 2:
            raise RuntimeError("Upload both kakarot_simple.mpeg and kakarot_noisy.mpeg (or two files whose names contain 'kakarot').")
        if not archives:
            raise RuntimeError("Upload a separate background/click ZIP.  Do not train a click-resistant wake word with dummy noise.")
        '''
    ),
    code(
        r'''
        # Cell 3 — extract and convert the positives; no arbitrary center crops.
        import json
        import shutil
        import subprocess
        import numpy as np
        import librosa
        import soundfile as sf

        CANDIDATES = WORK / "positive_candidates"
        if CANDIDATES.exists():
            shutil.rmtree(CANDIDATES)
        CANDIDATES.mkdir()
        metadata = []

        def decode_16k(source: Path) -> np.ndarray:
            destination = WORK / f"{source.stem}.decoded.wav"
            run(["ffmpeg", "-y", "-v", "error", "-i", source, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", destination])
            audio, _ = librosa.load(destination, sr=16000, mono=True)
            destination.unlink(missing_ok=True)
            return audio.astype(np.float32)

        def intervals_for(audio: np.ndarray):
            # The clean file succeeds near 30 dB; the noisy file needs a tighter
            # threshold.  Take the first setting yielding enough plausible clips.
            best = []
            for top_db in (30, 25, 20, 15, 10, 8, 5):
                intervals = librosa.effects.split(audio, top_db=top_db, frame_length=2048, hop_length=512)
                valid = [(int(start), int(end)) for start, end in intervals if 0.35 <= (end - start) / 16000 <= 2.2]
                if len(valid) > len(best):
                    best = valid
                if len(valid) >= 6:
                    return valid, top_db
            return best, None

        index = 0
        for source in positives:
            audio = decode_16k(source)
            intervals, used_db = intervals_for(audio)
            print(f"{source.name}: {len(intervals)} candidates (top_db={used_db})")
            for start, end in intervals:
                clip, _ = librosa.effects.trim(audio[start:end], top_db=30)
                duration = len(clip) / 16000
                peak = float(np.max(np.abs(clip))) if clip.size else 0.0
                rms = float(np.sqrt(np.mean(clip ** 2))) if clip.size else 0.0
                if not (0.35 <= duration <= 2.2 and peak >= 0.03 and rms >= 0.003):
                    continue
                clip = np.clip(clip / peak * 0.90, -1.0, 1.0)
                output = CANDIDATES / f"candidate_{index:03d}.wav"
                sf.write(output, clip, 16000, subtype="PCM_16")
                metadata.append({"file": output.name, "source": source.name, "start_s": start / 16000, "end_s": end / 16000, "duration_s": duration, "peak": peak, "rms": rms})
                index += 1

        (CANDIDATES / "manifest.json").write_text(json.dumps(metadata, indent=2))
        print(f"Saved {index} candidates to {CANDIDATES}")
        if index < 35:
            raise RuntimeError("Fewer than 35 plausible phrases were found. Record more isolated utterances; do not duplicate low-quality clips yet.")
        '''
    ),
    code(
        r'''
        # Cell 4 — listen to every candidate.  Delete anything that is not the
        # full target phrase, clipped, or dominated by noise, then run Cell 5.
        from IPython.display import Audio, HTML, display

        for path in sorted(CANDIDATES.glob("candidate_*.wav")):
            display(HTML(f"<b>{path.name}</b>"))
            display(Audio(filename=str(path), rate=16000))

        print("After review, delete bad candidates in the Colab file pane. Keep at least 35.")
        '''
    ),
    code(
        r'''
        # Cell 5 — make non-leaky train/held-out splits and materialize real seeds.
        import math
        import random
        import wave
        import zipfile

        def safe_extract(archive: Path, destination: Path):
            with zipfile.ZipFile(archive) as zf:
                for member in zf.infolist():
                    target = (destination / member.filename).resolve()
                    if destination.resolve() not in target.parents and target != destination.resolve():
                        raise RuntimeError(f"Unsafe ZIP path: {member.filename}")
                zf.extractall(destination)

        BACKGROUND_RAW = WORK / "background_raw"
        if BACKGROUND_RAW.exists():
            shutil.rmtree(BACKGROUND_RAW)
        BACKGROUND_RAW.mkdir()
        for archive in archives:
            safe_extract(archive, BACKGROUND_RAW)

        chosen = sorted(CANDIDATES.glob("candidate_*.wav"))
        if len(chosen) < 35:
            raise RuntimeError(f"Only {len(chosen)} reviewed positives remain; record more before training.")
        rng = random.Random(20261001)
        rng.shuffle(chosen)
        holdout_n = max(8, math.ceil(len(chosen) * 0.20))
        held_out, train_seed = chosen[:holdout_n], chosen[holdout_n:]

        MODEL_NAME = "hey_kakarot"
        MODEL_DIR = WORK / "my_custom_model" / MODEL_NAME
        if MODEL_DIR.exists():
            shutil.rmtree(MODEL_DIR)
        train_dir = MODEL_DIR / "positive_train"
        test_dir = MODEL_DIR / "positive_test"
        train_dir.mkdir(parents=True)
        test_dir.mkdir()

        # 20% of positives are real audio and 80% will be generated TTS.  Repeated
        # seed files are only used on the training side; augmentation makes each
        # exposure different.  No original recording crosses the split boundary.
        REAL_TRAIN_TARGET = 4000
        REAL_TEST_TARGET = 400
        def materialize(paths, count, destination, prefix):
            for index in range(count):
                source = paths[index % len(paths)]
                shutil.copy2(source, destination / f"{prefix}_{index:05d}_{source.stem}.wav")

        materialize(train_seed, REAL_TRAIN_TARGET, train_dir, "real_train")
        materialize(held_out, REAL_TEST_TARGET, test_dir, "real_heldout")

        # Convert background files to 16 kHz PCM16, then reserve 20% for the
        # post-training false-positive check.  Those files never enter augmentation.
        source_background = [p for p in BACKGROUND_RAW.rglob("*") if p.suffix.lower() in audio_extensions]
        if len(source_background) < 8:
            raise RuntimeError("The background ZIP needs at least 8 clips (clicks, typing, room noise, near-miss speech).")
        rng.shuffle(source_background)
        eval_background, train_background = source_background[:max(2, math.ceil(len(source_background) * 0.2))], source_background[max(2, math.ceil(len(source_background) * 0.2)):]
        BG_TRAIN, BG_EVAL = WORK / "background_train", WORK / "background_eval"
        for folder in (BG_TRAIN, BG_EVAL):
            if folder.exists():
                shutil.rmtree(folder)
            folder.mkdir()

        def convert_background(paths, destination):
            for index, source in enumerate(paths):
                output = destination / f"background_{index:03d}.wav"
                run(["ffmpeg", "-y", "-v", "error", "-i", source, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", output])

        convert_background(train_background, BG_TRAIN)
        convert_background(eval_background, BG_EVAL)
        background_seconds = 0.0
        for path in BG_TRAIN.glob("*.wav"):
            with wave.open(str(path), "rb") as wav:
                background_seconds += wav.getnframes() / wav.getframerate()
        if background_seconds < 30:
            raise RuntimeError(f"Only {background_seconds:.1f}s of training background. Supply at least 30 seconds, preferably several minutes.")

        print(f"real train seeds: {len(train_seed)} -> {len(list(train_dir.glob('*.wav')))} augmented inputs")
        print(f"held-out real seeds: {len(held_out)} -> {len(list(test_dir.glob('*.wav')))} validation inputs")
        print(f"background: {background_seconds:.1f}s train; {len(list(BG_EVAL.glob('*.wav')))} held-out files")
        (WORK / "split_manifest.json").write_text(json.dumps({"train_seed": [p.name for p in train_seed], "held_out": [p.name for p in held_out]}, indent=2))
        '''
    ),
    code(
        r'''
        # Cell 6 — download RIRs/features and write the training config.
        import yaml
        import numpy as np
        from datasets import load_dataset

        RIRS = WORK / "mit_rirs"
        RIRS.mkdir(exist_ok=True)
        if len(list(RIRS.glob("*.wav"))) < 50:
            for old in RIRS.glob("*.wav"):
                old.unlink()
            dataset = load_dataset("davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True)
            for index, row in enumerate(dataset):
                audio = row["audio"]
                sf.write(RIRS / f"rir_{index:04d}.wav", audio["array"], audio["sampling_rate"], subtype="PCM_16")
                if index >= 199:
                    break
        if len(list(RIRS.glob("*.wav"))) < 50:
            raise RuntimeError("RIR download produced too few files; stop rather than silently training without reverb.")

        def fetch(url: str, destination: Path, minimum_bytes: int):
            if not destination.exists() or destination.stat().st_size < minimum_bytes:
                run(["wget", "-q", "-c", "-O", destination, url])
            if destination.stat().st_size < minimum_bytes:
                raise RuntimeError(f"Incomplete download: {destination.name}")

        validation_features = WORK / "validation_set_features.npy"
        training_features = WORK / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
        fetch("https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy", validation_features, 100_000_000)
        fetch("https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy", training_features, 1_000_000_000)

        cfg = yaml.safe_load((OWW / "examples" / "custom_model.yml").read_text())
        cfg.update({
            "model_name": MODEL_NAME,
            "target_phrase": ["hey kakarot"],
            "custom_negative_phrases": [
                "hey carrot", "hey karate", "hey carla", "hey kartik", "hey cameron",
                "kakarot", "carrot", "karate", "okay carrot", "hey computer",
            ],
            "output_dir": str(WORK / "my_custom_model"),
            "piper_sample_generator_path": str(PIPER),
            "n_samples": 20_000,
            "n_samples_val": 2_000,
            "tts_batch_size": 32,
            "augmentation_rounds": 1,
            "augmentation_batch_size": 16,
            "background_paths": [str(BG_TRAIN)],
            # train.py requires at least one background path per augmentation batch.
            "background_paths_duplication_rate": [4],
            "rir_paths": [str(RIRS)],
            "false_positive_validation_data_path": str(validation_features),
            "feature_data_files": {"ACAV100M_sample": str(training_features)},
            # Give positive examples a meaningful share of each batch.  The old run
            # used only 50 positives against 1,074 negatives and collapsed to 0 recall.
            "batch_n_per_class": {"ACAV100M_sample": 1024, "adversarial_negative": 128, "positive": 256},
            "model_type": "dnn",
            "layer_size": 32,
            "steps": 20_000,
            "max_negative_weight": 25,
            "target_false_positives_per_hour": 1.0,
        })
        config_path = WORK / "my_model.yaml"
        config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))

        assert cfg["target_phrase"] == ["hey kakarot"]
        assert len(list(train_dir.glob("*.wav"))) == REAL_TRAIN_TARGET
        assert len(list(test_dir.glob("*.wav"))) == REAL_TEST_TARGET
        assert len(list(BG_TRAIN.glob("*.wav"))) * cfg["background_paths_duplication_rate"][0] >= cfg["augmentation_batch_size"]
        print(config_path.read_text())
        print(f"RIRs: {len(list(RIRS.glob('*.wav')))}; real train/test: {REAL_TRAIN_TARGET}/{REAL_TEST_TARGET}")
        '''
    ),
    code(
        r'''
        # Cell 7 — generate only the missing synthetic positives and phonetic negatives.
        # The real seed files already occupy 20% of each positive directory.
        run([sys.executable, str(train_py), "--training_config", config_path, "--generate_clips"])
        for subdirectory, minimum in (("positive_train", 20_000), ("positive_test", 2_000), ("negative_train", 20_000), ("negative_test", 2_000)):
            count = len(list((MODEL_DIR / subdirectory).glob("*.wav")))
            print(subdirectory, count)
            if count < minimum * 0.95:
                raise RuntimeError(f"{subdirectory} is incomplete: {count}")
        '''
    ),
    code(
        r'''
        # Cell 8 — augment and compute feature arrays.  `--overwrite` is deliberate:
        # stale arrays from the failed training run must never be reused.
        run([sys.executable, str(train_py), "--training_config", config_path, "--augment_clips", "--overwrite"])
        for name in ("positive_features_train.npy", "negative_features_train.npy", "positive_features_test.npy", "negative_features_test.npy"):
            path = MODEL_DIR / name
            values = np.load(path, mmap_mode="r")
            print(name, values.shape, values.dtype)
            if values.shape[0] == 0:
                raise RuntimeError(f"Empty feature array: {name}")
        '''
    ),
    code(
        r'''
        # Cell 9 — train and export ONNX only.  Do NOT pass --convert_to_tflite.
        run([sys.executable, str(train_py), "--training_config", config_path, "--train_model"])
        onnx_path = WORK / "my_custom_model" / f"{MODEL_NAME}.onnx"
        if not onnx_path.exists() or onnx_path.stat().st_size < 10_000:
            raise RuntimeError("Training did not produce a usable ONNX model.")
        import onnx
        import onnxruntime as ort
        onnx.checker.check_model(str(onnx_path))
        session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        print("ONNX export verified:", onnx_path, "inputs:", [item.name for item in session.get_inputs()])
        '''
    ),
    code(
        r'''
        # Cell 10 — held-out score gate.  This is the required quality check.
        import statistics
        from openwakeword.model import Model

        def load_runtime_model(path):
            # Supports both the recent and older openWakeWord constructor names.
            attempts = [
                {"wakeword_models": [str(path)], "inference_framework": "onnx"},
                {"wakeword_model_paths": [str(path)]},
            ]
            errors = []
            for kwargs in attempts:
                try:
                    return Model(**kwargs)
                except Exception as exc:
                    errors.append(repr(exc))
            raise RuntimeError("Could not load exported model: " + " | ".join(errors))

        runtime_model = load_runtime_model(onnx_path)
        def score_wav(path):
            audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
            if getattr(audio, "ndim", 1) > 1:
                audio = audio.mean(axis=1)
            if sample_rate != 16000:
                audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)
            samples = np.clip(audio * 32767, -32768, 32767).astype("<i2")
            reset = getattr(runtime_model, "reset", None)
            if callable(reset):
                reset()
            peak = 0.0
            for offset in range(0, len(samples), 1280):
                frame = samples[offset:offset + 1280]
                if len(frame) < 1280:
                    frame = np.pad(frame, (0, 1280 - len(frame)))
                prediction = runtime_model.predict(frame)
                if isinstance(prediction, dict):
                    key = onnx_path.stem if onnx_path.stem in prediction else next(iter(prediction))
                    peak = max(peak, float(prediction[key]))
                else:
                    peak = max(peak, float(prediction))
            return peak

        heldout_scores = {path.name: score_wav(path) for path in held_out}
        negative_scores = {path.name: score_wav(path) for path in sorted(BG_EVAL.glob("*.wav"))}
        selected_threshold = None
        for threshold in np.arange(0.20, 0.96, 0.05):
            recall = sum(value >= threshold for value in heldout_scores.values()) / len(heldout_scores)
            false_clip_rate = sum(value >= threshold for value in negative_scores.values()) / len(negative_scores)
            if recall >= 0.80 and false_clip_rate <= 0.05:
                selected_threshold = float(round(threshold, 2))
                break
        report = {
            "model": onnx_path.name,
            "threshold": selected_threshold,
            "heldout_scores": heldout_scores,
            "negative_scores": negative_scores,
            "heldout_mean": float(statistics.mean(heldout_scores.values())),
            "negative_max": float(max(negative_scores.values())),
        }
        report_path = WORK / "validation_report.json"
        report_path.write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        if selected_threshold is None:
            raise RuntimeError(
                "MODEL REJECTED: no threshold reached >=80% held-out recall with <=5% false-positive clips. "
                "Add more independently recorded positives and hard negatives, then retrain; do not deploy this ONNX."
            )
        print(f"MODEL ACCEPTED at threshold {selected_threshold}.  This is a small held-out corpus, not a substitute for long live-room testing.")
        '''
    ),
    code(
        r'''
        # Cell 11 — download only a model that passed Cell 10.
        from google.colab import files
        files.download(str(onnx_path))
        files.download(str(report_path))
        print("Download both files, then promote the ONNX through the repository's installer/gate.")
        '''
    ),
]


notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(json.dumps(notebook, indent=1) + "\n")
print(OUTPUT)
