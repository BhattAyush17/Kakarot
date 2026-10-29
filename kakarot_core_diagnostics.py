import unittest
import asyncio
import sqlite3
import time
import json
import subprocess
import os
import sys
from pathlib import Path

# Provide module resolution for system_db
sys.path.insert(0, str(Path(__file__).parent))
from kakarot_daemon.system_db import get_db

DB_PATH = Path.home() / ".local" / "share" / "kakarot" / "kakarot.db"
SOCKET_PATH = Path.home() / ".local" / "share" / "kakarot" / "kakarot.sock"
TEST_FILE = Path.home() / "Downloads" / "kakarot_test_watch.tmp"

class KakarotCoreDiagnostics(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Ensure we have a clean test file state
        if TEST_FILE.exists():
            TEST_FILE.unlink()

    def tearDown(self):
        if TEST_FILE.exists():
            TEST_FILE.unlink()

    def test_1_memory_architecture_and_fts5(self):
        """Test DB memory constraints and FTS5 retrieval latency."""
        conn = get_db(DB_PATH.parent)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Assert Pragmas
        cursor.execute("PRAGMA journal_mode")
        self.assertEqual(cursor.fetchone()[0].lower(), 'wal', "journal_mode is not WAL")

        cursor.execute("PRAGMA mmap_size")
        self.assertGreaterEqual(cursor.fetchone()[0], 268435456, "mmap_size is beneath the 256MB threshold")

        # 2. Insert dummy note
        test_title = "Diagnostic Test Note"
        test_body = "Kakarot diagnostic threshold sequence"
        cursor.execute("INSERT INTO user_notes (title, body) VALUES (?, ?)", (test_title, test_body))
        conn.commit()
        
        try:
            # 3. FTS5 Retrieval & Execution Time Assertions
            start_time = time.perf_counter()
            cursor.execute("SELECT * FROM user_notes WHERE user_notes MATCH ?", ('diagnostic*',))
            results = cursor.fetchall()
            exec_time_ms = (time.perf_counter() - start_time) * 1000
            
            self.assertLess(exec_time_ms, 5.0, f"FTS5 query took {exec_time_ms:.2f}ms (expected < 5ms)")
            self.assertTrue(any(r['body'] == test_body for r in results), "FTS5 failed to retrieve the dummy note")
        finally:
            # 4. Cleanup
            cursor.execute("DELETE FROM user_notes WHERE title = ?", (test_title,))
            conn.commit()
            conn.close()

    async def test_2_inotify_indexing(self):
        """Test Real-Time Inotify & Background Indexing logic."""
        # Touch file
        TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        TEST_FILE.write_text("dummy inotify payload")
        
        # Await 250ms coalescing window + 100ms processing buffer
        await asyncio.sleep(0.35)
        
        conn = get_db(DB_PATH.parent)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            # Check ingestion
            cursor.execute("SELECT * FROM file_index WHERE path = ?", (str(TEST_FILE),))
            row = cursor.fetchone()
            self.assertIsNotNone(row, "File was not automatically ingested by the background watcher")
            
            # Delete file
            TEST_FILE.unlink()
            await asyncio.sleep(0.35)
            
            # Check deletion
            cursor.execute("SELECT * FROM file_index WHERE path = ?", (str(TEST_FILE),))
            row_after = cursor.fetchone()
            self.assertIsNone(row_after, "File was not purged from the index after deletion")
        finally:
            conn.close()

    async def test_3_hitl_async_suspension(self):
        """Test IPC Socket and HITL Consent Engine."""
        self.assertTrue(SOCKET_PATH.exists(), "Unix socket not found")
        
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
        
        try:
            # Send trigger payload
            trigger_payload = json.dumps({"command": "trigger_hitl", "tool": "file_deletion"}) + "\n"
            writer.write(trigger_payload.encode('utf-8'))
            await writer.drain()
            
            response_line = await reader.readline()
            response = json.loads(response_line.decode('utf-8'))
            self.assertEqual(response.get("state"), "AWAITING_CONFIRMATION", "Daemon did not enter AWAITING_CONFIRMATION state")
            
            # Send resolve payload on a fresh connection
            reader2, writer2 = await asyncio.open_unix_connection(str(SOCKET_PATH))
            try:
                resolve_payload = json.dumps({"command": "resolve_action", "approved": False}) + "\n"
                writer2.write(resolve_payload.encode('utf-8'))
                await writer2.drain()
                
                resolve_line = await reader2.readline()
                resolve_response = json.loads(resolve_line.decode('utf-8'))
                
                self.assertNotEqual(resolve_response.get("state"), "AWAITING_CONFIRMATION", "Daemon did not resume from HITL state")
            finally:
                writer2.close()
                await writer2.wait_closed()
        finally:
            writer.close()
            await writer.wait_closed()

    def test_4_cgroup_memory_ceiling(self):
        """Test systemd resource cgroup bounds."""
        result = subprocess.run(
            ["systemctl", "--user", "show", "kakarot.service", "-p", "MemoryCurrent", "-p", "MemoryHigh", "-p", "MemoryMax"],
            capture_output=True, text=True, check=True
        )
        props = dict(line.split("=", 1) for line in result.stdout.strip().split("\n") if "=" in line)
        
        self.assertEqual(props.get("MemoryHigh"), "134217728", "MemoryHigh bound is incorrect or missing")
        self.assertEqual(props.get("MemoryMax"), "201326592", "MemoryMax bound is incorrect or missing")
        
        mem_current = props.get("MemoryCurrent")
        self.assertIsNotNone(mem_current)
        self.assertNotEqual(mem_current, "[not set]", "Memory accounting is disabled in cgroup")
        self.assertNotEqual(mem_current, "infinity")
        
        mem_curr_val = int(mem_current)
        self.assertLess(mem_curr_val, 134217728, f"MemoryCurrent ({mem_curr_val} bytes) exceeded MemoryHigh threshold")

if __name__ == '__main__':
    unittest.main(verbosity=2)
