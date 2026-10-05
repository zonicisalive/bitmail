"""
Automated Unit and Integration Test Suite for System Logs & Diagnostic Telemetry.
Tests:
1. SystemLogBuffer in-memory ring buffer, FIFO eviction, filtering, stats, export.
2. SystemLogHandler interceptor capturing Python standard logging events.
3. REST API endpoints (/api/logs, /api/logs/test, /api/logs/export, DELETE /api/logs).
4. HTML page route (/logs).
"""

import asyncio
import logging
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db
from app.logging_service import (
    LogEntry,
    SystemLogBuffer,
    SystemLogHandler,
    resolve_log_source,
    system_log_buffer,
    setup_logging_interceptor
)
from app.main import app


class TestSystemLogBuffer(unittest.TestCase):
    """Unit tests for SystemLogBuffer data structure."""

    def setUp(self):
        self.buffer = SystemLogBuffer(max_capacity=5)

    def test_buffer_add_and_fifo_eviction(self):
        for i in range(8):
            self.buffer.add(LogEntry(
                id=f"log_{i}",
                timestamp="2026-10-05 12:00:00",
                level="INFO",
                logger="bitmail.test",
                source="system",
                message=f"Message {i}"
            ))

        entries = self.buffer.get_entries(limit=10)
        self.assertEqual(len(entries), 5)
        # Newest first
        self.assertEqual(entries[0]["message"], "Message 7")
        self.assertEqual(entries[-1]["message"], "Message 3")

    def test_buffer_filtering_by_level_and_source(self):
        self.buffer.add(LogEntry("1", "2026-10-05 12:00:00", "INFO", "bitmail.queue", "queue", "Queue started"))
        self.buffer.add(LogEntry("2", "2026-10-05 12:00:01", "ERROR", "bitmail.queue", "queue", "Worker crashed"))
        self.buffer.add(LogEntry("3", "2026-10-05 12:00:02", "WARNING", "bitmail.scheduler", "scheduler", "Schedule delayed"))
        self.buffer.add(LogEntry("4", "2026-10-05 12:00:03", "INFO", "bitmail.auth", "auth", "User login"))

        # Filter level=ERROR
        errors = self.buffer.get_entries(level="error")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["message"], "Worker crashed")

        # Filter source=scheduler
        sched = self.buffer.get_entries(source="scheduler")
        self.assertEqual(len(sched), 1)
        self.assertEqual(sched[0]["message"], "Schedule delayed")

        # Search query
        searched = self.buffer.get_entries(search="login")
        self.assertEqual(len(searched), 1)
        self.assertEqual(searched[0]["source"], "auth")

    def test_buffer_stats(self):
        self.buffer.add(LogEntry("1", "2026-10-05 12:00:00", "INFO", "bitmail", "system", "Info 1"))
        self.buffer.add(LogEntry("2", "2026-10-05 12:00:01", "WARNING", "bitmail", "system", "Warn 1"))
        self.buffer.add(LogEntry("3", "2026-10-05 12:00:02", "ERROR", "bitmail", "system", "Err 1"))
        self.buffer.add(LogEntry("4", "2026-10-05 12:00:03", "DEBUG", "bitmail", "system", "Dbg 1"))

        stats = self.buffer.get_stats()
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["warnings"], 1)
        self.assertEqual(stats["info"], 1)
        self.assertEqual(stats["debug"], 1)

    def test_buffer_clear_and_export(self):
        self.buffer.add(LogEntry("1", "2026-10-05 12:00:00", "INFO", "bitmail.system", "system", "First log"))
        exported = self.buffer.export_text()
        self.assertIn("First log", exported)
        self.assertIn("[INFO   ]", exported)

        cleared_count = self.buffer.clear()
        self.assertEqual(cleared_count, 1)
        self.assertEqual(len(self.buffer.get_entries()), 0)


