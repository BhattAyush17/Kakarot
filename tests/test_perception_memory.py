import unittest
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
import zoneinfo
import sqlite3
import tempfile
import time
from unittest.mock import patch

from kakarot_daemon.scrubber import is_app_sensitive, sanitize_text
from kakarot_daemon.memory_tools import get_recent_files, search_memory, remember_fact, schedule_reminder
from kakarot_daemon.desktop_context import read_screen_context
from kakarot_daemon.reminders import ReminderStore
import kakarot_daemon.memory_tools

class TestScrubber(unittest.TestCase):
    def test_is_app_sensitive(self):
        self.assertTrue(is_app_sensitive("keepassxc"))
        self.assertTrue(is_app_sensitive("org.gnome.Polkit"))
        self.assertFalse(is_app_sensitive("org.gnome.Nautilus"))

    def test_sanitize_text(self):
        aws_key = "AKIAIOSFODNN7EXAMPLE"
        self.assertEqual(sanitize_text(f"Key is {aws_key}."), "Key is [REDACTED_AWS_KEY].")
        
        google_key = "AIzaSyD-XYZ1234567890abcdef_A"
        self.assertIn("[REDACTED_GOOGLE_API_KEY]", sanitize_text(f"API: {google_key}"))
        
        google_aq_key = "AQ.AbCdEfGhIjKlMnOpQrStUvWxYz1234567"
        self.assertEqual(sanitize_text(f"My new key is {google_aq_key}"), "My new key is [REDACTED_SECRET]")
        
        private_key = "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----"
        self.assertIn("[REDACTED_PRIVATE_KEY]", sanitize_text(private_key))
        
        plain = "Just some normal text."
        self.assertEqual(sanitize_text(plain), plain)


class TestMemoryTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "kakarot-reminders.sqlite3"
        self.store = ReminderStore(self.db_path)
        
        # Setup mock kakarot.db for memory tools
        self.kakarot_db_path = Path(self.temp_dir.name) / "kakarot.db"
        self.conn = sqlite3.connect(self.kakarot_db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA mmap_size=268435456")
        self.conn.execute(
            "CREATE VIRTUAL TABLE user_notes USING fts5(title, body)"
        )
        self.conn.execute(
            "CREATE TABLE file_index (path TEXT, name TEXT, extension TEXT, size_bytes INTEGER, modified_at TEXT)"
        )
        self.conn.commit()
        
        # Patch the _get_kakarot_db function
        self.patcher = patch('kakarot_daemon.memory_tools._get_kakarot_db')
        self.mock_get_db = self.patcher.start()
        self.mock_get_db.side_effect = self._mock_db_connection

    def _mock_db_connection(self):
        conn = sqlite3.connect(self.kakarot_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def asyncTearDown(self):
        self.patcher.stop()
        self.conn.close()
        await self.store.close()
        self.temp_dir.cleanup()

    async def test_schedule_reminder_accuracy(self):
        res = await schedule_reminder(self.store, "Test deterministic task", relative_days=1, time_str="09:30")
        self.assertTrue(res.get("ok"))
        
        due_iso = res["due_at"]
        parsed_due = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
        tz = datetime.now().astimezone().tzinfo
        local_due = parsed_due.astimezone(tz)
        
        # Should be tomorrow in local timezone at 9:30
        self.assertEqual(local_due.hour, 9)
        self.assertEqual(local_due.minute, 30)

    def test_fts5_snippet_boundaries(self):
        remember_fact("Architecture Note", "We are building a highly robust, secure, and deterministic execution pipeline for testing boundaries on the daemon.")
        res = search_memory("deterministic")
        self.assertTrue(res.get("ok"))
        self.assertGreater(len(res["results"]), 0)
        
        snippet = res["results"][0]["snippet"]
        # FTS5 snippet function surrounds matches with '[' and ']' based on our query
        self.assertIn("[deterministic]", snippet)

    def test_recent_files_query(self):
        # Insert dummy data
        conn = self._mock_db_connection()
        conn.execute("INSERT INTO file_index VALUES ('/a', 'a', 'txt', 10, '2026-01-01T00:00:00')")
        conn.commit()
        conn.close()

        start = time.perf_counter()
        res = get_recent_files(limit=3)
        duration_ms = (time.perf_counter() - start) * 1000
        
        self.assertTrue(res.get("ok"))
        self.assertEqual(len(res["recent_files"]), 1)
        # Assuming modern systems and mmap, should be very fast. We'll set a reasonable CI threshold (50ms).
        # In a hot WAL mapped memory DB, it's often < 5ms.
        self.assertLess(duration_ms, 50.0)


class TestDesktopContext(unittest.IsolatedAsyncioTestCase):
    async def test_read_screen_context(self):
        res = await read_screen_context()
        # Even if pyatspi fails or no window is focused, it should return a safe dictionary.
        self.assertTrue("content" in res or "error" in res)
        if "content" in res:
            content = res["content"]
            # Length should not exceed 2000 chars significantly (truncation message adds a few chars)
            self.assertLessEqual(len(content), 2050)
            
            # Verify no secrets leak
            aws_key = "AKIAIOSFODNN7EXAMPLE"
            self.assertNotIn(aws_key, content)

if __name__ == '__main__':
    unittest.main()
