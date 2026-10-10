"""
Comprehensive Unit and Integration Test Suite for Advanced Deliverability & Automation Suite:
1. Live IP & Domain Blacklist Monitor (DNSBL / RBL Checker)
2. RFC 8058 One-Click List-Unsubscribe Header & Zero-Friction POST Handler
3. Catch-All Domain Detection Engine
4. Smart Bounce & FBL Classifier with Self-Healing Lists
5. Outbound Webhooks Engine (HMAC-SHA256 Signed Dispatches & Management CRUD)
"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from fastapi.testclient import TestClient
import httpx

from app.blacklist import BLACKLIST_ZONES, BlacklistMonitorService, blacklist_service
from app.bounce import BounceClassifier
from app.config import settings
from app.db import get_db, init_db, utc_now_iso
from app.deliverability import PreSendSafetyGuard
from app.main import app
from app.models import BounceType
from app.sender import build_mime_message
from app.webhooks import WebhookDispatcher


class TestBlacklistMonitor(unittest.TestCase):
    """Unit and service tests for DNSBL / RBL Blacklist Monitor."""

    @classmethod
    def setUpClass(cls):
        cls.service = BlacklistMonitorService(query_timeout=2.0)

    def test_reverse_ip(self):
        self.assertEqual(self.service.reverse_ip("192.0.2.1"), "1.2.0.192")
        self.assertEqual(self.service.reverse_ip("127.0.0.2"), "2.0.0.127")
        self.assertEqual(self.service.reverse_ip("example.com"), "example.com")

    def test_is_ip(self):
        self.assertTrue(self.service.is_ip("127.0.0.1"))
        self.assertTrue(self.service.is_ip("192.168.1.100"))
        self.assertTrue(self.service.is_ip("::1"))
        self.assertFalse(self.service.is_ip("mail.bitnade.com"))
        self.assertFalse(self.service.is_ip("invalid-ip"))

    def test_zones_catalog_count(self):
        # Must track 30+ major zones
        self.assertGreaterEqual(len(BLACKLIST_ZONES), 30)
        ip_zones = [z for z in BLACKLIST_ZONES if z["type"] == "ip"]
        domain_zones = [z for z in BLACKLIST_ZONES if z["type"] == "domain"]
        self.assertGreaterEqual(len(ip_zones), 25)
        self.assertGreaterEqual(len(domain_zones), 5)

    def test_check_clean_ip(self):
        # 1.1.1.1 is Cloudflare DNS, clean across blacklists
        res = asyncio.run(self.service.check_target("1.1.1.1"))
        self.assertEqual(res.target, "1.1.1.1")
        self.assertEqual(res.target_type, "ip")
        self.assertGreaterEqual(res.total_zones_checked, 25)
        self.assertFalse(res.is_blacklisted)
        self.assertEqual(res.listed_count, 0)
        self.assertEqual(res.status, "clean")


class TestRfc8058Unsubscribe(unittest.TestCase):
    """Tests for RFC 8058 List-Unsubscribe dual headers and zero-friction POST handler."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.test_dir.name) / "test_unsub.db"
        cls.orig_db = settings.DATABASE_PATH
        settings.DATABASE_PATH = cls.db_path
        asyncio.run(init_db())
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH = cls.orig_db
        cls.test_dir.cleanup()

    def test_build_mime_message_rfc8058_headers(self):
        unsub_url = "https://mail.bitnade.com/api/tracking/unsubscribe/token_123"
        msg, raw_eml = build_mime_message(
            sender_name="Bitmail Admin",
            sender_email="support@bitnade.com",
            recipient_name="Alice Smith",
            recipient_email="alice@example.com",
            subject="RFC 8058 Test",
            body_html="<p>Test Content</p>",
            body_text="Test Content",
            unsubscribe_url=unsub_url
        )

        list_unsub = msg.get("List-Unsubscribe", "")
        list_unsub_post = msg.get("List-Unsubscribe-Post", "")

        # Verify dual HTTPS and mailto URIs
        self.assertIn(f"<{unsub_url}>", list_unsub)
        self.assertIn("mailto:unsubscribe@bitnade.com?subject=unsubscribe", list_unsub)
        # Verify List-Unsubscribe-Post: List-Unsubscribe=One-Click
        self.assertEqual(list_unsub_post, "List-Unsubscribe=One-Click")

    def test_one_click_post_endpoint(self):
        # Seed subscriber
        sub_email = "oneclick_test@example.com"
        now = utc_now_iso()

        async def _seed():
            async with get_db() as db:
                await db.execute("""
                    INSERT INTO subscribers (id, email, first_name, last_name, status, created_at, updated_at)
                    VALUES ('sub_test_001', ?, 'OneClick', 'Tester', 'active', ?, ?)
                """, (sub_email, now, now))
                await db.commit()

        asyncio.run(_seed())

        # MUA sends HTTP POST
        res = self.client.post(
            f"/unsubscribe/sub_test_001",
            content=b"List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["unsubscribed"])
        self.assertEqual(data["email"], sub_email)

        # Verify subscriber updated and added to both suppressions tables
        async def _verify():
            async with get_db() as db:
                async with db.execute("SELECT status FROM subscribers WHERE email = ?", (sub_email,)) as cur:
                    sub = await cur.fetchone()
                    self.assertEqual(sub["status"], "unsubscribed")

                async with db.execute("SELECT email FROM suppressions WHERE email = ?", (sub_email,)) as cur:
                    sup1 = await cur.fetchone()
                    self.assertIsNotNone(sup1)

                async with db.execute("SELECT email FROM suppressions WHERE email = ?", (sub_email,)) as cur:
                    sup2 = await cur.fetchone()
                    self.assertIsNotNone(sup2)

        asyncio.run(_verify())

    def test_one_click_get_renders_html(self):
        res = self.client.get("/unsubscribe/direct_user@example.com")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers.get("content-type", ""))
        self.assertIn("Unsubscribed Successfully", res.text)
        self.assertIn("Subscribe Again", res.text)

    def test_resubscribe_endpoint_flow(self):
        # 1. Unsubscribe first
        res_unsub = self.client.get("/unsubscribe/resub_test@example.com")
        self.assertEqual(res_unsub.status_code, 200)

        # 2. Resubscribe via JSON
        res_json = self.client.post("/resubscribe/resub_test@example.com", headers={"Accept": "application/json"})
        self.assertEqual(res_json.status_code, 200)
        self.assertTrue(res_json.json()["resubscribed"])

        # 3. Resubscribe via HTML
        res_html = self.client.get("/resubscribe/resub_test@example.com")
        self.assertEqual(res_html.status_code, 200)
        self.assertIn("Welcome Back!", res_html.text)


