"""Capture boundary regressions; no microphone, model, or GUI is required."""

import queue
import threading
import unittest
from unittest.mock import Mock, patch

from kakarot_daemon.audio import AudioBridge, INPUT_FRAMES, SAMPLE_WIDTH


class CaptureQueueTests(unittest.TestCase):
    def setUp(self):
        self.bridge = AudioBridge(input_device=None, output_device=None)
        self.bridge._muted = False
        self.addCleanup(self.bridge.close)
        self.frame = b"\x00\x10" * INPUT_FRAMES

    def flush(self):
        pending = self.bridge.audio_queue
        with pending.all_tasks_done:
            self.assertTrue(pending.all_tasks_done.wait_for(
                lambda: pending.unfinished_tasks == 0, timeout=2.0,
            ), "Capture worker did not drain its queue")

    def feed(self, pcm):
        self.bridge._capture_callback(pcm, len(pcm) // SAMPLE_WIDTH, None, None)

    def test_callback_only_copies_and_enqueues(self):
        pcm = bytearray(self.frame)
        with patch.object(self.bridge, "_process_capture_frame") as process:
            self.feed(pcm)
            pcm[:] = b"\0" * len(pcm)
            self.assertEqual(self.bridge.audio_queue.get_nowait(), self.frame)
            self.bridge.audio_queue.task_done()
            process.assert_not_called()
            self.assertTrue(self.bridge._input.empty())

    def test_full_queue_drops_frames_and_muted_callback_does_nothing(self):
        for _ in range(101):
            self.feed(self.frame)
        self.assertEqual(self.bridge.audio_queue.qsize(), 100)
        self.assertEqual(self.bridge.dropped_input_chunks, 1)
        self.bridge._muted = True
        self.feed(self.frame)
        self.assertEqual(self.bridge.dropped_input_chunks, 1)

    def test_uneven_and_odd_chunks_preserve_exact_frames(self):
        self.bridge._start_capture_worker()
        raw = self.frame + b"\x00\x20" * INPUT_FRAMES
        for fragment in (raw[:1], raw[1:17], raw[17:1301], raw[1301:]):
            self.feed(fragment)
        self.flush()
        self.assertEqual(self.bridge._input.get_nowait(), raw[:len(self.frame)])
        self.assertEqual(self.bridge._input.get_nowait(), raw[len(self.frame):])
        self.assertEqual(self.bridge._capture_buffer, bytearray())

    def test_mute_discards_partial_and_queued_audio(self):
        self.bridge._start_capture_worker()
        self.feed(self.frame[:13])
        self.flush()
        self.assertEqual(len(self.bridge._capture_buffer), 13)
        self.bridge.set_muted(True)
        self.assertFalse(self.bridge._capture_buffer)
        self.assertTrue(self.bridge.audio_queue.empty())
        self.bridge.set_muted(False)
        self.feed(self.frame)
        self.flush()
        self.assertEqual(self.bridge._input.get_nowait(), self.frame)
        self.assertTrue(self.bridge._input.empty())

    def test_dequeued_audio_cannot_cross_discard_boundary(self):
        removed = threading.Event()

        class ObservedQueue(queue.Queue):
            def get(self, *args, **kwargs):
                value = super().get(*args, **kwargs)
                removed.set()
                return value

        old = self.bridge.audio_queue = ObservedQueue(maxsize=100)
        with self.bridge._capture_lock:
            self.feed(self.frame)
            self.bridge._start_capture_worker()
            self.assertTrue(removed.wait(1))
            self.bridge.discard_input()
        with old.all_tasks_done:
            self.assertTrue(old.all_tasks_done.wait_for(lambda: old.unfinished_tasks == 0, 2))
        self.assertTrue(self.bridge._input.empty())
        self.assertFalse(self.bridge._capture_buffer)

    def test_analysis_and_wake_notifications_run_on_worker_and_are_throttled(self):
        threads = []
        levels = []
        self.bridge.set_level_listener(lambda level: (threads.append(threading.get_ident()), levels.append(level)))
        self.bridge._wake_listener = Mock()
        self.bridge._wake_listener.feed.side_effect = lambda *args: threads.append(threading.get_ident())
        with patch.object(self.bridge, "_level_payload", return_value={"active": True}):
            # A backlog drained at once must not burst 20 UI notifications.
            with patch("kakarot_daemon.audio.time.monotonic", return_value=100.0):
                for _ in range(20):
                    self.feed(self.frame)
                self.bridge._start_capture_worker()
                self.flush()
        self.assertEqual(len(levels), 1)
        self.assertEqual(self.bridge._wake_listener.feed.call_count, 20)
        self.assertEqual(set(threads), {self.bridge._capture_worker.ident})
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_failed_frame_does_not_kill_worker(self):
        with patch.object(self.bridge, "_process_capture_frame", side_effect=[RuntimeError("bad frame"), None]) as process:
            with self.assertLogs("kakarot_daemon.audio", level="WARNING"):
                self.bridge._start_capture_worker()
                self.feed(self.frame * 2)
                self.flush()
            self.assertEqual(process.call_count, 2)
            self.assertTrue(self.bridge._capture_worker.is_alive())

    def test_worker_is_single_instance_and_joins_on_close(self):
        self.bridge._start_capture_worker()
        worker = self.bridge._capture_worker
        self.bridge._start_capture_worker()
        self.assertIs(self.bridge._capture_worker, worker)
        self.bridge.close()
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.bridge._capture_worker)
        self.bridge._start_capture_worker()
        self.assertIsNot(self.bridge._capture_worker, worker)


if __name__ == "__main__":
    unittest.main()
