"""
Automated Unit and Integration Test Suite for Email Warmup Engine & Relay Rotation.
Tests:
1. Warmup Schedule Curves (conservative_30, standard_14, aggressive_7, custom)
2. ISP Provider Identification & Balanced Slicing (Gmail, Microsoft, Yahoo, Apple, Corporate)
3. Relay Pool Manager (round-robin rotation, failover, 421/451 cooldown tracking, toggle pool)
4. Safety Circuit Breaker (auto-pause on >2% bounce rate, >5% failure rate, low volume threshold)
5. Warmup REST API endpoints & Frontend UI page route
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
import uuid

from fastapi.testclient import TestClient

from app.config import settings
from app.db import get_db, init_db, utc_now_iso
from app.main import app
from app.warmup import (
    RelayPoolManager,
    WarmupCircuitBreaker,
    WarmupScheduleCurves,
    WarmupSlicer,
)


class TestWarmupCurves(unittest.TestCase):
    """Unit tests for warmup ramp-up curves."""

    def test_conservative_30_curve(self):
        total = 50000
        caps = WarmupScheduleCurves.get_curve_caps("conservative_30", total)
        self.assertGreater(len(caps), 0)
        self.assertEqual(sum(caps), total)
        self.assertEqual(caps[0], 50)
        # Check monotonic or increasing capacity trend
        self.assertLessEqual(caps[0], caps[1])

    def test_standard_14_curve(self):
        total = 20000
        caps = WarmupScheduleCurves.get_curve_caps("standard_14", total)
        self.assertGreater(len(caps), 0)
        self.assertEqual(sum(caps), total)
        self.assertEqual(caps[0], 100)
        self.assertLessEqual(len(caps), 14)

    def test_aggressive_7_curve(self):
        total = 10000
        caps = WarmupScheduleCurves.get_curve_caps("aggressive_7", total)
        self.assertGreater(len(caps), 0)
        self.assertEqual(sum(caps), total)
        self.assertEqual(caps[0], 250)
        self.assertLessEqual(len(caps), 7)

    def test_custom_curve(self):
        total = 5000
        caps = WarmupScheduleCurves.get_curve_caps(
            "custom",
            total_recipients=total,
            custom_days=10,
            custom_start_cap=50
        )
        self.assertEqual(len(caps), 10)
        self.assertEqual(sum(caps), total)
        self.assertEqual(caps[0], 50)

    def test_small_total_recipients(self):
        # When audience is smaller than day 1 cap
        total = 25
        caps = WarmupScheduleCurves.get_curve_caps("standard_14", total)
        self.assertEqual(caps, [25])

    def test_large_recipient_overflow(self):
        # Ensure remaining recipients are appended to the final slice
        total = 1000000
        caps = WarmupScheduleCurves.get_curve_caps("aggressive_7", total)
        self.assertEqual(sum(caps), total)
        self.assertEqual(len(caps), 7)


class TestWarmupSlicer(unittest.TestCase):
    """Unit tests for provider identification and balanced slicing."""

    def test_identify_provider(self):
        self.assertEqual(WarmupSlicer.identify_provider("user@gmail.com"), "gmail")
        self.assertEqual(WarmupSlicer.identify_provider("user@googlemail.com"), "gmail")
        self.assertEqual(WarmupSlicer.identify_provider("ceo@outlook.com"), "microsoft")
        self.assertEqual(WarmupSlicer.identify_provider("manager@hotmail.com"), "microsoft")
        self.assertEqual(WarmupSlicer.identify_provider("lead@office365.com"), "microsoft")
        self.assertEqual(WarmupSlicer.identify_provider("press@yahoo.com"), "yahoo")
        self.assertEqual(WarmupSlicer.identify_provider("staff@aol.com"), "yahoo")
        self.assertEqual(WarmupSlicer.identify_provider("designer@icloud.com"), "apple")
        self.assertEqual(WarmupSlicer.identify_provider("developer@mac.com"), "apple")
        self.assertEqual(WarmupSlicer.identify_provider("client@bitnade.com"), "corporate")
        self.assertEqual(WarmupSlicer.identify_provider("invalid-email"), "corporate")

    def test_balance_and_slice(self):
        # Mix of 20 Gmail, 10 Microsoft, 5 Yahoo, 5 Corporate (40 total)
        recipients = (
            [{"email": f"g_{i}@gmail.com"} for i in range(20)] +
            [{"email": f"m_{i}@outlook.com"} for i in range(10)] +
            [{"email": f"y_{i}@yahoo.com"} for i in range(5)] +
            [{"email": f"c_{i}@corporate.com"} for i in range(5)]
        )
        daily_caps = [10, 15, 15]
        start_time = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)

        slices = WarmupSlicer.balance_and_slice(
            recipients=recipients,
            daily_caps=daily_caps,
            start_datetime=start_time
        )

        self.assertEqual(len(slices), 3)
        self.assertEqual(slices[0]["target_count"], 10)
        self.assertEqual(slices[1]["target_count"], 15)
        self.assertEqual(slices[2]["target_count"], 15)

        # Total recipients across slices
        total_sliced = sum(s["target_count"] for s in slices)
        self.assertEqual(total_sliced, 40)

        # Provider distribution in slice 1 should contain multiple providers (balanced)
        pdist_1 = slices[0]["provider_distribution"]
        self.assertIn("gmail", pdist_1)
        self.assertIn("microsoft", pdist_1)
        self.assertIn("yahoo", pdist_1)
        self.assertIn("corporate", pdist_1)

        # Verify dates advance day by day
        self.assertEqual(slices[0]["scheduled_for"], "2026-01-01 09:00:00")
        self.assertEqual(slices[1]["scheduled_for"], "2026-01-02 09:00:00")
        self.assertEqual(slices[2]["scheduled_for"], "2026-01-03 09:00:00")


class TestRelayPoolAndCircuitBreaker(unittest.IsolatedAsyncioTestCase):
    """Integration tests for RelayPoolManager and WarmupCircuitBreaker with test DB."""

    async def asyncSetUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.test_dir.name) / "test_warmup.db"
        self.orig_db = settings.DATABASE_PATH
        settings.DATABASE_PATH = self.db_path
        await init_db()

    async def asyncTearDown(self):
        settings.DATABASE_PATH = self.orig_db
        self.test_dir.cleanup()

    async def test_relay_pool_rotation_and_cooldown(self):
        now = utc_now_iso()
        # Seed 2 relays into DB
        async with get_db() as db:
            await db.execute("""
                INSERT INTO smtp_configs (
                    id, name, host, port, username, password, use_tls, use_ssl,
                    in_relay_pool, warmup_day, is_active, is_default, created_at, updated_at
                ) VALUES 
                ('relay_1', 'Relay Primary', 'smtp.primary.com', 587, 'user1', 'pass1', 1, 0, 1, 1, 1, 1, ?, ?),
                ('relay_2', 'Relay Secondary', 'smtp.secondary.com', 587, 'user2', 'pass2', 1, 0, 1, 1, 1, 0, ?, ?)
            """, (now, now, now, now))
            await db.commit()

        # 1. Fetch pool relays
        relays = await RelayPoolManager.get_pool_relays()
        self.assertEqual(len(relays), 2)
        self.assertTrue(all(r["in_pool"] for r in relays))

        # 2. Select relay (round-robin)
        selected_1 = await RelayPoolManager.select_relay("round_robin")
        selected_2 = await RelayPoolManager.select_relay("round_robin")
        self.assertIsNotNone(selected_1)
        self.assertIsNotNone(selected_2)
        self.assertNotEqual(selected_1["id"], selected_2["id"])

        # 3. Mark success
        await RelayPoolManager.mark_success("relay_1")
        relays_after_success = await RelayPoolManager.get_pool_relays()
        r1 = next(r for r in relays_after_success if r["id"] == "relay_1")
        self.assertEqual(r1["daily_sends"], 1)

        # 4. Mark temporary failure with 421 code (triggers cooldown)
        await RelayPoolManager.mark_failure(
            "relay_1",
            "421 4.7.0 Try again later, closing connection",
            cooldown_seconds=600
        )
        relays_after_fail = await RelayPoolManager.get_pool_relays()
        r1_failed = next(r for r in relays_after_fail if r["id"] == "relay_1")
        self.assertTrue(r1_failed["is_cooling_down"])
        self.assertEqual(r1_failed["daily_failures"], 1)

        # 5. Next selection must skip cooling-down relay_1 and return relay_2
        selected_relay = await RelayPoolManager.select_relay("round_robin")
        self.assertEqual(selected_relay["id"], "relay_2")

        # 6. Toggle relay pool inclusion
        await RelayPoolManager.toggle_relay_pool("relay_2", False)
        relays_after_toggle = await RelayPoolManager.get_pool_relays()
        r2 = next(r for r in relays_after_toggle if r["id"] == "relay_2")
        self.assertFalse(r2["in_pool"])

    async def test_warmup_circuit_breaker(self):
        now = utc_now_iso()
        sched_id = "sched_test_cb"
        slice_safe_id = "slice_safe"
        slice_bounce_id = "slice_bounce"
        slice_fail_id = "slice_fail"
        slice_low_vol_id = "slice_low_vol"

        async with get_db() as db:
            # Seed schedule
            await db.execute("""
                INSERT INTO warmup_schedules (
                    id, name, strategy, total_recipients, current_day, total_days,
                    daily_cap, status, rotation_mode, max_bounce_rate, created_at, updated_at
                ) VALUES (?, 'Test Schedule', 'standard_14', 1000, 1, 14, 100, 'active', 'round_robin', 0.02, ?, ?)
            """, (sched_id, now, now))

            # Seed safe slice: 100 dispatched, 1 bounce (1.0% < 2%), 1 failure (1.0% < 5%)
            await db.execute("""
                INSERT INTO warmup_slices (
                    id, schedule_id, day_number, scheduled_for, target_count,
                    dispatched_count, bounce_count, failure_count, status, created_at, updated_at
                ) VALUES (?, ?, 1, ?, 100, 100, 1, 1, 'completed', ?, ?)
            """, (slice_safe_id, sched_id, now, now, now))

            # Seed bounce-tripped slice: 100 dispatched, 5 bounces (5.0% > 2%)
            await db.execute("""
                INSERT INTO warmup_slices (
                    id, schedule_id, day_number, scheduled_for, target_count,
                    dispatched_count, bounce_count, failure_count, status, created_at, updated_at
                ) VALUES (?, ?, 2, ?, 100, 100, 5, 0, 'completed', ?, ?)
            """, (slice_bounce_id, sched_id, now, now, now))

            # Seed failure-tripped slice: 100 dispatched, 8 failures (8.0% > 5%)
            await db.execute("""
                INSERT INTO warmup_slices (
                    id, schedule_id, day_number, scheduled_for, target_count,
                    dispatched_count, bounce_count, failure_count, status, created_at, updated_at
                ) VALUES (?, ?, 3, ?, 100, 100, 0, 8, 'completed', ?, ?)
            """, (slice_fail_id, sched_id, now, now, now))

            # Seed low-volume slice: 5 dispatched, 1 bounce (20%, but < 10 threshold)
            await db.execute("""
                INSERT INTO warmup_slices (
                    id, schedule_id, day_number, scheduled_for, target_count,
                    dispatched_count, bounce_count, failure_count, status, created_at, updated_at
                ) VALUES (?, ?, 4, ?, 10, 5, 1, 0, 'completed', ?, ?)
            """, (slice_low_vol_id, sched_id, now, now, now))
            await db.commit()

        # 1. Test low volume slice (<10 dispatched) -> safe
        ok_low, msg_low = await WarmupCircuitBreaker.check_and_apply(sched_id, slice_low_vol_id)
        self.assertTrue(ok_low)
        self.assertIn("Low volume", msg_low)

        # 2. Test healthy slice -> safe
        ok_safe, msg_safe = await WarmupCircuitBreaker.check_and_apply(sched_id, slice_safe_id)
        self.assertTrue(ok_safe)
        self.assertIn("healthy", msg_safe)

        # Verify schedule is still active
        async with get_db() as db:
            async with db.execute("SELECT status FROM warmup_schedules WHERE id = ?", (sched_id,)) as cur:
                row = await cur.fetchone()
                self.assertEqual(row["status"], "active")

        # 3. Test excessive bounces -> trips circuit breaker and pauses schedule
        ok_bounce, msg_bounce = await WarmupCircuitBreaker.check_and_apply(sched_id, slice_bounce_id)
        self.assertFalse(ok_bounce)
        self.assertIn("Bounce rate", msg_bounce)

        async with get_db() as db:
            async with db.execute("SELECT status FROM warmup_schedules WHERE id = ?", (sched_id,)) as cur:
                row = await cur.fetchone()
                self.assertEqual(row["status"], "paused")

        # Reset schedule to active to test failure breaker
        async with get_db() as db:
            await db.execute("UPDATE warmup_schedules SET status = 'active' WHERE id = ?", (sched_id,))
            await db.commit()

        # 4. Test excessive relay failures -> trips circuit breaker
        ok_fail, msg_fail = await WarmupCircuitBreaker.check_and_apply(sched_id, slice_fail_id)
        self.assertFalse(ok_fail)
        self.assertIn("failure rate", msg_fail)

        async with get_db() as db:
            async with db.execute("SELECT status FROM warmup_schedules WHERE id = ?", (sched_id,)) as cur:
                row = await cur.fetchone()
                self.assertEqual(row["status"], "paused")


class TestWarmupAPIAndPages(unittest.TestCase):
    """Integration tests for FastAPI Warmup API endpoints and UI page."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.test_dir.name) / "test_warmup_api.db"
        cls.orig_db = settings.DATABASE_PATH
        settings.DATABASE_PATH = cls.db_path

        asyncio.run(init_db())
        cls.client = TestClient(app)

        # Login seeded admin
        login_res = cls.client.post("/api/auth/login", json={
            "login": "admin@bitmail.com",
            "password": "admin123"
        })
        assert login_res.status_code == 200, f"Login failed: {login_res.text}"
        cls.token = login_res.json()["token"]
        cls.auth_headers = {"Authorization": f"Bearer {cls.token}"}

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH = cls.orig_db
        cls.test_dir.cleanup()

    def test_preview_warmup_curve_endpoint(self):
        res = self.client.post(
            "/api/warmup/preview",
            headers=self.auth_headers,
            json={
                "strategy": "standard_14",
                "total_recipients": 5000
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["strategy"], "standard_14")
        self.assertEqual(data["total_recipients"], 5000)
        self.assertGreater(data["total_days"], 0)
        self.assertEqual(len(data["slices"]), data["total_days"])
        self.assertEqual(data["slices"][-1]["cumulative_volume"], 5000)

    def test_schedules_crud_lifecycle(self):
        # 1. Create a schedule with direct recipient emails
        emails = [f"lead_{i}@gmail.com" for i in range(15)] + [f"corp_{i}@company.com" for i in range(10)]
        create_payload = {
            "name": "Q1 Cold Outreach Warmup",
            "strategy": "standard_14",
            "recipient_emails": emails,
            "rotation_mode": "round_robin"
        }
        res_create = self.client.post(
            "/api/warmup/schedules",
            headers=self.auth_headers,
            json=create_payload
        )
        self.assertEqual(res_create.status_code, 201)
        created_sched = res_create.json()
        sched_id = created_sched["id"]
        self.assertEqual(created_sched["name"], "Q1 Cold Outreach Warmup")
        self.assertEqual(created_sched["total_recipients"], 25)
        self.assertEqual(created_sched["status"], "active")
        self.assertGreater(len(created_sched["slices"]), 0)

        # 2. List schedules
        res_list = self.client.get("/api/warmup/schedules", headers=self.auth_headers)
        self.assertEqual(res_list.status_code, 200)
        schedules = res_list.json()
        self.assertTrue(any(s["id"] == sched_id for s in schedules))

        # 3. Get single schedule details
        res_get = self.client.get(f"/api/warmup/schedules/{sched_id}", headers=self.auth_headers)
        self.assertEqual(res_get.status_code, 200)
        self.assertEqual(res_get.json()["id"], sched_id)

        # 4. Pause schedule
        res_pause = self.client.post(f"/api/warmup/schedules/{sched_id}/pause", headers=self.auth_headers)
        self.assertEqual(res_pause.status_code, 200)
        self.assertEqual(res_pause.json()["status"], "success")

        res_check_pause = self.client.get(f"/api/warmup/schedules/{sched_id}", headers=self.auth_headers)
        self.assertEqual(res_check_pause.json()["status"], "paused")

        # 5. Resume schedule
        res_resume = self.client.post(f"/api/warmup/schedules/{sched_id}/resume", headers=self.auth_headers)
        self.assertEqual(res_resume.status_code, 200)
        self.assertEqual(res_resume.json()["status"], "success")

        res_check_resume = self.client.get(f"/api/warmup/schedules/{sched_id}", headers=self.auth_headers)
        self.assertEqual(res_check_resume.json()["status"], "active")

        # 6. Delete schedule
        res_delete = self.client.delete(f"/api/warmup/schedules/{sched_id}", headers=self.auth_headers)
        self.assertEqual(res_delete.status_code, 200)

        res_check_del = self.client.get(f"/api/warmup/schedules/{sched_id}", headers=self.auth_headers)
        self.assertEqual(res_check_del.status_code, 404)

    def test_relays_endpoint_and_toggle(self):
        # 1. Fetch relay pool
        res = self.client.get("/api/warmup/relays", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        relays = res.json()
        self.assertIsInstance(relays, list)

        if relays:
            relay_id = relays[0]["smtp_config_id"]
            orig_in_pool = relays[0]["in_pool"]
            new_in_pool = not orig_in_pool

            # 2. Toggle relay pool status
            res_toggle = self.client.post(
                f"/api/warmup/relays/{relay_id}/toggle-pool",
                headers=self.auth_headers,
                json={"in_pool": new_in_pool}
            )
            self.assertEqual(res_toggle.status_code, 200)
            self.assertEqual(res_toggle.json()["in_pool"], new_in_pool)

            # Revert toggle
            self.client.post(
                f"/api/warmup/relays/{relay_id}/toggle-pool",
                headers=self.auth_headers,
                json={"in_pool": orig_in_pool}
            )

    def test_warmup_page_route(self):
        # Page route is served without auth header or with auth
        res = self.client.get("/warmup")
        self.assertEqual(res.status_code, 200)
        self.assertIn("panel-warmup", res.text)
        self.assertTrue(
            "Warmup & Relay Rotation" in res.text or
            "Warmup &amp; Relay Rotation" in res.text
        )


if __name__ == "__main__":
    unittest.main()
