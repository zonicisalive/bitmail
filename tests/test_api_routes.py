"""
End-to-End Test Suite for all FastAPI REST API Endpoints.
Tests Dashboard, Subscribers, Templates, Campaigns, Storage Vault, Tracking, SMTP, and Transactional APIs.
"""

import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db
from app.main import app


class TestApiRoutes(unittest.TestCase):
    """E2E REST API Integration Test Suite."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.test_dir.name) / "data"
        cls.archive_dir = cls.data_dir / "eml_archive"
        cls.db_path = cls.data_dir / "test_api.db"
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

        # Authenticate test client with default seeded administrator
        login_res = cls.client.post("/api/auth/login", json={
            "login": "admin@bitmail.com",
            "password": "admin123"
        })
        assert login_res.status_code == 200, f"Failed to login seeded admin: {login_res.text}"
        cls.token = login_res.json()["token"]
        cls.client.headers["Authorization"] = f"Bearer {cls.token}"

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH = cls.orig_db
        settings.EML_ARCHIVE_DIR = cls.orig_archive
        settings.EML_STORAGE_DIR = cls.orig_storage
        settings.TRACKING_BASE_URL = cls.orig_tracking
        cls.test_dir.cleanup()

    def test_00_auth_lock_and_session_lifecycle(self):
        """Test that all protected APIs strictly reject unauthenticated requests and accept valid login tokens."""
        # 1. Unauthenticated client MUST be rejected with 401
        raw_client = TestClient(app)
        res = raw_client.get("/api/dashboard/stats")
        self.assertEqual(res.status_code, 401)
        self.assertIn("Authentication required", res.json().get("detail", ""))

        res2 = raw_client.get("/api/subscribers")
        self.assertEqual(res2.status_code, 401)

        res3 = raw_client.get("/api/templates")
        self.assertEqual(res3.status_code, 401)

        # 2. Bad credentials MUST be rejected with 401
        bad_login = raw_client.post("/api/auth/login", json={
            "login": "admin@bitmail.com",
            "password": "WrongPassword999"
        })
        self.assertEqual(bad_login.status_code, 401)

        # 3. Successful login with username as well as email
        good_login = raw_client.post("/api/auth/login", json={
            "login": "admin",
            "password": "admin123"
        })
        self.assertEqual(good_login.status_code, 200)
        auth_data = good_login.json()
        self.assertTrue(auth_data["success"])
        self.assertIn("token", auth_data)
        self.assertEqual(auth_data["user"]["email"], "admin@bitmail.com")

        # 4. Verify /api/auth/me returns current user
        me_res = raw_client.get("/api/auth/me", headers={"Authorization": f"Bearer {auth_data['token']}"})
        self.assertEqual(me_res.status_code, 200)
        self.assertEqual(me_res.json()["email"], "admin@bitmail.com")

        # 5. Direct QR Scan session and secure approval lifecycle
        scan_sess_res = raw_client.post("/api/auth/scan/session")
        self.assertEqual(scan_sess_res.status_code, 200)
        session_id = scan_sess_res.json()["session_id"]
        scan_token = scan_sess_res.json()["token"]

        # Backdoor simulate-approval endpoint must not exist (404)
        sim_res = raw_client.post(f"/api/auth/scan/simulate-approval/{session_id}")
        self.assertEqual(sim_res.status_code, 404)

        # Unauthenticated approval attempt must be rejected (401)
        unauth_client = TestClient(app)
        unauth_appr = unauth_client.post("/api/auth/scan/approve", json={"token": scan_token})
        self.assertEqual(unauth_appr.status_code, 401)

        # Authenticated approval succeeds
        auth_appr = raw_client.post(
            "/api/auth/scan/approve",
            json={"token": scan_token},
            headers={"Authorization": f"Bearer {auth_data['token']}"}
        )
        self.assertEqual(auth_appr.status_code, 200)

        # Status without secret scan token does not disclose auth_token
        status_unauth = raw_client.get(f"/api/auth/scan/session/{session_id}/status")
        self.assertEqual(status_unauth.status_code, 200)
        self.assertIsNone(status_unauth.json()["auth_token"])

        # Status with secret scan token discloses auth_token
        status_auth = raw_client.get(f"/api/auth/scan/session/{session_id}/status?token={scan_token}")
        self.assertEqual(status_auth.status_code, 200)
        disclosed_token = status_auth.json()["auth_token"]
        self.assertIsNotNone(disclosed_token)

        # Disclosed auth_token can now access protected APIs
        sub_check = raw_client.get("/api/subscribers", headers={"Authorization": f"Bearer {disclosed_token}"})
        self.assertEqual(sub_check.status_code, 200)

    def test_01_health_and_static(self):
        """Test healthcheck and SPA static root."""
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "healthy")

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])

    def test_02_dashboard_kpis_and_chart(self):
        """Test dashboard statistics, activity stream, and deliverability charts."""
        res = self.client.get("/api/dashboard/stats")
        self.assertEqual(res.status_code, 200)
        stats = res.json()
        self.assertIn("total_sent", stats)
        self.assertIn("active_subscribers", stats)

        res = self.client.get("/api/dashboard/activity")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)

        res = self.client.get("/api/dashboard/chart")
        self.assertEqual(res.status_code, 200)
        chart_data = res.json()
        self.assertIn("labels", chart_data)
        self.assertIn("delivered", chart_data["datasets"])

    def test_03_subscriber_and_list_crud(self):
        """Test creating, reading, updating, and querying subscribers and subscriber lists."""
        # 1. Create a list
        res = self.client.post("/api/lists", json={
            "name": "Beta Testers",
            "description": "Early adopters community"
        })
        self.assertIn(res.status_code, (200, 201))
        list_obj = res.json()
        list_id = list_obj["id"]
        self.assertEqual(list_obj["name"], "Beta Testers")

        # 2. Create subscriber
        res = self.client.post("/api/subscribers", json={
            "email": "alex.test@startup.io",
            "first_name": "Alex",
            "last_name": "Vance",
            "custom_fields": {"company": "Startup Labs", "plan": "Scale"},
            "list_ids": [list_id]
        })
        self.assertIn(res.status_code, (200, 201))
        sub = res.json()
        self.assertEqual(sub["email"], "alex.test@startup.io")

        # 3. List subscribers with search query
        res = self.client.get("/api/subscribers?search=alex")
        self.assertEqual(res.status_code, 200)
        sub_list = res.json()
        items = sub_list if isinstance(sub_list, list) else sub_list.get("items", sub_list.get("subscribers", []))
        self.assertTrue(any(s["email"] == "alex.test@startup.io" for s in items))

        # 4. Import CSV batch
        csv_data = "email,first_name,last_name,company\nuser1@test.com,User,One,Acme\nuser2@test.com,User,Two,Globex\n"
        res = self.client.post(
            f"/api/subscribers/import-csv?list_id={list_id}",
            files={"file": ("users.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
        )
        self.assertIn(res.status_code, (200, 201))
        import_res = res.json()
        self.assertGreaterEqual(import_res.get("imported", import_res.get("count", 2)), 1)

    def test_04_template_studio(self):
        """Test template creation, preview interpolation, and cloning."""
        res = self.client.post("/api/templates", json={
            "name": "Special Promo",
            "subject": "Exclusive Discount for {{first_name}}!",
            "body_html": "<p>Hello {{first_name}} from {{company}}, use code <strong>SAVE50</strong>. <a href='https://example.com/shop'>Shop Now</a></p>",
            "body_text": "Hello {{first_name}}, use code SAVE50."
        })
        self.assertIn(res.status_code, (200, 201))
        tmpl = res.json()
        tmpl_id = tmpl["id"]

        # Preview template
        preview_res = self.client.post("/api/templates/preview", json={
            "body_html": tmpl["body_html"],
            "subject": tmpl["subject"],
            "sample_variables": {"first_name": "Diana", "company": "CloudTech"}
        })
        self.assertEqual(preview_res.status_code, 200)
        preview_data = preview_res.json()
        self.assertIn("Exclusive Discount for Diana!", preview_data["rendered_subject"])
        self.assertIn("Diana from CloudTech", preview_data.get("rendered_body_html", preview_data.get("rendered_html", "")))

        # Clone template
        clone_res = self.client.post(f"/api/templates/{tmpl_id}/clone")
        self.assertIn(clone_res.status_code, (200, 201))
        self.assertIn("Copy", clone_res.json()["name"])

    def test_05_smtp_config_and_diagnostic(self):
        """Test SMTP configuration management and diagnostic connection runner."""
        # The dry-run relay the later send tests dispatch through.
        res = self.client.post("/api/smtp", json={
            "name": "Sandbox Dry-Run Relay",
            "host": "sandbox",
            "port": 587,
            "use_tls": False,
            "use_ssl": False,
            "rate_limit_per_second": 30,
            "is_default": True
        })
        self.assertIn(res.status_code, (200, 201))
        smtp_cfg = res.json()
        self.assertEqual(smtp_cfg["name"], "Sandbox Dry-Run Relay")

        # Probing the sandbox relay never touches the network.
        sandbox_diag = self.client.post("/api/smtp/test", json={"host": "sandbox"})
        self.assertEqual(sandbox_diag.status_code, 200)
        self.assertTrue(sandbox_diag.json()["success"])

        # A dead port must be reported as a failure, not papered over as a
        # "local loopback simulation" - that is how silent non-delivery happens.
        diag_res = self.client.post("/api/smtp/test", json={
            "host": "127.0.0.1",
            "port": 1,
            "use_tls": False,
            "use_ssl": False
        })
        self.assertEqual(diag_res.status_code, 200)
        diag_data = diag_res.json()
        self.assertFalse(diag_data["success"])
        self.assertIn("steps", diag_data.get("details", {}))

    def test_05b_send_without_relay_fails_loudly(self):
        """A send with no usable relay must report failure instead of a fake 250."""
        from app.sender import dispatch_smtp_message
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart("alternative")
        msg["Message-ID"] = "<probe@test>"
        ok, detail, _ = asyncio.run(
            dispatch_smtp_message(msg, "a@b.com", "c@d.com", smtp_config=None)
        )
        self.assertFalse(ok)
        self.assertIn("No SMTP relay configured", detail)

    def test_06_transactional_send_and_storage_vault(self):
        """Test sending transactional email, archiving in Email Storage Vault, and retrieving headers/EML."""
        res = self.client.post("/api/v1/send", json={
            "recipient_email": "clara.oswald@spacefleet.org",
            "recipient_name": "Clara Oswald",
            "subject": "Your Access Token: {{token}}",
            "body_html": "<p>Hi {{name}}, your login token is <code>{{token}}</code>. <a href='https://spacefleet.org/verify?token={{token}}'>Verify Account</a></p>",
            "template_context": {"token": "SEC-9988-ABC", "name": "Clara"},
            "track_opens": True,
            "track_clicks": True,
            "tags": ["auth", "security", "onboarding"]
        })
        self.assertIn(res.status_code, (200, 201))
        tx_data = res.json()
        self.assertTrue(tx_data["success"])
        storage_id = tx_data["sent_email_id"]

        # 1. Query Email Storage Vault
        vault_res = self.client.get("/api/storage/emails?search=clara.oswald")
        self.assertEqual(vault_res.status_code, 200)
        v_data = vault_res.json()
        vault_emails = v_data["emails"] if "emails" in v_data else v_data
        self.assertTrue(any(e["id"] == storage_id for e in vault_emails))

        # 2. Get Single Email from Storage Vault
        email_detail = self.client.get(f"/api/storage/emails/{storage_id}")
        self.assertEqual(email_detail.status_code, 200)
        data = email_detail.json()
        self.assertEqual(data["recipient_email"], "clara.oswald@spacefleet.org")
        self.assertIn("SEC-9988-ABC", data.get("rendered_html", data.get("body_html", "")))

        # 3. Get Rendered HTML iframe view
        render_res = self.client.get(f"/api/storage/emails/{storage_id}/rendered")
        self.assertEqual(render_res.status_code, 200)
        self.assertIn("text/html", render_res.headers["content-type"])
        self.assertIn("SEC-9988-ABC", render_res.text)

        # 4. Download RFC 822 / 5322 .EML raw file
        eml_res = self.client.get(f"/api/storage/emails/{storage_id}/eml")
        self.assertEqual(eml_res.status_code, 200)
        self.assertIn("message/rfc822", eml_res.headers["content-type"])

        # 5. Vault storage summary
        summary_res = self.client.get("/api/storage/summary")
        self.assertEqual(summary_res.status_code, 200)
        summary = summary_res.json()
        self.assertGreaterEqual(summary["total_archived"], 1)

    def test_07_open_click_and_unsubscribe_tracking(self):
        """Test open tracking pixel beacon, click redirection, and 1-click unsubscribe."""
        tx_res = self.client.post("/api/v1/send", json={
            "recipient_email": "tracker.user@demo.com",
            "recipient_name": "Tracker User",
            "subject": "Track Me",
            "body_html": "<p>Hello <a href='https://example.com/target-landing-page'>Click Link</a></p>",
            "track_opens": True,
            "track_clicks": True
        })
        self.assertIn(tx_res.status_code, (200, 201))
        storage_id = tx_res.json()["sent_email_id"]

        # 1. Trigger Open Tracking Pixel
        open_res = self.client.get(f"/track/open/{storage_id}")
        self.assertEqual(open_res.status_code, 200)
        self.assertEqual(open_res.headers["content-type"], "image/png")

        # 2. Trigger Click Tracking Redirect
        click_res = self.client.get(
            f"/track/click/{storage_id}?url=https%3A%2F%2Fexample.com%2Ftarget-landing-page",
            follow_redirects=False
        )
        self.assertIn(click_res.status_code, (302, 307))
        self.assertEqual(click_res.headers["location"], "https://example.com/target-landing-page")

        # 3. Test Unsubscribe endpoint
        unsub_res = self.client.get(f"/unsubscribe/{storage_id}")
        self.assertEqual(unsub_res.status_code, 200)
        self.assertIn("text/html", unsub_res.headers["content-type"])
        self.assertIn("Unsubscribed", unsub_res.text)

    def test_07b_campaign_footer_unsubscribe_link_works(self):
        """The compliance footer addresses the mail by storage id + signed token.
        That link must actually suppress the lead."""
        from app.template_engine import template_engine

        # Deliberately an id with no sent_emails row, so only the signed token can
        # resolve the address - this isolates the token path from the id fallback.
        storage_id = "eml_footer_only_00001"
        email = "footer.lead@demo.com"

        bad = self.client.get(f"/unsubscribe/{storage_id}?token=deadbeef&email={email}")
        self.assertEqual(bad.status_code, 200)
        self.assertNotIn(email, bad.text)

        token = template_engine.generate_unsubscribe_token(storage_id, email)
        res = self.client.get(f"/unsubscribe/{storage_id}?token={token}&email={email}")
        self.assertEqual(res.status_code, 200)
        self.assertIn(email, res.text)

        import sqlite3
        with sqlite3.connect(settings.DATABASE_PATH) as conn:
            hits = conn.execute("SELECT COUNT(*) FROM suppressions WHERE email = ?", (email,)).fetchone()[0]
        self.assertEqual(hits, 1)

    def test_08_quick_broadcast_and_bulk_customer_add(self):
        """Test bulk pasting customer emails and launching quick mass broadcast."""
        # 1. Bulk add customers via raw text
        raw_text = "elon@spacex.com\n\"Tim Cook\" <tim@apple.com>\nsatya@microsoft.com, sundar@google.com"
        bulk_res = self.client.post("/api/subscribers/bulk-text", json={
            "raw_text": raw_text,
            "tags": ["vip", "tech-leaders"]
        })
        self.assertEqual(bulk_res.status_code, 200)
        bulk_data = bulk_res.json()
        self.assertTrue(bulk_data["success"])
        self.assertEqual(bulk_data["total_parsed"], 4)

        # 2. Launch quick broadcast with pasted recipients
        broadcast_res = self.client.post("/api/campaigns/quick-broadcast", json={
            "subject": "Exclusive Invitation for {{first_name}}",
            "body_html": "<p>Hello {{first_name}}, welcome to our platform! <a href='https://example.com/vip'>Access VIP Portal</a></p>",
            "recipients_text": "client1@corp.com, \"Client Two\" <client2@corp.com>, client3@corp.com",
            "rate_limit_per_second": 50,
            "track_opens": True,
            "track_clicks": True
        })
        self.assertEqual(broadcast_res.status_code, 200)
        b_data = broadcast_res.json()
        self.assertTrue(b_data["success"])
        self.assertEqual(b_data["total_recipients"], 3)
        self.assertEqual(b_data["status"], "sending")

        # Allow background dispatch tasks to settle before teardown
        import time
        time.sleep(0.3)

    def test_09_bulk_delete(self):
        """Mass delete removes every valid id, reports the bad ones, and rejects unknown resources."""
        created = []
        for i in range(3):
            res = self.client.post("/api/subscribers", json={"email": f"bulk-victim-{i}@example.com"})
            self.assertEqual(res.status_code, 201)
            created.append(res.json()["id"])

        # A missing id must not abort the rest of the batch.
        res = self.client.post("/api/bulk-delete", json={
            "resource": "subscribers",
            "ids": created + ["does-not-exist"]
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["deleted"], 3)
        self.assertEqual(data["failed_count"], 1)

        for sub_id in created:
            self.assertEqual(self.client.get(f"/api/subscribers/{sub_id}").status_code, 404)

        res = self.client.post("/api/bulk-delete", json={"resource": "hackers", "ids": ["x"]})
        self.assertEqual(res.status_code, 400)

        res = self.client.post("/api/bulk-delete", json={"resource": "subscribers", "ids": []})
        self.assertEqual(res.status_code, 422)

    def test_10_placeholders_and_csv_custom_fields(self):
        """Importing CSV with arbitrary custom columns detects placeholders and exposes them via API."""
        csv_content = (
            "email,first_name,last_name,phone,membership_tier,discount_code\n"
            "vip1@example.com,Alice,Walker,+123456789,Gold,VIP2026\n"
            "vip2@example.com,Bob,Davis,+987654321,Platinum,PLATINUM50\n"
        )
        files = {"file": ("customers.csv", csv_content.encode("utf-8"), "text/csv")}
        import_res = self.client.post("/api/subscribers/import-csv", files=files)
        self.assertEqual(import_res.status_code, 200)
        import_data = import_res.json()
        self.assertEqual(import_data["added_count"], 2)
        self.assertIn("phone", import_data["custom_fields_detected"])
        self.assertIn("membership_tier", import_data["custom_fields_detected"])
        self.assertIn("discount_code", import_data["custom_fields_detected"])

        # Query placeholders endpoint
        pl_res = self.client.get("/api/subscribers/placeholders")
        self.assertEqual(pl_res.status_code, 200)
        pl_data = pl_res.json()
        self.assertIn("phone", pl_data["custom_fields"])
        self.assertIn("membership_tier", pl_data["custom_fields"])
        self.assertIn("discount_code", pl_data["custom_fields"])
        self.assertIn("first_name", pl_data["standard_placeholders"])

    def test_11_template_preview_with_custom_context(self):
        """Preview endpoint resolves standard and custom placeholder tags accurately."""
        preview_res = self.client.post("/api/templates/preview", json={
            "subject_template": "Hello {{first_name}}, your code is {{discount_code}}!",
            "body_template": "<p>Tier: {{membership_tier}}</p><p>Call {{phone}}</p><a href='{{unsubscribe_url}}'>Unsubscribe</a>",
            "context": {
                "first_name": "Alice",
                "discount_code": "DISC2026",
                "membership_tier": "VIP Gold",
                "phone": "+1-555-0199"
            }
        })
        self.assertEqual(preview_res.status_code, 200)
        p_data = preview_res.json()
        self.assertIn("Hello Alice, your code is DISC2026!", p_data["rendered_subject"])
        self.assertIn("Tier: VIP Gold", p_data["rendered_body"])
        self.assertIn("Call +1-555-0199", p_data["rendered_body"])
        self.assertIn("discount_code", p_data["detected_tags"])
        self.assertIn("membership_tier", p_data["detected_tags"])
        self.assertIn("phone", p_data["detected_tags"])

    def test_12_customer_groups_and_multitable_import(self):
        """Creating customer groups, importing CSV into group, and multi-table membership tracking."""
        # 1. Create a customer group
        group_res = self.client.post("/api/lists", json={
            "name": "High-Value VIPs",
            "description": "High tier recurring clients"
        })
        self.assertEqual(group_res.status_code, 201)
        group_id = group_res.json()["id"]

        # 2. Import CSV directly into this customer group
        csv_data = (
            "email,first_name,last_name,company,account_tier\n"
            "group_user1@acme.com,Diana,Prince,Themyscira Corp,Tier-1\n"
            "group_user2@wayne.com,Bruce,Wayne,Wayne Enterprises,Tier-1\n"
        )
        files = {"file": ("vip_cohort.csv", csv_data.encode("utf-8"), "text/csv")}
        import_res = self.client.post(
            "/api/subscribers/import-csv",
            data={"list_id": group_id, "update_duplicates": "true"},
            files=files
        )
        self.assertEqual(import_res.status_code, 200)
        i_data = import_res.json()
        self.assertEqual(i_data["added_count"], 2)
        self.assertIn("company", i_data["custom_fields_detected"])
        self.assertIn("account_tier", i_data["custom_fields_detected"])

        # 3. Verify group details and member counts
        detail_res = self.client.get(f"/api/lists/{group_id}")
        self.assertEqual(detail_res.status_code, 200)
        d_data = detail_res.json()
        self.assertEqual(d_data["subscriber_count"], 2)
        member_emails = [m["email"] for m in d_data["subscribers"]]
        self.assertIn("group_user1@acme.com", member_emails)
        self.assertIn("group_user2@wayne.com", member_emails)

        # 4. Verify subscriber record has list association and custom fields
        sub1 = next(m for m in d_data["subscribers"] if m["email"] == "group_user1@acme.com")
        self.assertIn("company", sub1["custom_fields"])
        self.assertEqual(sub1["custom_fields"]["company"], "Themyscira Corp")


if __name__ == "__main__":
    unittest.main()