class TestCatchAllDetection(unittest.TestCase):
    """Tests for Accept-All / Catch-All domain detector."""

    def test_detect_catch_all_accept_all_domain(self):
        mock_probe = {
            "tested": True,
            "passed": True,
            "status": "accepted",
            "code": 250,
            "details": "Mailbox accepted",
            "mx_host": "mail.acceptall.com"
        }

        with patch.object(PreSendSafetyGuard, "probe_smtp_mailbox", new=AsyncMock(return_value=mock_probe)):
            res = asyncio.run(PreSendSafetyGuard.detect_catch_all("acceptall.com"))
            self.assertTrue(res["tested"])
            self.assertTrue(res["is_catch_all"])
            self.assertEqual(res["status"], "catch_all_detected")
            self.assertIn("blindly accepts", res["details"])

    def test_detect_catch_all_strict_domain(self):
        mock_probe = {
            "tested": True,
            "passed": False,
            "status": "rejected",
            "code": 550,
            "details": "User unknown",
            "mx_host": "mail.strict.com"
        }

        with patch.object(PreSendSafetyGuard, "probe_smtp_mailbox", new=AsyncMock(return_value=mock_probe)):
            res = asyncio.run(PreSendSafetyGuard.detect_catch_all("strict.com"))
            self.assertTrue(res["tested"])
            self.assertFalse(res["is_catch_all"])
            self.assertEqual(res["status"], "strict_validation")
            self.assertIn("rejects invalid addresses", res["details"])


