from __future__ import annotations

import unittest
from unittest.mock import Mock

from kakarot_daemon import daemon
from kakarot_daemon.state import DaemonState


class ColdStartTests(unittest.TestCase):
    def test_cold_start_initializes_muted_and_releases_the_input_device(self):
        state = DaemonState()
        audio = Mock()

        daemon._initialize_cold_start(state, audio)

        snapshot = state.snapshot()
        self.assertTrue(snapshot["muted"])
        self.assertFalse(snapshot["listening"])
        self.assertEqual(snapshot["phase"], "IDLE_MUTED")
        audio.set_muted.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
