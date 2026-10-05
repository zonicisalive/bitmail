"""
Unit and Integration Test Suite for Campaign Scheduler.
Tests:
1. Datetime normalization (ISO-8601, HTML5 datetime-local, timezone conversions, edge cases)
2. API endpoints:
   - POST /api/campaigns with scheduled_at
   - GET /api/campaigns/scheduled
   - POST /api/campaigns/{id}/schedule
   - POST /api/campaigns/{id}/unschedule
   - POST /api/campaigns/quick-broadcast with scheduled_at
3. CampaignScheduler worker loop and dispatch logic
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db, get_db
from app.main import app
from app.models import CampaignStatus
from app.scheduler import parse_and_normalize_schedule_time, campaign_scheduler, CampaignScheduler


class TestSchedulerParser(unittest.TestCase):
    """Test suite for datetime parser and normalization helper."""

    def test_iso_utc_format(self):
        result = parse_and_normalize_schedule_time("2026-10-15T14:30:00Z")
        self.assertEqual(result, "2026-10-15 14:30:00")

    def test_iso_offset_format(self):
        # 16:30 at UTC+2 is 14:30 UTC
        result = parse_and_normalize_schedule_time("2026-10-15T16:30:00+02:00")
        self.assertEqual(result, "2026-10-15 14:30:00")

    def test_iso_negative_offset_format(self):
        # 10:30 at UTC-4 is 14:30 UTC
        result = parse_and_normalize_schedule_time("2026-10-15T10:30:00-04:00")
        self.assertEqual(result, "2026-10-15 14:30:00")

    def test_datetime_local_format(self):
        result = parse_and_normalize_schedule_time("2026-10-15T14:30")
        self.assertEqual(result, "2026-10-15 14:30:00")

    def test_standard_space_format(self):
        result = parse_and_normalize_schedule_time("2026-10-15 14:30:00")
        self.assertEqual(result, "2026-10-15 14:30:00")

    def test_datetime_object_input(self):
        dt = datetime(2026, 10, 15, 14, 30, 0, tzinfo=timezone.utc)
        result = parse_and_normalize_schedule_time(dt)
        self.assertEqual(result, "2026-10-15 14:30:00")

    def test_invalid_date_strings(self):
        with self.assertRaises(ValueError):
            parse_and_normalize_schedule_time("invalid-date-string")
        with self.assertRaises(ValueError):
            parse_and_normalize_schedule_time("")
        with self.assertRaises(ValueError):
            parse_and_normalize_schedule_time(None)


class TestSchedulerApiAndWorker(unittest.TestCase):
    """Integration test suite for scheduling API routes and background execution."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.test_dir.name) / "data"
        cls.archive_dir = cls.data_dir / "eml_archive"
        cls.db_path = cls.data_dir / "test_scheduler.db"
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        cls.archive_dir.mkdir(parents=True, exist_ok=True)

        cls.orig_db = settings.DATABASE_PATH
        cls.orig_archive = settings.EML_ARCHIVE_DIR
        cls.orig_storage = settings.EML_STORAGE_DIR
        cls.orig_tracking = settings.TRACKING_BASE_URL

        settings.DATABASE_PATH = cls.db_path
        settings.EML_ARCHIVE_DIR = cls.archive_dir
        settings.EML_STORAGE_DIR = cls.archive_dir
        settings.TRACKING_BASE_URL = "http://testserver"

        asyncio.run(init_db())
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
        settings.TRACKING_BASE_URL = cls.orig_tracking
        cls.test_dir.cleanup()

    def test_create_campaign_with_scheduled_at(self):
        future_dt = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        res = self.client.post("/api/campaigns", json={
            "name": "Scheduled Promo Campaign",
            "subject": "Exclusive 2-Day Early Access",
            "body_html": "<p>Coming soon!</p>",
            "sender_name": "Bitmail Team",
            "sender_email": "team@bitmail.io",
            "scheduled_at": future_dt
        })
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["status"], CampaignStatus.SCHEDULED)
        self.assertIsNotNone(data["scheduled_at"])

        # Fetch and verify
        get_res = self.client.get(f"/api/campaigns/{data['id']}")
        self.assertEqual(get_res.status_code, 200)
        get_data = get_res.json()
        self.assertEqual(get_data["status"], "scheduled")

    def test_list_scheduled_campaigns(self):
        res = self.client.get("/api/campaigns/scheduled")
        self.assertEqual(res.status_code, 200)
        scheduled = res.json()
        self.assertIsInstance(scheduled, list)
        self.assertTrue(any(c["name"] == "Scheduled Promo Campaign" for c in scheduled))

    def test_schedule_and_unschedule_campaign(self):
        # Create draft campaign
        create_res = self.client.post("/api/campaigns", json={
            "name": "Draft for Scheduling",
            "subject": "Draft Subject",
            "body_html": "<p>Content</p>",
            "sender_name": "Bitmail",
            "sender_email": "sender@bitmail.io"
        })
        self.assertEqual(create_res.status_code, 201)
        camp_id = create_res.json()["id"]
        self.assertEqual(create_res.json()["status"], "draft")

        # Schedule it
        target_time = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        sched_res = self.client.post(f"/api/campaigns/{camp_id}/schedule", json={
            "scheduled_at": target_time
        })
        self.assertEqual(sched_res.status_code, 200)
        self.assertEqual(sched_res.json()["status"], "scheduled")

        # Check DB
        get_res = self.client.get(f"/api/campaigns/{camp_id}")
        self.assertEqual(get_res.json()["status"], "scheduled")

        # Unschedule it
        unsched_res = self.client.post(f"/api/campaigns/{camp_id}/unschedule")
        self.assertEqual(unsched_res.status_code, 200)
        self.assertEqual(unsched_res.json()["status"], "draft")
        self.assertIsNone(unsched_res.json()["scheduled_at"])

        # Check DB again
        get_res2 = self.client.get(f"/api/campaigns/{camp_id}")
        self.assertEqual(get_res2.json()["status"], "draft")
        self.assertIsNone(get_res2.json()["scheduled_at"])

    def test_schedule_campaign_invalid_payload(self):
        # Create draft campaign
        create_res = self.client.post("/api/campaigns", json={
            "name": "Invalid Schedule Campaign",
            "subject": "Invalid Schedule",
            "body_html": "<p>Content</p>",
            "sender_name": "Bitmail",
            "sender_email": "sender@bitmail.io"
        })
        camp_id = create_res.json()["id"]

        # Missing or invalid date
        res = self.client.post(f"/api/campaigns/{camp_id}/schedule", json={
            "scheduled_at": "not-a-valid-date"
        })
        self.assertEqual(res.status_code, 400)

    def test_quick_broadcast_with_schedule(self):
        # First ensure we have a subscriber
        self.client.post("/api/subscribers", json={
            "email": "customer_scheduled@example.com",
            "name": "Scheduled Customer"
        })

        future_time = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        res = self.client.post("/api/campaigns/quick-broadcast", json={
            "subject": "Scheduled Announcement",
            "body_html": "<h1>Important News</h1>",
            "sender_name": "Bitmail",
            "sender_email": "news@bitmail.io",
            "scheduled_at": future_time,
            "recipients_text": "customer_scheduled@example.com"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "scheduled")
        self.assertIsNotNone(data["scheduled_at"])

        # Verify campaign state in DB: it must NOT be sending/queued immediately
        camp_res = self.client.get(f"/api/campaigns/{data['campaign_id']}")
        self.assertEqual(camp_res.status_code, 200)
        self.assertEqual(camp_res.json()["status"], "scheduled")

    def test_scheduler_service_triggers_due_campaigns(self):
        # Create a campaign scheduled in the past (due right now)
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        create_res = self.client.post("/api/campaigns", json={
            "name": "Due Campaign",
            "subject": "Due Subject",
            "body_html": "<p>Due body</p>",
            "sender_name": "Bitmail",
            "sender_email": "sender@bitmail.io",
            "scheduled_at": past_time
        })
        self.assertEqual(create_res.status_code, 201)
        camp_id = create_res.json()["id"]

        # Also create a campaign scheduled in the future (not due)
        future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        future_res = self.client.post("/api/campaigns", json={
            "name": "Future Campaign",
            "subject": "Future Subject",
            "body_html": "<p>Future body</p>",
            "sender_name": "Bitmail",
            "sender_email": "sender@bitmail.io",
            "scheduled_at": future_time
        })
        self.assertEqual(future_res.status_code, 201)
        future_camp_id = future_res.json()["id"]

        # Run scheduler check with launch_campaign mocked to verify trigger
        scheduler = CampaignScheduler(poll_interval_seconds=1.0)
        with patch("app.scheduler.campaign_queue.launch_campaign", new_callable=AsyncMock) as mock_launch:
            mock_launch.return_value = {"success": True, "message": "Campaign launched", "status": "sending"}
            triggered = asyncio.run(scheduler.check_and_trigger_due_campaigns())
            self.assertIn(camp_id, triggered)
            self.assertNotIn(future_camp_id, triggered)
            mock_launch.assert_awaited_once_with(camp_id)

        # Verify due campaign status transitioned to queued
        res_due = self.client.get(f"/api/campaigns/{camp_id}")
        self.assertEqual(res_due.json()["status"], "queued")

        # Verify future campaign status remains scheduled
        res_future = self.client.get(f"/api/campaigns/{future_camp_id}")
        self.assertEqual(res_future.json()["status"], "scheduled")

    def test_scheduler_lifecycle_start_and_stop(self):
        scheduler = CampaignScheduler(poll_interval_seconds=0.1)
        self.assertFalse(scheduler.is_running)

        async def run_lifecycle():
            await scheduler.start()
            self.assertTrue(scheduler.is_running)
            self.assertIsNotNone(scheduler._task)
            # Starting again should be a no-op
            await scheduler.start()
            self.assertTrue(scheduler.is_running)
            # Stop
            await scheduler.stop()
            self.assertFalse(scheduler.is_running)

        asyncio.run(run_lifecycle())


if __name__ == "__main__":
    unittest.main()