class TestSmartBounceClassifier(unittest.TestCase):
    """Tests for RFC 3464 / 5965 bounce classification and self-healing suppression."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.test_dir.name) / "test_bounce.db"
        cls.orig_db = settings.DATABASE_PATH
        settings.DATABASE_PATH = cls.db_path
        asyncio.run(init_db())

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH = cls.orig_db
        cls.test_dir.cleanup()

    def test_classify_hard_bounce_codes(self):
        b_type, reason = BounceClassifier.classify(status_code="5.1.1", diagnostic="User unknown")
        self.assertEqual(b_type, BounceType.HARD)

        b_type, reason = BounceClassifier.classify(status_code="5.7.1", diagnostic="Access denied / permanently rejected")
        self.assertEqual(b_type, BounceType.HARD)

    def test_classify_soft_bounce_codes(self):
        b_type, reason = BounceClassifier.classify(status_code="4.2.2", diagnostic="Mailbox full")
        self.assertEqual(b_type, BounceType.SOFT)

        b_type, reason = BounceClassifier.classify(status_code="4.4.1", diagnostic="Connection timed out")
        self.assertEqual(b_type, BounceType.SOFT)

    def test_classify_complaint(self):
        b_type, reason = BounceClassifier.classify(raw_text="Feedback-Type: abuse\nOriginal-Rcpt-To: spammer@victim.com")
        self.assertEqual(b_type, BounceType.COMPLAINT)

    def test_process_hard_bounce_suppression(self):
        email_addr = "dead_recipient@example.com"
        now = utc_now_iso()

        async def _run():
            # Seed subscriber
            async with get_db() as db:
                await db.execute("""
                    INSERT INTO subscribers (id, email, first_name, last_name, status, created_at, updated_at)
                    VALUES ('sub_dead_01', ?, 'Dead', 'User', 'active', ?, ?)
                """, (email_addr, now, now))
                await db.commit()

            # Process 5.1.1 hard bounce
            res = await BounceClassifier.process_bounce(
                recipient_email=email_addr,
                bounce_type=BounceType.HARD,
                reason="5.1.1 Mailbox does not exist",
                status_code="5.1.1"
            )

            self.assertTrue(res.success)
            self.assertTrue(res.suppressed)
            self.assertEqual(res.bounce_type, BounceType.HARD)

            # Verify subscriber was marked bounced and added to suppressions
            async with get_db() as db:
                async with db.execute("SELECT status FROM subscribers WHERE email = ?", (email_addr,)) as cur:
                    sub = await cur.fetchone()
                    self.assertEqual(sub["status"], "bounced")

                async with db.execute("SELECT email, reason FROM suppressions WHERE email = ?", (email_addr,)) as cur:
                    sup = await cur.fetchone()
                    self.assertIsNotNone(sup)
                    self.assertIn("hard_bounce", sup["reason"])

        asyncio.run(_run())

    def test_process_soft_bounce_no_suppression(self):
        email_addr = "full_mailbox@example.com"

        async def _run():
            res = await BounceClassifier.process_bounce(
                recipient_email=email_addr,
                bounce_type=BounceType.SOFT,
                reason="4.2.2 Mailbox quota exceeded",
                status_code="4.2.2"
            )

            self.assertTrue(res.success)
            self.assertFalse(res.suppressed)

            async with get_db() as db:
                async with db.execute("SELECT email FROM suppressions WHERE email = ?", (email_addr,)) as cur:
                    sup = await cur.fetchone()
                    self.assertIsNone(sup)

        asyncio.run(_run())


class TestOutboundWebhooks(unittest.TestCase):
    """Tests for HMAC-SHA256 signed webhooks dispatcher and CRUD endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.test_dir.name) / "test_webhooks.db"
        cls.orig_db = settings.DATABASE_PATH
        settings.DATABASE_PATH = cls.db_path
        asyncio.run(init_db())

        cls.client = TestClient(app)
        # Login admin
        login_res = cls.client.post("/api/auth/login", json={
            "login": "admin@bitmail.com",
            "password": "admin123"
        })
        assert login_res.status_code == 200
        cls.token = login_res.json()["token"]
        cls.auth_headers = {"Authorization": f"Bearer {cls.token}"}

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH = cls.orig_db
        cls.test_dir.cleanup()

    def test_hmac_signature_calculation(self):
        secret = "super_secret_webhook_key_123"
        payload = b'{"event":"email.sent","delivery_id":"whd_123"}'
        sig = WebhookDispatcher.compute_signature(secret, payload)
        self.assertEqual(len(sig), 64)  # 64 hex characters for SHA-256

        # Verifying deterministic calculation
        sig2 = WebhookDispatcher.compute_signature(secret, payload)
        self.assertEqual(sig, sig2)

    def test_webhooks_crud_api(self):
        # 1. Create Webhook
        create_res = self.client.post(
            "/api/webhooks",
            headers=self.auth_headers,
            json={
                "name": "HubSpot CRM Integration",
                "url": "https://httpbin.org/post",
                "events": ["email.sent", "email.bounced", "subscriber.unsubscribed"],
                "is_active": True
            }
        )
        self.assertEqual(create_res.status_code, 201)
        wh = create_res.json()
        self.assertEqual(wh["name"], "HubSpot CRM Integration")
        self.assertTrue(wh["secret"].startswith("whsec_"))
        wh_id = wh["id"]

        # 2. List Webhooks
        list_res = self.client.get("/api/webhooks", headers=self.auth_headers)
        self.assertEqual(list_res.status_code, 200)
        items = list_res.json()
        self.assertGreaterEqual(len(items), 1)
        self.assertIn(wh_id, [i["id"] for i in items])

        # 3. Get Single Webhook & Deliveries
        get_res = self.client.get(f"/api/webhooks/{wh_id}", headers=self.auth_headers)
        self.assertEqual(get_res.status_code, 200)
        detail = get_res.json()
        self.assertEqual(detail["webhook"]["name"], "HubSpot CRM Integration")
        self.assertIn("recent_deliveries", detail)

        # 4. Update Webhook
        update_res = self.client.put(
            f"/api/webhooks/{wh_id}",
            headers=self.auth_headers,
            json={"name": "HubSpot Production CRM"}
        )
        self.assertEqual(update_res.status_code, 200)
        self.assertEqual(update_res.json()["name"], "HubSpot Production CRM")

        # 5. Delete Webhook
        del_res = self.client.delete(f"/api/webhooks/{wh_id}", headers=self.auth_headers)
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.json()["deleted"])

    def test_webhook_event_dispatch_and_logging(self):
        wh_id = "whk_test_dispatch"
        secret = "secret123"
        now = utc_now_iso()

        async def _run():
            async with get_db() as db:
                await db.execute("""
                    INSERT INTO webhooks (id, name, url, secret, events_json, is_active, created_at, updated_at)
                    VALUES (?, 'Mock Endpoint', 'https://mock.endpoint.local/webhook', ?, '["email.sent", "email.bounced"]', 1, ?, ?)
                """, (wh_id, secret, now, now))
                await db.commit()

            # Mock httpx AsyncClient post
            mock_response = httpx.Response(status_code=200, text='{"received": true}')
            with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
                deliveries = await WebhookDispatcher.dispatch_event(
                    "email.sent",
                    {"recipient": "user@example.com", "subject": "Welcome"},
                    background=False
                )

                self.assertEqual(len(deliveries), 1)
                self.assertTrue(deliveries[0]["success"])
                self.assertEqual(deliveries[0]["status_code"], 200)

            # Verify delivery logged in database
            async with get_db() as db:
                async with db.execute("SELECT * FROM webhook_deliveries WHERE webhook_id = ?", (wh_id,)) as cur:
                    d_row = await cur.fetchone()
                    self.assertIsNotNone(d_row)
                    self.assertEqual(d_row["event_type"], "email.sent")
                    self.assertEqual(d_row["success"], 1)

        asyncio.run(_run())