class TestSystemLogHandler(unittest.TestCase):
    """Unit tests for Python logging handler interception."""

    def test_handler_interception(self):
        test_buf = SystemLogBuffer(max_capacity=50)
        handler = SystemLogHandler(test_buf)
        handler.setFormatter(logging.Formatter("%(message)s"))

        test_logger = logging.getLogger("bitmail.queue.test_worker")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)

        test_logger.info("Test dispatch worker event launched.")
        entries = test_buf.get_entries(limit=10)
        self.assertTrue(any("Test dispatch worker event launched." in e["message"] for e in entries))
        test_entry = next(e for e in entries if "Test dispatch worker event launched." in e["message"])
        self.assertEqual(test_entry["source"], "queue")
        self.assertEqual(test_entry["level"], "INFO")

    def test_resolve_log_source(self):
        self.assertEqual(resolve_log_source("bitmail.queue"), "queue")
        self.assertEqual(resolve_log_source("bitmail.scheduler"), "scheduler")
        self.assertEqual(resolve_log_source("bitmail.auth"), "auth")
        self.assertEqual(resolve_log_source("bitmail.smtp"), "smtp")
        self.assertEqual(resolve_log_source("mass_email.storage"), "storage")
        self.assertEqual(resolve_log_source("bitmail.tracking"), "tracking")
        self.assertEqual(resolve_log_source("other.unknown"), "system")


class TestLogsApiAndPage(unittest.TestCase):
    """Integration test suite for Logs API and Page routes."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.test_dir.name) / "data"
        cls.archive_dir = cls.data_dir / "eml_archive"
        cls.db_path = cls.data_dir / "test_logs.db"
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        cls.archive_dir.mkdir(parents=True, exist_ok=True)

        cls.orig_db = settings.DATABASE_PATH
        cls.orig_archive = settings.EML_ARCHIVE_DIR
        cls.orig_storage = settings.EML_STORAGE_DIR

        settings.DATABASE_PATH = cls.db_path
        settings.EML_ARCHIVE_DIR = cls.archive_dir
        settings.EML_STORAGE_DIR = cls.archive_dir

        asyncio.run(init_db())
        setup_logging_interceptor()
        cls.client = TestClient(app)

        # Authenticate test client
        login_res = cls.client.post("/api/auth/login", json={
            "login": "admin@bitmail.com",
            "password": "admin123"
        })
        assert login_res.status_code == 200, f"Login failed: {login_res.text}"
        cls.token = login_res.json()["token"]
        cls.client.headers["Authorization"] = f"Bearer {cls.token}"

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH = cls.orig_db
        settings.EML_ARCHIVE_DIR = cls.orig_archive
        settings.EML_STORAGE_DIR = cls.orig_storage
        cls.test_dir.cleanup()

    def test_emit_test_log_endpoint(self):
        res = self.client.post("/api/logs/test", json={
            "level": "warning",
            "message": "Sample diagnostic warning emitted during test run.",
            "source": "smtp"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["level"], "WARNING")
        self.assertEqual(data["source"], "smtp")

    def test_get_logs_endpoint(self):
        # Emit a known error
        self.client.post("/api/logs/test", json={
            "level": "error",
            "message": "Critical relay connection timeout error",
            "source": "smtp"
        })

        # Query all logs
        res = self.client.get("/api/logs")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("logs", data)
        self.assertIn("stats", data)
        self.assertGreater(data["stats"]["total"], 0)

        # Query level=error
        res_err = self.client.get("/api/logs?level=error")
        self.assertEqual(res_err.status_code, 200)
        err_data = res_err.json()
        self.assertTrue(all(x["level"] == "ERROR" for x in err_data["logs"]))

        # Query search
        res_search = self.client.get("/api/logs?search=Critical+relay")
        self.assertEqual(res_search.status_code, 200)
        self.assertTrue(any("Critical relay" in x["message"] for x in res_search.json()["logs"]))

    def test_export_logs_endpoint(self):
        # Text format
        res_text = self.client.get("/api/logs/export?format=text")
        self.assertEqual(res_text.status_code, 200)
        self.assertEqual(res_text.headers.get("content-type"), "text/plain; charset=utf-8")
        self.assertIn("attachment", res_text.headers.get("content-disposition", ""))

        # JSON format
        res_json = self.client.get("/api/logs/export?format=json")
        self.assertEqual(res_json.status_code, 200)
        self.assertEqual(res_json.headers.get("content-type"), "application/json")

    def test_clear_logs_endpoint(self):
        res = self.client.delete("/api/logs")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])

        # Check buffer is empty (except for the log entry about clearing)
        res_after = self.client.get("/api/logs")
        self.assertLessEqual(res_after.json()["stats"]["total"], 2)

    def test_logs_page_route(self):
        # Set cookie or authorization for page load
        self.client.cookies.set("bitmail_token", self.token)
        res = self.client.get("/logs")
        self.assertEqual(res.status_code, 200)
        self.assertIn("System & Dispatch Logs", res.text)
        self.assertIn("panel-logs", res.text)
        self.assertIn('data-initial-tab="logs"', res.text)


if __name__ == "__main__":
    unittest.main()
