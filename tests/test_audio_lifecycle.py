from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from google.genai import types as genai_types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout

from kakarot_daemon.audio import (
    BARGE_IN_FRAMES,
    BARGE_IN_GRACE_SECONDS,
    INPUT_FRAMES,
    INPUT_RATE,
    OUTPUT_FRAMES,
    OUTPUT_RATE,
    AudioBridge,
)
from kakarot_daemon.control import ControlServer
from kakarot_daemon.live import KakarotLiveService
from kakarot_overlay.mascot import KakarotMascot
from kakarot_overlay.settings_dialog import SettingsDialog


class _Stream:
    def __init__(self, *, stop_error=False):
        self.active = True
        self.stop_error = stop_error
        self.stopped = 0
        self.closed = 0
        self.started = 0

    def start(self):
        self.started += 1
        self.active = True

    def stop(self):
        self.stopped += 1
        if self.stop_error:
            raise RuntimeError("stop failed")
        self.active = False

    def close(self):
        self.closed += 1


def _tone(frames, rate, freq, amplitude=0.4):
    """One int16 PCM frame of a sine tone."""
    t = np.arange(frames) / rate
    return (np.sin(2 * np.pi * freq * t) * amplitude * 32767).astype("<i2").tobytes()


class BargeInTests(unittest.TestCase):
    def _bridge(self, *, rms_frames):
        bridge = AudioBridge(input_device=None, output_device=None)
        bridge._input_stream = _Stream()
        bridge.audio_available = True
        bridge._muted = False
        bridge._output_limit = OUTPUT_FRAMES * 2 * 50
        bridge._monitor_only = True
        bridge._monitor_started_at = time.monotonic() - BARGE_IN_GRACE_SECONDS - 1
        bridge._rms_frames = rms_frames
        bridge._start_capture_worker()
        self.addCleanup(bridge.close)
        return bridge

    def _feed(self, bridge, count):
        for _ in range(count):
            bridge._capture_callback(bridge._rms_frames, INPUT_FRAMES, None, None)
        with bridge.audio_queue.all_tasks_done:
            self.assertTrue(bridge.audio_queue.all_tasks_done.wait_for(
                lambda: bridge.audio_queue.unfinished_tasks == 0, timeout=2.0,
            ))

    def test_sustained_speech_over_playback_clears_the_speaker(self):
        bridge = self._bridge(rms_frames=_tone(INPUT_FRAMES, INPUT_RATE, 500))
        bridge.push_output(_tone(OUTPUT_FRAMES, OUTPUT_RATE, 500))
        interrupted = []
        bridge.set_barge_in_listener(lambda: interrupted.append(True))

        self._feed(bridge, BARGE_IN_FRAMES - 1)
        self.assertFalse(bridge.barge_in_active)
        self.assertGreater(bridge.pending_output_bytes, 0)

        self._feed(bridge, 1)
        self.assertTrue(bridge.barge_in_active)
        self.assertEqual(bridge.pending_output_bytes, 0)
        self.assertEqual(interrupted, [True])

    def test_quiet_frames_do_not_interrupt(self):
        bridge = self._bridge(rms_frames=_tone(INPUT_FRAMES, INPUT_RATE, 500, amplitude=0.001))
        bridge.push_output(_tone(OUTPUT_FRAMES, OUTPUT_RATE, 500))

        self._feed(bridge, 20)

        self.assertFalse(bridge.barge_in_active)
        # The monitor stays armed and the answer keeps playing.
        self.assertTrue(bridge._monitor_only)
        self.assertGreater(bridge.pending_output_bytes, 0)

    def test_assistant_echo_inside_the_grace_window_cannot_interrupt(self):
        bridge = self._bridge(rms_frames=_tone(INPUT_FRAMES, INPUT_RATE, 500))
        bridge.push_output(_tone(OUTPUT_FRAMES, OUTPUT_RATE, 500))
        bridge.start_playback_monitor()
        bridge._monitor_started_at = time.monotonic()

        self._feed(bridge, 10)

        self.assertFalse(bridge.barge_in_active)
        self.assertGreater(bridge.pending_output_bytes, 0)

    def test_armed_frames_are_dropped_so_the_assistant_cannot_transcribe_itself(self):
        bridge = self._bridge(rms_frames=_tone(INPUT_FRAMES, INPUT_RATE, 500, amplitude=0.001))
        fed = []
        listener = Mock()
        listener.feed = lambda *args: fed.append(args)
        bridge._wake_listener = listener

        self._feed(bridge, 10)

        self.assertEqual(bridge._input.qsize(), 0)
        self.assertEqual(fed, [])

    def test_interrupting_frames_are_replayed_into_the_next_turn(self):
        bridge = self._bridge(rms_frames=_tone(INPUT_FRAMES, INPUT_RATE, 500))
        fed = []
        listener = Mock()
        listener.feed = lambda *args: fed.append(args)
        bridge._wake_listener = listener

        self._feed(bridge, BARGE_IN_FRAMES)

        self.assertTrue(bridge.barge_in_active)
        self.assertEqual(bridge._input.qsize(), BARGE_IN_FRAMES)
        self.assertEqual(len(fed), BARGE_IN_FRAMES)
        # Normal capture resumes, so the following words reach the wake worker.
        self._feed(bridge, 1)
        self.assertEqual(len(fed), BARGE_IN_FRAMES + 1)

    def test_sleep_and_shutdown_release_the_monitor(self):
        bridge = self._bridge(rms_frames=_tone(INPUT_FRAMES, INPUT_RATE, 500))
        bridge.start_playback_monitor()
        bridge.sleep_microphone()

        self.assertFalse(bridge._monitor_only)
        self.assertFalse(bridge.barge_in_active)

    def test_arming_is_skipped_without_a_live_microphone(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        bridge._muted = True

        bridge.start_playback_monitor()

        self.assertFalse(bridge._monitor_only)


class _SynthesisNoiseAudio:
    """Audio double that treats noise as barge-in only while monitoring."""

    def __init__(self):
        self.muted = True
        self.audio_available = True
        self.barge_in_active = False
        self.monitor_armed = False
        self.monitor_armed_during_synthesis = None
        self.mute_epoch = 0
        self.output = bytearray()
        self.pending_output_bytes = 0
        self.events = []

    def set_barge_in_listener(self, _listener):
        pass

    def reset_barge_in(self):
        self.barge_in_active = False
        self.events.append(("reset_barge_in",))

    def set_muted(self, muted, *, preserve_input=False):
        self.muted = bool(muted)
        if muted:
            self.mute_epoch += 1
        self.events.append(("muted", self.muted))

    def discard_input(self):
        self.events.append(("discard",))

    def start_playback_monitor(self):
        self.monitor_armed = True
        self.events.append(("monitor", True))

    def stop_playback_monitor(self):
        self.monitor_armed = False
        self.events.append(("monitor", False))

    def emit_room_noise(self):
        if not self.monitor_armed:
            self.events.append(("noise_ignored",))
            return
        self.barge_in_active = True
        self.output.clear()
        self.pending_output_bytes = 0
        self.events.append(("barge_in",))

    async def queue_output(self, pcm):
        if self.barge_in_active:
            self.events.append(("queue_dropped", len(pcm)))
            return
        self.output.extend(pcm)
        self.pending_output_bytes = 0
        self.events.append(("queue", len(pcm)))

    def clear_playback(self):
        self.output.clear()
        self.pending_output_bytes = 0
        self.events.append(("clear",))


class SynthesisGateTests(unittest.TestCase):
    def test_noise_during_synthesis_cannot_interrupt_or_drop_the_answer(self):
        audio = _SynthesisNoiseAudio()
        pcm = _tone(OUTPUT_FRAMES, OUTPUT_RATE, 440)

        class Models:
            async def generate_content(self, **_kwargs):
                audio.monitor_armed_during_synthesis = audio.monitor_armed
                audio.emit_room_noise()
                return types.SimpleNamespace(
                    candidates=[
                        types.SimpleNamespace(
                            content=types.SimpleNamespace(
                                parts=[
                                    types.SimpleNamespace(
                                        inline_data=genai_types.Blob(
                                            data=pcm,
                                            mime_type="audio/pcm;rate=24000",
                                        )
                                    )
                                ]
                            )
                        )
                    ]
                )

        service = KakarotLiveService(
            api_key="test",
            model="test-model",
            audio=audio,
            tools=types.SimpleNamespace(),
            tts_model="test-tts",
            barge_in_enabled=True,
        )
        service._types = genai_types
        service._client = types.SimpleNamespace(models=Models())

        asyncio.run(service.speak("Speak this answer."))

        self.assertFalse(audio.monitor_armed_during_synthesis)
        self.assertFalse(audio.barge_in_active)
        self.assertFalse(audio.monitor_armed)
        self.assertEqual(bytes(audio.output), pcm)
        self.assertNotIn("queue_dropped", [event[0] for event in audio.events])
        noise_index = audio.events.index(("noise_ignored",))
        monitor_index = audio.events.index(("monitor", True))
        queue_index = audio.events.index(("queue", len(pcm)))
        self.assertLess(noise_index, monitor_index)
        self.assertLess(monitor_index, queue_index)


class AudioLifecycleTests(unittest.TestCase):
    def test_shutdown_closes_each_stream_even_if_stop_raises(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        input_stream = _Stream(stop_error=True)
        output_stream = _Stream()
        bridge._input_stream = input_stream
        bridge._output_stream = output_stream
        bridge.audio_available = True
        bridge._muted = False

        bridge.close()

        self.assertEqual(input_stream.closed, 1)
        self.assertEqual(output_stream.closed, 1)
        self.assertIsNone(bridge._input_stream)
        self.assertIsNone(bridge._output_stream)
        self.assertTrue(bridge.muted)
        self.assertFalse(bridge.audio_available)

    def test_sleep_releases_microphone_and_stops_wake_worker(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        stream = _Stream()
        listener = Mock()
        bridge._input_stream = stream
        bridge._wake_listener = listener
        bridge.audio_available = True
        bridge._muted = False
        epoch = bridge.mute_epoch

        bridge.sleep_microphone()

        self.assertEqual(stream.stopped, 1)
        self.assertEqual(stream.closed, 1)
        listener.stop.assert_called_once_with()
        self.assertIsNone(bridge._input_stream)
        self.assertIsNone(bridge._wake_listener)
        self.assertTrue(bridge.muted)
        self.assertFalse(bridge.audio_available)
        # An in-flight answer must see this as an external mute and keep the
        # microphone closed when it restores follow-up listening.
        self.assertGreater(bridge.mute_epoch, epoch)

    def test_manual_wake_reopens_released_stream(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        self.addCleanup(bridge.close)
        opened = _Stream()
        sounddevice = types.SimpleNamespace(RawInputStream=lambda **_kwargs: opened)
        with patch.dict(sys.modules, {"sounddevice": sounddevice}), patch.object(
            bridge, "_start_wake_listener"
        ) as start_listener:
            self.assertTrue(bridge.manual_wake())

        self.assertEqual(opened.started, 1)
        self.assertIs(bridge._input_stream, opened)
        self.assertFalse(bridge.muted)
        start_listener.assert_called_once_with()

    def test_failed_reopen_closes_stream_and_stops_capture_worker(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        self.addCleanup(bridge.close)
        opened = _Stream()
        opened.start = Mock(side_effect=RuntimeError("device unplugged"))
        sounddevice = types.SimpleNamespace(RawInputStream=lambda **_kwargs: opened)
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            with self.assertLogs("kakarot_daemon.audio", level="WARNING"):
                self.assertFalse(bridge.manual_wake())
        self.assertEqual(opened.closed, 1)
        self.assertIsNone(bridge._capture_worker)
        self.assertTrue(bridge.audio_queue.empty())

    def test_start_keeps_input_closed_until_an_explicit_unmute(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        self.addCleanup(bridge.close)
        capture, output = _Stream(), _Stream()
        capture.active = False
        sounddevice = types.SimpleNamespace(
            RawInputStream=lambda **_kwargs: capture,
            RawOutputStream=lambda **_kwargs: output,
        )
        with patch.dict(sys.modules, {"sounddevice": sounddevice}), patch.object(
            bridge, "_start_wake_listener"
        ):
            bridge.start()

        self.assertEqual(capture.started, 0)
        self.assertEqual(output.started, 1)
        self.assertTrue(bridge.muted)
        self.assertTrue(bridge.audio_available)

        bridge.set_muted(False)
        self.assertEqual(capture.started, 1)

    def test_failed_start_joins_worker_and_closes_both_devices(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        self.addCleanup(bridge.close)
        capture, output = _Stream(), _Stream()
        output.start = Mock(side_effect=RuntimeError("speaker unavailable"))
        sounddevice = types.SimpleNamespace(
            RawInputStream=lambda **_kwargs: capture,
            RawOutputStream=lambda **_kwargs: output,
        )
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            with self.assertLogs("kakarot_daemon.audio", level="ERROR"):
                bridge.start()
        self.assertEqual(capture.closed, 1)
        self.assertEqual(output.closed, 1)
        self.assertIsNone(bridge._capture_worker)
        self.assertFalse(bridge.audio_available)

    def test_mute_stop_failure_still_discards_capture_backlog(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        self.addCleanup(bridge.close)
        bridge._input_stream = _Stream(stop_error=True)
        bridge.audio_queue.put_nowait(b"old audio")
        bridge._capture_buffer.extend(b"old partial")
        with self.assertRaises(RuntimeError):
            bridge.set_muted(True)
        self.assertTrue(bridge.muted)
        self.assertTrue(bridge.audio_queue.empty())
        self.assertFalse(bridge._capture_buffer)


class SleepMicProtocolTests(unittest.IsolatedAsyncioTestCase):
    def _server(self, callback):
        return ControlServer(
            Path("/tmp/kakarot-control-test.sock"),
            mute=lambda _muted: None,
            status=lambda: {"phase": "IDLE_MUTED"},
            stop=lambda: None,
            sleep_mic=callback,
        )

    async def test_mascot_sleep_event_releases_capture(self):
        released = []
        response = await self._server(lambda: released.append(True)).dispatch_message(
            {
                "sender": "mascot",
                "type": "system",
                "payload": {"action": "sleep_mic"},
            }
        )
        self.assertEqual(response, {"ok": True})
        self.assertEqual(released, [True])

    async def test_unknown_action_and_callback_error_are_contained(self):
        callback = Mock(side_effect=RuntimeError("device error"))
        server = self._server(callback)
        invalid = await server.dispatch_message(
            {"sender": "mascot", "type": "system", "payload": {"action": "other"}}
        )
        failed = await server.dispatch_message(
            {"sender": "mascot", "type": "system", "payload": {"action": "sleep_mic"}}
        )
        self.assertFalse(invalid["ok"])
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"], "could not release microphone")
        callback.assert_called_once_with()


class AudioControlEventTests(unittest.IsolatedAsyncioTestCase):
    def _server(self, callback, *, status=None):
        return ControlServer(
            Path("/tmp/kakarot-audio-control-test.sock"),
            mute=lambda _muted: None,
            status=status or (lambda: {"phase": "IDLE", "output_muted": True}),
            stop=lambda: None,
            audio_control=callback,
        )

    async def test_speaker_toggle_routes_to_the_audio_callback(self):
        applied = []
        response = await self._server(applied.append).dispatch_message(
            {"sender": "mascot", "type": "audio_control", "payload": {"speaker_muted": True}}
        )
        self.assertTrue(response["ok"])
        self.assertEqual(applied, [True])
        self.assertNotIn("unknown command", json.dumps(response))

    async def test_malformed_and_unavailable_audio_control_are_rejected(self):
        server = self._server(lambda _muted: None)
        invalid = await server.dispatch_message(
            {"sender": "mascot", "type": "audio_control", "payload": {"speaker_muted": "yes"}}
        )
        self.assertFalse(invalid["ok"])
        missing = ControlServer(
            Path("/tmp/kakarot-audio-control-test.sock"),
            mute=lambda _muted: None,
            status=lambda: {},
            stop=lambda: None,
        )
        unavailable = await missing.dispatch_message(
            {"sender": "mascot", "type": "audio_control", "payload": {"speaker_muted": False}}
        )
        self.assertFalse(unavailable["ok"])
        self.assertEqual(unavailable["error"], "speaker control is unavailable")


class AudioLevelTests(unittest.TestCase):
    def test_levels_are_normalized_into_four_bands(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        frames = np.arange(INPUT_FRAMES) / INPUT_RATE
        tone = (np.sin(2 * np.pi * 180 * frames) * 12000).astype("<i2").tobytes()

        payload = bridge._level_payload(tone, INPUT_RATE)

        self.assertTrue(payload["active"])
        self.assertEqual(len(payload["bands"]), 4)
        self.assertTrue(0.0 < payload["rms"] < 1.0)
        # A 180 Hz tone belongs to the low band, not the others.
        self.assertEqual(payload["bands"].index(max(payload["bands"])), 0)

    def test_playback_levels_keep_the_waveform_alive_while_the_mic_is_gated(self):
        bridge = AudioBridge(input_device=None, output_device=None)
        published = []
        bridge.set_level_listener(published.append)
        stream = _Stream()
        bridge._output_stream = stream
        bridge._output_limit = OUTPUT_FRAMES * 2 * 50
        frames = np.arange(OUTPUT_FRAMES) / OUTPUT_RATE
        pcm = (np.sin(2 * np.pi * 1200 * frames) * 15000).astype("<i2").tobytes()
        for _ in range(4):
            bridge.push_output(pcm)
            out = bytearray(OUTPUT_FRAMES * 2)
            bridge._playback_callback(out, OUTPUT_FRAMES, None, None)

        self.assertTrue(published)
        self.assertTrue(any(event["active"] for event in published))
        self.assertTrue(all(len(event["bands"]) == 4 for event in published))


class MascotLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_controls_and_settings_are_embedded_and_close_sends_sleep(self):
        mascot = KakarotMascot()
        self.assertIsInstance(mascot.layout(), QVBoxLayout)
        self.assertIs(mascot.layout().itemAt(0).widget(), mascot.avatar)
        self.assertIs(mascot.layout().itemAt(1).widget(), mascot.dock)
        self.assertIsInstance(mascot.toolbar_layout, QHBoxLayout)
        self.assertEqual(mascot.toolbar_layout.count(), 4)
        self.assertFalse(hasattr(mascot, "settings"))

        mascot.show()
        self.app.processEvents()
        with patch.object(SettingsDialog, "exec", return_value=0) as show_modal:
            mascot.open_settings()
        show_modal.assert_called_once_with()
        self.assertGreaterEqual(mascot.width(), 140)
        self.assertEqual(
            mascot.status_led.toolTip(),
            "Kakarot daemon is unreachable. Check kakarot-core.service.",
        )

        sent = []
        mascot._send_system_event = sent.append
        mascot.close()
        self.assertEqual(sent, ["sleep_mic"])
        mascot._finish_close()
        self.app.processEvents()
        self.assertFalse(mascot.isVisible())

    def test_human_required_renders_non_modal_agency_strip(self):
        mascot = KakarotMascot()
        self.addCleanup(mascot.poll.stop)
        self.addCleanup(mascot.events_retry.stop)
        self.assertTrue(mascot.agency_strip.isHidden())

        mascot._handle_core_event(
            {
                "type": "human_required",
                "payload": {
                    "request_id": "request-123",
                    "level": "action",
                    "label": "Action required",
                    "reason": "Only you can plug in the drive.",
                    "prompt": "Insert the USB drive, then confirm.",
                    "options": ["I'm done", "Skip"],
                },
            }
        )
        self.app.processEvents()

        strip = mascot.agency_strip
        self.assertFalse(strip.isHidden())
        self.assertTrue(strip.header.text().startswith("👤 Action required"))
        self.assertIn("Only you can plug in the drive.", strip.header.text())
        self.assertEqual(
            [button.text() for button in strip.buttons], ["I'm done", "Skip"]
        )
        stylesheet = strip.styleSheet()
        # Strict monochrome terminal aesthetic: a single white border and
        # mono font, with no per-tier accent colors anywhere.
        self.assertIn("background-color: rgba(0, 0, 0, 0.85)", stylesheet)
        self.assertIn("border: 1px solid #ffffff", stylesheet)
        self.assertIn("monospace", stylesheet)
        for accent in ("#a855f7", "#f59e0b", "#38bdf8", "#ef4444"):
            self.assertNotIn(accent, stylesheet)

        emitted = []
        strip.response_selected.connect(emitted.append)
        sent = []

        def capture(event_type, payload):
            sent.append((event_type, payload))
            mascot._request = object()

        mascot._send_event = capture
        strip.buttons[0].click()
        self.app.processEvents()

        self.assertEqual(emitted, ["I'm done"])
        self.assertTrue(strip.isHidden())
        self.assertEqual(sent[0][0], "human_response")
        self.assertEqual(sent[0][1]["request_id"], "request-123")
        self.assertEqual(sent[0][1]["option"], "I'm done")
        self.assertTrue(sent[0][1]["approved"])

    def test_post_init_never_unmutes_but_explicit_mic_click_still_does(self):
        mascot = KakarotMascot()
        self.addCleanup(mascot.poll.stop)
        sent = []
        with patch.object(mascot, "_update_health"), patch.object(
            mascot, "_connect_events"
        ), patch.object(
            mascot, "_send", side_effect=lambda command, **_: sent.append(command)
        ):
            mascot._post_init()

        self.assertEqual(sent, [])

        mascot._send = Mock()
        mascot._mic_active = False
        mascot.toggle_microphone()
        mascot._send.assert_called_once_with("unmute")

    def test_muted_status_outranks_a_stale_mic_active_flag(self):
        mascot = KakarotMascot()
        self.addCleanup(mascot.poll.stop)
        mascot._request = ("status", {})
        mascot._finish(
            {
                "ok": True,
                "phase": "IDLE_MUTED",
                "muted": True,
                "listening": False,
                "mic_active": True,
                "audio_available": True,
                "connected": True,
            }
        )

        self.assertFalse(mascot._mic_active)
        self.assertEqual(mascot.mic_button.toolTip(), "Enable microphone")
        mascot._send = Mock()
        mascot.toggle_microphone()
        mascot._send.assert_called_once_with("unmute")

    def test_failed_unmute_status_does_not_look_like_listening(self):
        mascot = KakarotMascot()
        self.addCleanup(mascot.poll.stop)
        mascot._request = ("status", {})
        mascot._finish(
            {
                "ok": True,
                "phase": "LISTENING",
                "muted": False,
                "listening": True,
                "mic_active": True,
                "audio_available": False,
                "connected": True,
            }
        )

        self.assertFalse(mascot._mic_active)
        self.assertEqual(mascot.mic_button.toolTip(), "Enable microphone")

    def test_health_led_reflects_connection_not_microphone(self):
        mascot = KakarotMascot()
        mascot._daemon_connected = True
        mascot._api_connected = True
        mascot._last_error = ""
        mascot._daemon_error = ""

        mascot._mic_active = False
        mascot._update_health()
        muted_color = mascot.status_led.styleSheet()
        muted_tooltip = mascot.status_led.toolTip()
        self.assertIn(mascot.LED_GREEN, muted_color)
        self.assertIn("healthy", muted_tooltip.lower())

        mascot._mic_active = True
        mascot._update_health()
        self.assertEqual(mascot.status_led.toolTip(), muted_tooltip)
        self.assertEqual(mascot.status_led.styleSheet(), muted_color)

    def test_health_led_reports_the_actual_fault_on_hover(self):
        mascot = KakarotMascot()

        mascot._daemon_connected = False
        mascot._daemon_error = "Connection refused"
        mascot._update_health()
        self.assertIn(mascot.LED_RED, mascot.status_led.styleSheet())
        self.assertIn("unreachable", mascot.status_led.toolTip())
        self.assertIn("Connection refused", mascot.status_led.toolTip())

        mascot._daemon_connected = True
        mascot._daemon_error = ""
        mascot._api_connected = False
        mascot._last_error = ""
        mascot._update_health()
        self.assertIn(mascot.LED_RED, mascot.status_led.styleSheet())
        self.assertIn("Gemini session is disconnected", mascot.status_led.toolTip())

        mascot._api_connected = True
        mascot._last_error = "401 UNAUTHENTICATED"
        mascot._update_health()
        self.assertIn(mascot.LED_RED, mascot.status_led.styleSheet())
        self.assertIn("401 UNAUTHENTICATED", mascot.status_led.toolTip())

    def test_waveform_drives_bars_from_bands_and_decays_to_baseline(self):
        mascot = KakarotMascot()
        waveform = mascot.bubble_waveform
        mascot._audio_mode = "listening"

        waveform.update_levels([0.0, 0.5, 1.0, 0.25])
        self.assertEqual(waveform.target_heights[-2], waveform.max_height)
        waveform.update_levels([], active=False)
        self.assertEqual(waveform.target_heights, [4.0] * len(waveform.current_heights))

        mascot._set_audio_mode("speaking")
        self.assertEqual(mascot._audio_mode, "speaking")
        self.assertTrue(waveform.isVisibleTo(mascot))

        mascot._set_audio_mode("idle")
        self.assertEqual(mascot._audio_mode, "idle")

    def test_speaker_toggle_updates_local_state_and_queues_the_event(self):
        mascot = KakarotMascot()
        mascot._update_health()
        before = mascot._speaker_active
        sent = []
        mascot._send_event = lambda event_type, payload: sent.append((event_type, payload))

        mascot.toggle_speaker()

        self.assertEqual(mascot._speaker_active, not before)
        self.assertEqual(sent, [("audio_control", {"speaker_muted": before})])
        self.assertEqual(
            mascot.bubble_label.text(),
            "Voice Unactivated" if before else "Voice Activated",
        )


if __name__ == "__main__":
    unittest.main()