class TestDeliverabilityIntegrationRoutes(unittest.TestCase):
    """Integration tests for Deliverability and Bounce routes."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.test_dir.name) / "test_routes.db"
        cls.orig_db = settings.DATABASE_PATH
        settings.DATABASE_PATH = cls.db_path
        asyncio.run(init_db())

        cls.client = TestClient(app)
        login_res = cls.client.post("/api/auth/login", json={
            "login": "admin@bitmail.com",
            "password": "admin123"
        })
        cls.token = login_res.json()["token"]
        cls.auth_headers = {"Authorization": f"Bearer {cls.token}"}

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH = cls.orig_db
        cls.test_dir.cleanup()

    def test_blacklist_check_endpoint(self):
        res = self.client.post(
            "/api/deliverability/blacklist/check",
            headers=self.auth_headers,
            json={"target": "1.1.1.1"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["target"], "1.1.1.1")
        self.assertGreaterEqual(data["total_zones_checked"], 25)

    def test_blacklist_zones_endpoint(self):
        res = self.client.get("/api/deliverability/blacklist/zones", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertGreaterEqual(data["total_zones"], 30)

    def test_inbound_bounce_endpoint(self):
        res = self.client.post(
            "/api/bounces/inbound",
            json={
                "recipient_email": "inbound_bounced_mta@example.com",
                "bounce_type": "hard",
                "status_code": "5.1.1",
                "reason": "Unknown user rejected by MTA"
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["suppressed"])
        self.assertEqual(data["bounce_type"], "hard")

    def test_page_routes_render_ok(self):
        # Deliverability page
        deliv_res = self.client.get("/deliverability")
        self.assertEqual(deliv_res.status_code, 200)
        self.assertIn("Live Blacklist Monitor", deliv_res.text)

        # Webhooks page
        wh_res = self.client.get("/webhooks")
        self.assertEqual(wh_res.status_code, 200)
        self.assertIn("Webhooks & Integrations", wh_res.text)


if __name__ == "__main__":
    unittest.main()
