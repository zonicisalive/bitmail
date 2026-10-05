"""
Automated Unit and Integration Test Suite for Pre-Send Mailbox Availability & Safety Guard.
Tests:
1. Role-based account detection (admin@, support@, etc.)
2. Suppression and bounce blacklist checking
3. Live DNS MX and domain availability check
4. Disposable/burner detection
5. Comprehensive sendability evaluation (RECOMMENDED, NOT_RECOMMENDED, DO_NOT_SEND)
6. Strict safety mode enforcement
7. Batch safety evaluation
8. Pre-send dispatch gate in send_single_email (verifies auto-skip without dialing SMTP)
9. FastAPI REST endpoints (/api/deliverability/safety-lookup and /safety-batch-lookup)
"""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import settings
from app.db import get_db, init_db, utc_now_iso
from app.deliverability import PreSendSafetyGuard
from app.main import app
from app.models import EmailStatus, SendabilityVerdict
from app.sender import send_single_email


class TestPreSendSafetyGuard(unittest.IsolatedAsyncioTestCase):
    """Unit tests for PreSendSafetyGuard core engine."""

    async def asyncSetUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.test_dir.name) / "test_safety.db"
        self.orig_db = settings.DATABASE_PATH
        settings.DATABASE_PATH = self.db_path
        await init_db()

    async def asyncTearDown(self):
        settings.DATABASE_PATH = self.orig_db
        self.test_dir.cleanup()

    def test_role_account_detection(self):
        role_emails = [
            "admin@company.com",
            "support@service.io",
            "support+urgent@service.io",
            "billing@saas.com",
            "postmaster@domain.org",
            "abuse@isp.net",
            "info@startup.dev",
            "sales@agency.com",
            "careers@enterprise.com",
            "noreply@automated.com",
        ]
        for em in role_emails:
            is_role, reason = PreSendSafetyGuard.is_role_account(em)
            self.assertTrue(is_role, f"Expected {em} to be identified as role account")
            self.assertIsNotNone(reason)

        personal_emails = [
            "john.doe@company.com",
            "sarah_smith@gmail.com",
            "developer.alex@tech.io",
            "lead.engineer@bitnade.com",
        ]
        for em in personal_emails:
            is_role, reason = PreSendSafetyGuard.is_role_account(em)
            self.assertFalse(is_role, f"Expected {em} to be identified as personal account")
            self.assertIsNone(reason)

    async def test_suppression_and_bounce_check(self):
        now = utc_now_iso()
        # Seed suppression and bounced subscriber
        async with get_db() as db:
            await db.execute("""
                INSERT INTO suppressions (id, email, reason, created_at)
                VALUES ('sup_1', 'suppressed@badleads.com', 'user_unsubscribed', ?)
            """, (now,))
            await db.execute("""
                INSERT INTO subscribers (id, email, first_name, last_name, status, created_at, updated_at)
                VALUES ('sub_bounced', 'hardbounce@target.com', 'Bad', 'Lead', 'bounced', ?, ?)
            """, (now, now))
            await db.execute("""
                INSERT INTO subscribers (id, email, first_name, last_name, status, created_at, updated_at)
                VALUES ('sub_active', 'goodlead@target.com', 'Good', 'Lead', 'active', ?, ?)
            """, (now, now))
            await db.commit()

        # 1. Suppressed email
        is_sup, reason = await PreSendSafetyGuard.check_suppression("suppressed@badleads.com")
        self.assertTrue(is_sup)
        self.assertIn("blacklisted", reason)

        # 2. Bounced subscriber
        is_sup2, reason2 = await PreSendSafetyGuard.check_suppression("hardbounce@target.com")
        self.assertTrue(is_sup2)
        self.assertIn("bounced", reason2)

        # 3. Active subscriber
        is_sup3, reason3 = await PreSendSafetyGuard.check_suppression("goodlead@target.com")
        self.assertFalse(is_sup3)
        self.assertIsNone(reason3)

    async def test_evaluate_sendability_recommended(self):
        # Using a well-known domain with active MX
        res = await PreSendSafetyGuard.evaluate_sendability("test.user@gmail.com")
        self.assertEqual(res["verdict"], "recommended")
        self.assertTrue(res["is_safe_to_send"])
        self.assertGreaterEqual(res["safety_score"], 90)
        self.assertTrue(res["checks"]["syntax"]["passed"])
        self.assertTrue(res["checks"]["domain_mx"]["passed"])
        self.assertTrue(res["checks"]["disposable"]["passed"])
        self.assertTrue(res["checks"]["role_account"]["passed"])

    async def test_evaluate_sendability_syntax_error(self):
        res = await PreSendSafetyGuard.evaluate_sendability("invalid-syntax@@bad.com")
        self.assertEqual(res["verdict"], "do_not_send")
        self.assertFalse(res["is_safe_to_send"])
        self.assertEqual(res["safety_score"], 0)
        self.assertFalse(res["checks"]["syntax"]["passed"])

    async def test_evaluate_sendability_dead_domain_no_mx(self):
        res = await PreSendSafetyGuard.evaluate_sendability("user@nonexistent-fake-domain-xyz-987.com")
        self.assertEqual(res["verdict"], "do_not_send")
        self.assertFalse(res["is_safe_to_send"])
        self.assertEqual(res["safety_score"], 0)
        self.assertFalse(res["checks"]["domain_mx"]["passed"])

    async def test_evaluate_sendability_burner_domain(self):
        res = await PreSendSafetyGuard.evaluate_sendability("fakelead@mailinator.com")
        self.assertEqual(res["verdict"], "not_recommended")
        self.assertTrue(res["is_safe_to_send"])  # Permissive default allows with warning
        self.assertLess(res["safety_score"], 80)
        self.assertFalse(res["checks"]["disposable"]["passed"])

    async def test_evaluate_sendability_role_account(self):
        res = await PreSendSafetyGuard.evaluate_sendability("support@gmail.com")
        self.assertEqual(res["verdict"], "not_recommended")
        self.assertTrue(res["is_safe_to_send"])
        self.assertFalse(res["checks"]["role_account"]["passed"])

    async def test_evaluate_sendability_strict_mode(self):
        # Under strict mode, role accounts and burners become DO_NOT_SEND
        res_role = await PreSendSafetyGuard.evaluate_sendability("admin@gmail.com", strict_mode=True)
        self.assertEqual(res_role["verdict"], "do_not_send")
        self.assertFalse(res_role["is_safe_to_send"])

        res_burner = await PreSendSafetyGuard.evaluate_sendability("user@tempmail.com", strict_mode=True)
        self.assertEqual(res_burner["verdict"], "do_not_send")
        self.assertFalse(res_burner["is_safe_to_send"])

    async def test_evaluate_batch(self):
        emails = [
            "valid.user@gmail.com",
            "support@gmail.com",           # role account (not_recommended)
            "burner@mailinator.com",        # burner (not_recommended)
            "bad@@syntax.com",              # invalid syntax (do_not_send)
            "ghost@nonexistent-xyz-999.com" # no MX (do_not_send)
        ]
        batch_res = await PreSendSafetyGuard.evaluate_batch(emails, strict_mode=False)
        summary = batch_res["summary"]
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["recommended_count"], 1)
        self.assertEqual(summary["not_recommended_count"], 2)
        self.assertEqual(summary["do_not_send_count"], 2)
        self.assertEqual(len(batch_res["clean_emails"]), 3)  # 1 recommended + 2 permissible risky

    async def test_pre_send_dispatch_gate_blocks_invalid_emails(self):
        # Dispatch to a dead domain: should be intercepted by safety guard before dialing SMTP
        res = await send_single_email(
            recipient_email="nobody@nonexistent-fake-domain-xyz-12345.com",
            subject="Test Subject",
            body_text="Hello World",
            pre_send_safety=True
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], EmailStatus.SKIPPED.value)
        self.assertIn("Pre-send Safety Guard blocked send", res["error"])

        # Check that it was saved to DB as skipped
        async with get_db() as db:
            async with db.execute(
                "SELECT status, error_message FROM sent_emails WHERE id = ?", (res["sent_email_id"],)
            ) as cur:
                row = await cur.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["status"], EmailStatus.SKIPPED.value)
                self.assertIn("Pre-send Safety Guard", row["error_message"])


class TestSafetyEndpoints(unittest.TestCase):
    """Integration tests for Safety REST API routes."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.test_dir.name) / "test_safety_api.db"
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

    def test_safety_lookup_endpoint(self):
        res = self.client.post(
            "/api/deliverability/safety-lookup",
            headers=self.auth_headers,
            json={
                "email": "support@bitnade.com",
                "probe_smtp": False,
                "strict_mode": False
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["email"], "support@bitnade.com")
        self.assertIn(data["verdict"], ["recommended", "not_recommended", "do_not_send"])
        self.assertIn("checks", data)
        self.assertIn("safety_score", data)
        self.assertIn("syntax", data["checks"])
        self.assertIn("domain_mx", data["checks"])
        self.assertIn("role_account", data["checks"])

    def test_safety_batch_lookup_endpoint(self):
        res = self.client.post(
            "/api/deliverability/safety-batch-lookup",
            headers=self.auth_headers,
            json={
                "emails": [
                    "lead1@gmail.com",
                    "fake@mailinator.com",
                    "broken-syntax@@@"
                ],
                "strict_mode": False
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        summary = data["summary"]
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["do_not_send_count"], 1)  # broken-syntax
        self.assertIn("clean_emails", data)


if __name__ == "__main__":
    unittest.main()
