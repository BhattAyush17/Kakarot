"""Bounded raw-PCM audio I/O.

Only standard buffer objects cross the real-time audio callback boundary.  The
callback never creates an asyncio task and never grows a queue without bound.
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import sys
import threading
import time
from array import array
from collections import deque
from collections.abc import Callable
from pathlib import Path

INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
SAMPLE_WIDTH = 2
CHUNK_MS = 40
INPUT_FRAMES = INPUT_RATE * CHUNK_MS // 1000
OUTPUT_FRAMES = OUTPUT_RATE * CHUNK_MS // 1000
NOISE_FLOOR_RMS = 0.004
# Normalized RMS threshold for interrupting active playback. Keep it above
# ordinary room noise while remaining reachable by a clear speaking voice.
BARGE_IN_THRESHOLD = 0.06
BARGE_IN_RMS = BARGE_IN_THRESHOLD
# Consecutive loud frames (40 ms each) used by the monitor-only fallback. A
# single burst is usually a cough, a desk knock, or speaker leakage.
BARGE_IN_FRAMES = 3
# The assistant's own first words are still leaving the speaker when the monitor
# arms, so their echo must not be able to trigger the interrupt.
BARGE_IN_GRACE_SECONDS = 0.3
# Bounded pre-roll: the frames that triggered the interrupt are handed to the
# wake worker afterwards, so the user's own words are not lost with the answer.
BARGE_IN_PREROLL_CHUNKS = 25


def _pcm_rms(pcm: bytes) -> float:
    """Normalized RMS of one int16 frame, computed without numpy.

    Used by the capture worker for barge-in and wake-word/VAD input.
    """
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += value * value
    return math.sqrt(total / len(samples)) / 32768.0


class AudioBridge:
    """Microphone capture and speaker playback with fixed memory bounds."""

    def __init__(self, *, input_device: str | int | None, output_device: str | int | None,
                 input_queue_chunks: int = 10, output_buffer_ms: int = 750,
                 wake_model_dir: Path | None = None,
                 on_wake_command: Callable[[bytes, str], None] | None = None) -> None:
        self._input_device = input_device
        self._output_device = output_device
        self._input: queue.Queue[bytes] = queue.Queue(maxsize=input_queue_chunks)
        # 100 × 40 ms = 4 s of capture. This is the hardware-callback buffer,
        # not the turn buffer; it must be deep enough to absorb a scheduler
        # hiccup without dropping speech.
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._capture_buffer = bytearray()
        # Only the worker and lifecycle methods take this lock, never PortAudio.
        self._capture_lock = threading.RLock()
        self._capture_stop = threading.Event()
        self._capture_worker: threading.Thread | None = None
        self._last_capture_level_at = 0.0
        self._output: deque[bytes] = deque()
        self._output_bytes = 0
        self._output_limit = OUTPUT_RATE * SAMPLE_WIDTH * output_buffer_ms // 1000
        self._lock = threading.Lock()
        self._input_stream = None
        self._output_stream = None
        self._muted = True
        # Monotonic counter for external mute requests. The live service uses
        # this to distinguish its own temporary playback mute from the user
        # muting the microphone while an answer is being synthesized.
        self._mute_epoch = 0
        self.audio_available = False
        self.output_muted = False
        self._wake_model_dir = wake_model_dir or (
            Path.home() / ".local" / "share" / "kakarot" / "models"
        )
        self._on_wake_command = on_wake_command
        self._wake_listener = None
        self._logger = logging.getLogger(__name__)
        self._output_listener: Callable[[bool], None] | None = None
        self._level_listener: Callable[[dict], None] | None = None
        self._output_level_elapsed = 0.0
        self._playback_active = False
        self._monitor_only = False
        self._monitor_started_at = 0.0
        self._barge_in_frames = 0
        self._barge_in_active = False
        self._barge_in_preroll: deque[bytes] = deque(maxlen=BARGE_IN_PREROLL_CHUNKS)
        self._barge_in_listener: Callable[[], None] | None = None
        self.dropped_input_chunks = 0
        self.dropped_output_bytes = 0

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def mute_epoch(self) -> int:
        return self._mute_epoch

    @property
    def pending_output_bytes(self) -> int:
        with self._lock:
            return self._output_bytes

    @property
    def wake_word_active(self) -> bool:
        """True only after the inference thread has loaded a usable model."""
        return bool(self._wake_listener and self._wake_listener.ready)

    @property
    def is_capturing(self) -> bool:
        """Whether an unmuted, live input stream is actively capturing."""
        stream = self._input_stream
        return bool(self.audio_available and not self._muted and stream and stream.active)

    @property
    def barge_in_active(self) -> bool:
        """True once the user interrupted playback during the current answer."""
        return self._barge_in_active

    def set_barge_in_listener(self, listener: Callable[[], None] | None) -> None:
        """Register a callback fired on the audio thread when speech interrupts."""
        self._barge_in_listener = listener

    def reset_barge_in(self) -> None:
        """Start a new answer with a clean interrupt state.

        Without this, a past barge-in leaves ``_barge_in_active`` set and the
        next ``queue_output`` returns before feeding a single sample.
        """
        self._barge_in_active = False
        self._barge_in_frames = 0
        self._barge_in_preroll.clear()

    def start_playback_monitor(self) -> None:
        """Watch microphone energy while the assistant speaks.

        Capture keeps running so the user can interrupt, but frames are not
        queued or fed to the wake worker: without that gate the assistant would
        transcribe its own voice as the next turn.
        """
        if self._muted or not self.audio_available:
            return
        # Each answer starts uninterrupted; the flag is per-answer state.
        self._barge_in_active = False
        self._monitor_only = True
        self._monitor_started_at = time.monotonic()
        self._barge_in_frames = 0
        self._barge_in_preroll.clear()

    def stop_playback_monitor(self) -> None:
        self._monitor_only = False
        self._monitor_started_at = 0.0
        self._barge_in_frames = 0
        self._barge_in_preroll.clear()

    def _monitor_barge_in(self, pcm: bytes, rms: float) -> bool:
        """Return True once sustained speech above threshold interrupts playback."""
        if not self._monitor_only or self._barge_in_active:
            return False
        # Ignore the tail of the assistant's own audio still leaving the speaker.
        if time.monotonic() - self._monitor_started_at < BARGE_IN_GRACE_SECONDS:
            return False
        if rms < BARGE_IN_THRESHOLD:
            self._barge_in_frames = 0
            self._barge_in_preroll.clear()
            return False
        self._barge_in_frames += 1
        self._barge_in_preroll.append(pcm)
        if self._barge_in_frames < BARGE_IN_FRAMES:
            return False
        self._barge_in_active = True
        self._barge_in_frames = 0
        preroll = list(self._barge_in_preroll)
        self.stop_playback_monitor()
        self.clear_playback()
        self._logger.info("Barge-in: speech over playback, interrupting the answer.")
        # Replay what the user just said so the next turn still has their words. The
        # triggering frame itself is skipped: it falls through to the normal
        # capture path below and would otherwise be queued twice.
        for frame in preroll[:-1]:
            self._replay_capture(frame)
        if self._barge_in_listener is not None:
            try:
                self._barge_in_listener()
            except Exception:
                self._logger.debug("Barge-in listener failed", exc_info=True)
        return True

    def _replay_capture(self, pcm: bytes) -> None:
        """Hand one recorded frame back to the normal capture path."""
        try:
            self._input.put_nowait(pcm)
        except queue.Full:
            self.dropped_input_chunks += 1
        listener = self._wake_listener
        if listener is not None:
            listener.feed(pcm, pcm, _pcm_rms(pcm))

    def set_muted(self, muted: bool, *, preserve_input: bool = False) -> None:
        """Gate microphone frames and stop the device to release the OS recording indicator.

        ``preserve_input`` is used only after a barge-in: the words already
        replayed into the turn queue must survive the temporary mute that closes
        the answer, otherwise the next turn starts after the user's interruption
        and the replayed speech is lost.
        """
        self._muted = bool(muted)
        if muted:
            self._mute_epoch += 1
            try:
                if self._input_stream is not None and self._input_stream.active:
                    self._input_stream.stop()
            finally:
                self._drain_input(
                    preserve_replay=bool(preserve_input and self._barge_in_active)
                )
                self._emit_silent_level()
        else:
            if self._input_stream is not None and not self._input_stream.active:
                self._input_stream.start()

    def manual_wake(self) -> bool:
        """Open the microphone gate for an explicit user listen request."""
        self._drain_input()
        if self._input_stream is None and not self._open_input_stream():
            return False
        self.set_muted(False)
        return self.audio_available

    def sleep_microphone(self) -> None:
        """Stop inference and release the capture device while the UI is closed."""
        self._muted = True
        stream, self._input_stream = self._input_stream, None
        if stream is not None:
            self._close_stream(stream)
        self._stop_capture_worker()
        listener, self._wake_listener = self._wake_listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception as exc:
                self._logger.debug("Wake-word worker shutdown failed: %s", exc)
        self.audio_available = False
        self._release_playback_monitor()
        self._drain_input()
        self._emit_silent_level()

    def _open_input_stream(self) -> bool:
        """Reopen a microphone released by sleep_microphone()."""
        stream = None
        try:
            import sounddevice as sd

            self._start_capture_worker()
            stream = sd.RawInputStream(
                samplerate=INPUT_RATE, channels=1, dtype="int16", blocksize=INPUT_FRAMES,
                device=self._input_device, callback=self._capture_callback,
            )
            stream.start()
        except Exception as exc:
            if stream is not None:
                self._close_stream(stream)
            self._stop_capture_worker()
            self._drain_input()
            self._logger.warning("Could not reopen microphone: %s", exc)
            self.audio_available = False
            return False
        self._input_stream = stream
        self.audio_available = True
        self._start_wake_listener()
        return True

    def _close_stream(self, stream) -> None:
        """Stop and close a PortAudio stream independently, even after errors."""
        try:
            if getattr(stream, "active", False):
                stream.stop()
        except Exception as exc:
            self._logger.debug("Audio stream stop failed: %s", exc)
        finally:
            try:
                stream.close()
            except Exception as exc:
                self._logger.debug("Audio stream close failed: %s", exc)

    def set_output_listener(self, listener: Callable[[bool], None]) -> None:
        self._output_listener = listener

    def set_level_listener(self, listener: Callable[[dict], None]) -> None:
        """Register a bounded-rate callback for normalized stream levels."""
        self._level_listener = listener
        self._last_capture_level_at = 0.0
        self._output_level_elapsed = 0.0

    def _level_payload(self, pcm: bytes, rate: int) -> dict:
        """Normalize one PCM frame into RMS plus four frequency bands."""
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        if not samples.size:
            return {"rms": 0.0, "bands": [0.0, 0.0, 0.0, 0.0], "active": False}
        rms = float(np.sqrt(np.mean(samples * samples))) / 32768.0
        active = rms > NOISE_FLOOR_RMS
        bands = [0.0, 0.0, 0.0, 0.0]
        if active:
            normalized = samples / 32768.0
            spectrum = np.abs(np.fft.rfft(normalized))
            power = spectrum * spectrum
            total_power = float(np.sum(power))
            if total_power > 0.0:
                frequencies = np.fft.rfftfreq(samples.size, d=1.0 / rate)
                ranges = ((60.0, 300.0), (300.0, 1000.0), (1000.0, 3000.0), (3000.0, 8000.1))
                gain = min(1.0, max(0.0, (rms - NOISE_FLOOR_RMS) / 0.06))
                for index, (low, high) in enumerate(ranges):
                    mask = (frequencies >= low) & (frequencies < high)
                    energy = float(np.sum(power[mask])) if np.any(mask) else 0.0
                    bands[index] = min(1.0, gain * (energy / total_power) ** 0.5)
        return {
            "rms": min(1.0, max(0.0, rms)),
            "bands": bands,
            "active": active,
        }

    def _publish_level(self, payload: dict) -> None:
        if self._level_listener is None:
            return
        try:
            self._level_listener(payload)
        except Exception:
            self._logger.debug("Stream level listener failed", exc_info=True)

    def _emit_silent_level(self) -> None:
        self._publish_level(
            {"rms": 0.0, "bands": [0.0, 0.0, 0.0, 0.0], "active": False}
        )

    def _set_playback_active(self, active: bool) -> None:
        if active == self._playback_active:
            return
        self._playback_active = active
        if self._output_listener:
            self._output_listener(active)

    def start(self) -> None:
        """Open and start the PortAudio streams.

        Degrades gracefully: a missing, busy or misconfigured device logs an
        error and leaves the bridge in text-only mode (``audio_available``
        False, capture muted) instead of crashing the daemon thread."""
        if self._input_stream is not None and self._output_stream is not None:
            return
        if self._input_stream is not None or self._output_stream is not None:
            # A manual_wake() before the supervisor reached start() can leave
            # only the input side open. Rebuild both streams instead of
            # returning and leaving the daemon permanently unable to speak.
            self.close()
        self.reset_barge_in()
        import logging
        logger = logging.getLogger(__name__)
        try:
            import sounddevice as sd
        except ImportError:
            logger.error(
                "sounddevice is not installed; audio pipeline disabled "
                "(text-only mode)."
            )
            self.audio_available = False
            self._muted = True
            return

        input_stream = output_stream = None
        try:
            self._start_capture_worker()
            input_stream = sd.RawInputStream(
                samplerate=INPUT_RATE, channels=1, dtype="int16", blocksize=INPUT_FRAMES,
                device=self._input_device, callback=self._capture_callback,
            )
            output_stream = sd.RawOutputStream(
                samplerate=OUTPUT_RATE, channels=1, dtype="int16", blocksize=OUTPUT_FRAMES,
                device=self._output_device, callback=self._playback_callback,
            )
            # A cold boot starts muted. Keep the input device closed until an
            # explicit unmute starts it, so startup cannot light the OS capture
            # indicator or briefly capture room audio.
            if not self._muted:
                input_stream.start()
            output_stream.start()
        except Exception as exc:  # device locked / unplugged / PortAudio errors
            for stream in (input_stream, output_stream):
                if stream is not None:
                    self._close_stream(stream)
            logger.error(
                "Audio device unavailable (%s); degrading to text-only mode.",
                exc,
            )
            self.audio_available = False
            self._muted = True
            self._stop_capture_worker()
            self._drain_input()
            self._input_stream = self._output_stream = None
            return

        self._input_stream = input_stream
        self._output_stream = output_stream
        self.audio_available = True
        self._start_wake_listener()

    def _start_wake_listener(self, custom_models: list[Path] | None = None) -> None:
        """Start wake-word/VAD inference in its own bounded worker thread."""
        try:
            from persona.wake_word import WakeWordListener
        except Exception as exc:
            self._logger.warning(
                "Wake-word dependencies are unavailable (%s); using push-to-talk.", exc
            )
            self._wake_listener = None
            return

        if custom_models is None:
            custom_models = [
                self._wake_model_dir / "hey_kakarot.onnx",
                self._wake_model_dir / "kakarot.onnx",
            ]
        model_paths = [str(path) for path in custom_models if path.is_file()]
        if not model_paths:
            self._logger.warning(
                "No Kakarot wake model found in %s (expected hey_kakarot.onnx or "
                "kakarot.onnx); staying in push-to-talk mode.",
                self._wake_model_dir,
            )
            self._wake_listener = None
            return
        self._logger.info("Loading Kakarot wake model(s): %s", ", ".join(model_paths))

        try:
            listener = WakeWordListener(
                {
                    "enabled": True,
                    "model_paths": model_paths,
                    "model_names": [],
                    "threshold": 0.5,
                    # openWakeWord's bundled Silero VAD gates wake predictions.
                    "vad_threshold": 0.5,
                    "command_vad_threshold": 0.004,
                    "auto_download_models": False,
                    "activation_timeout_seconds": 1.0,
                    "command_timeout_seconds": 8.0,
                    "command_silence_seconds": 0.8,
                    "preroll_seconds": 1.0,
                },
                lambda name, score: self._logger.info(
                    "Wake word detected: %s (%.3f)", name, score
                ),
                self._on_wake_command or (lambda _pcm, _reason: None),
                session_rate=INPUT_RATE,
            )
            self._wake_listener = listener
            listener.start()
        except Exception as exc:
            self._logger.warning(
                "Wake-word startup failed (%s); using push-to-talk.", exc
            )
            self._wake_listener = None

    async def watch_wake_model(self, poll_seconds: float = 5.0) -> None:
        """Hot-load the trained model when it appears or changes on disk."""
        model_paths = (
            self._wake_model_dir / "hey_kakarot.onnx",
            self._wake_model_dir / "kakarot.onnx",
        )
        try:
            self._wake_model_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._logger.warning("Cannot watch wake model directory %s: %s", self._wake_model_dir, exc)

        def signature() -> tuple[tuple[str, int, int], ...] | None:
            found = []
            for path in model_paths:
                try:
                    metadata = path.stat()
                except OSError:
                    continue
                if path.is_file():
                    found.append((path.name, metadata.st_mtime_ns, metadata.st_size))
            return tuple(found) or None

        previous = signature()
        while True:
            await asyncio.sleep(max(0.2, poll_seconds))
            current = signature()
            if current is None or current == previous:
                continue
            previous = current
            listener = self._wake_listener
            if self._input_stream is None:
                continue
            available_models = [path for path in model_paths if path.is_file()]
            if listener is not None and listener.request_model_reload(available_models):
                self._logger.info("Queued hot-load for Kakarot wake model(s): %s", available_models)
                continue

            # The fallback model could not initialize. Recreate only the wake
            # worker; the daemon, socket clients, audio streams, and chat history
            # remain untouched.
            if listener is not None:
                try:
                    listener.stop()
                except Exception as exc:
                    self._logger.debug("Old wake worker shutdown failed: %s", exc)
            self._start_wake_listener(available_models)

    def close(self) -> None:
        self._muted = True
        for stream in (self._input_stream, self._output_stream):
            if stream is not None:
                self._close_stream(stream)
        self._stop_capture_worker()
        listener, self._wake_listener = self._wake_listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception as exc:
                self._logger.debug("Wake-word worker shutdown failed: %s", exc)
        self._input_stream = self._output_stream = None
        self._muted = True
        self.audio_available = False
        self._release_playback_monitor()
        self.clear_playback()
        self._drain_input()
        self._emit_silent_level()

    def _capture_callback(self, indata, _frames: int, _time, _status) -> None:
        if self._muted:
            return
        try:
            # RawInputStream reuses its buffer after return, so enqueue an owned copy.
            in_data = bytes(indata)
            self.audio_queue.put_nowait(in_data)
        except queue.Full:
            self.dropped_input_chunks += 1

    def _start_capture_worker(self) -> None:
        if self._capture_worker is not None and self._capture_worker.is_alive():
            if self._capture_stop.is_set():
                raise RuntimeError("Previous audio worker is still shutting down")
            return
        self._capture_stop.clear()
        self._capture_worker = threading.Thread(
            target=self._process_audio_worker, name="KakarotAudioCapture", daemon=True,
        )
        self._capture_worker.start()

    def _stop_capture_worker(self) -> None:
        self._capture_stop.set()
        worker = self._capture_worker
        if worker is threading.current_thread():
            return
        if worker is not None:
            worker.join(timeout=2.0)
            if worker.is_alive():
                self._logger.warning("Audio capture worker is still shutting down")
                return
        self._capture_worker = None

    def _process_audio_worker(self) -> None:
        """Reframe PCM and analyze it away from the hardware callback.

        The existing capture/VAD path consumes 640 int16 samples (40 ms at
        16 kHz). Even byte boundaries from uneven device chunks are preserved.
        """
        chunk_bytes = INPUT_FRAMES * SAMPLE_WIDTH
        while not self._capture_stop.is_set():
            capture_queue = self.audio_queue
            try:
                pcm = capture_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                with self._capture_lock:
                    # Queue replacement invalidates frames already dequeued when
                    # mute/discard starts, as well as queued and partial frames.
                    if (self._muted or self._capture_stop.is_set()
                            or capture_queue is not self.audio_queue):
                        continue
                    self._capture_buffer.extend(pcm)
                    while len(self._capture_buffer) >= chunk_bytes:
                        frame = bytes(self._capture_buffer[:chunk_bytes])
                        del self._capture_buffer[:chunk_bytes]
                        if self._muted or self._capture_stop.is_set():
                            self._capture_buffer.clear()
                            break
                        try:
                            self._process_capture_frame(frame)
                        except Exception:
                            self._logger.warning("Audio frame processing failed", exc_info=True)
            finally:
                capture_queue.task_done()

    def _process_capture_frame(self, pcm: bytes) -> None:
        rms = _pcm_rms(pcm)
        now = time.monotonic()
        # Use wall time so draining a backlog cannot flood the socket listener.
        if self._level_listener is not None and now - self._last_capture_level_at >= 0.05:
            self._last_capture_level_at = now
            self._publish_level(self._level_payload(pcm, INPUT_RATE))
        if self._monitor_only:
            if not self._monitor_barge_in(pcm, rms):
                return
        try:
            self._input.put_nowait(pcm)
        except queue.Full:
            self.dropped_input_chunks += 1
        listener = self._wake_listener
        if listener is not None:
            # WakeWordListener owns model inference and command VAD state.
            listener.feed(pcm, pcm, rms)

    def _playback_callback(self, outdata, _frames: int, _time, _status) -> None:
        needed = len(outdata)
        active = False
        with self._lock:
            payload = bytearray()
            while self._output and len(payload) < needed:
                chunk = self._output[0]
                take = min(needed - len(payload), len(chunk))
                payload.extend(chunk[:take])
                if take == len(chunk):
                    self._output.popleft()
                else:
                    self._output[0] = chunk[take:]
                self._output_bytes -= take
            active = self._output_bytes > 0
        outdata[:] = bytes(payload).ljust(needed, b"\0")
        self._set_playback_active(active)
        # The microphone is gated while the assistant speaks, so playback levels
        # are what keeps the overlay waveform alive during a spoken answer.
        self._output_level_elapsed += needed // SAMPLE_WIDTH / OUTPUT_RATE
        if self._level_listener is not None and payload and self._output_level_elapsed >= 0.05:
            self._output_level_elapsed %= 0.05
            self._publish_level(self._level_payload(bytes(payload), OUTPUT_RATE))
        elif not payload and self._output_level_elapsed >= 0.05:
            self._output_level_elapsed = 0.0
            self._publish_level(
                {"rms": 0.0, "bands": [0.0, 0.0, 0.0, 0.0], "active": False}
            )

    async def next_input(self) -> bytes:
        while True:
            try:
                return self._input.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)

    async def queue_output(self, pcm: bytes) -> None:
        """Apply backpressure instead of dropping the tail of spoken responses."""
        size = min(OUTPUT_FRAMES * SAMPLE_WIDTH, self._output_limit)
        for offset in range(0, len(pcm), size):
            chunk = pcm[offset:offset + size]
            while self.pending_output_bytes + len(chunk) > self._output_limit:
                # An interrupt empties the buffer, so stop feeding the speaker
                # instead of resuming the answer mid-sentence.
                if self.output_muted or self._barge_in_active:
                    return
                await asyncio.sleep(.01)
            if self._barge_in_active:
                return
            self.push_output(chunk)

    def push_output(self, pcm: bytes) -> None:
        if not pcm or self.output_muted:
            return
        with self._lock:
            remaining = self._output_limit - self._output_bytes
            if remaining <= 0:
                self.dropped_output_bytes += len(pcm)
                return
            accepted = pcm[:remaining]
            self._output.append(accepted)
            self._output_bytes += len(accepted)
            self.dropped_output_bytes += len(pcm) - len(accepted)
        self._set_playback_active(True)

    def clear_playback(self) -> None:
        with self._lock:
            self._output.clear()
            self._output_bytes = 0
        self._set_playback_active(False)

    def _release_playback_monitor(self) -> None:
        self.stop_playback_monitor()
        self._barge_in_active = False

    def discard_input(self) -> None:
        self._drain_input()

    def _drain_input(self, *, preserve_replay: bool = False) -> None:
        with self._capture_lock:
            old_queue = self.audio_queue
            self.audio_queue = queue.Queue(maxsize=100)
            self._capture_buffer.clear()
            self._last_capture_level_at = 0.0
            while True:
                try:
                    old_queue.get_nowait()
                    old_queue.task_done()
                except queue.Empty:
                    break
            if preserve_replay:
                # The barge-in frames are the start of the user's next turn.
                return
            while True:
                try:
                    self._input.get_nowait()
                except queue.Empty:
                    return
