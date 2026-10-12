"""
SMTP relays, the sent-email vault, dashboard stats, server-rendered pages, transactional
sends and suppression checks are private per user; system logs are admin-only.
"""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import set_current_user_id
from app.config import settings
from app.db import init_db
from app.deliverability import PreSendSafetyGuard
from app.main import app


class TestVaultIsolation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(cls.tmp.name)
        cls.orig = (settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR)
        settings.DATABASE_PATH = data_dir / "iso_vault.db"
        settings.EML_ARCHIVE_DIR = settings.EML_STORAGE_DIR = data_dir / "eml"
        settings.EML_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        asyncio.run(init_db())

        cls.admin = TestClient(app)
        cls.admin.headers["Authorization"] = f"Bearer {cls._login(cls.admin, 'admin@bitmail.com', 'admin123')}"
        for email in ("va@iso.test", "vb@iso.test"):
            res = cls.admin.post("/api/auth/users", json={"email": email, "password": "Passw0rd!x", "role": "user"})
            assert res.status_code in (200, 201), res.text
        cls.a, cls.b = TestClient(app), TestClient(app)
        cls.a.headers["Authorization"] = f"Bearer {cls._login(cls.a, 'va@iso.test', 'Passw0rd!x')}"
        cls.b.headers["Authorization"] = f"Bearer {cls._login(cls.b, 'vb@iso.test', 'Passw0rd!x')}"
        cls.uid_a = cls.a.get("/api/auth/me").json()["id"]
        cls.uid_b = cls.b.get("/api/auth/me").json()["id"]

        res = cls.a.post("/api/smtp", json={"name": "A-Relay-Secret", "host": "sandbox", "port": 25, "is_default": True})
        assert res.status_code == 201, res.text
        cls.smtp_a = res.json()["id"]

        # Same recipient address in both users' vaults; A also has an .eml on disk, an event and a suppression.
        cls.eml_a = settings.EML_STORAGE_DIR / "eml_a.eml"
        cls.eml_a.write_bytes(b"Subject: A secret\r\n\r\nhi")
        con = sqlite3.connect(settings.DATABASE_PATH)
        for sid, uid, subject, path in (("eml_a", cls.uid_a, "A secret subject", str(cls.eml_a)),
                                        ("eml_b", cls.uid_b, "B subject", None)):
            con.execute(
                "INSERT INTO sent_emails (id, recipient_email, sender_email, subject, body_html, status, "
                "raw_eml_path, created_at, user_id) VALUES (?, 'same@x.com', 's@x.com', ?, '<p>x</p>', 'failed', ?, "
                "datetime('now'), ?)", (sid, subject, path, uid))
        con.execute("INSERT INTO email_events (id, sent_email_id, event_type, created_at) "
                    "VALUES ('ev_a', 'eml_a', 'open', datetime('now'))")
        con.execute("INSERT INTO suppressions (id, email, reason, created_at, user_id) "
                    "VALUES ('sup_a', 'blocked@x.com', 'manual', datetime('now'), ?)", (cls.uid_a,))
        con.commit()
        con.close()

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR = cls.orig
        cls.tmp.cleanup()

    @staticmethod
    def _login(client, login, password):
        res = client.post("/api/auth/login", json={"login": login, "password": password})
        assert res.status_code == 200, res.text
        return res.json()["token"]

    def test_smtp_relays_are_private(self):
        self.assertEqual(self.b.get("/api/smtp").json(), [])
        self.assertEqual(self.b.get(f"/api/smtp/{self.smtp_a}").status_code, 404)
        self.assertEqual(self.b.put(f"/api/smtp/{self.smtp_a}", json={"name": "pwn"}).status_code, 404)
        self.assertEqual(self.b.delete(f"/api/smtp/{self.smtp_a}").status_code, 404)

        # B's default does not clear A's default.
        res = self.b.post("/api/smtp", json={"name": "B-Relay", "host": "sandbox", "port": 25, "is_default": True})
        self.assertEqual(res.status_code, 201, res.text)
        a_relay = self.a.get(f"/api/smtp/{self.smtp_a}").json()
        self.assertTrue(a_relay["is_default"])
        self.assertEqual(a_relay["name"], "A-Relay-Secret")
        self.assertEqual([r["name"] for r in self.b.get("/api/smtp").json()], ["B-Relay"])

    def test_vault_is_private(self):
        for client, mine in ((self.a, "eml_a"), (self.b, "eml_b")):
            ids = [e["id"] for e in client.get("/api/storage/emails").json()["emails"]]
            self.assertEqual(ids, [mine])
            ids = [e["id"] for e in client.get("/api/v1/vault/emails").json()["emails"]]
            self.assertEqual(ids, [mine])
            self.assertEqual(client.get("/api/storage/summary").json()["total_archived"], 1)

        for path in ("", "/rendered", "/eml"):
            self.assertEqual(self.b.get(f"/api/storage/emails/eml_a{path}").status_code, 404, path)
        self.assertEqual(self.b.post("/api/storage/emails/eml_a/resend", json={}).status_code, 404)
        self.assertEqual(self.b.delete("/api/storage/emails/eml_a").status_code, 404)

        res = self.a.get("/api/storage/emails/eml_a/eml")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"A secret", res.content)

    def test_dashboard_is_private(self):
        stats = self.b.get("/api/dashboard/stats").json()
        self.assertEqual(stats["total_stored_emails"], 1)
        self.assertEqual(stats["suppressed_count"], 0)
        self.assertNotIn("A secret subject", str(stats))
        self.assertNotEqual(stats["relay"]["name"], "A-Relay-Secret")
        self.assertEqual(self.b.get("/api/dashboard/activity").json(), [])
        self.assertEqual(len(self.a.get("/api/dashboard/activity").json()), 1)
        self.assertEqual(sum(self.b.get("/api/dashboard/chart").json()["datasets"]["sent"]), 1)

    def test_pages_are_private(self):
        self.assertNotIn("A-Relay-Secret", self.b.get("/smtp").text)
        self.assertNotIn("A secret subject", self.b.get("/vault").text)
        anon = TestClient(app)
        page = anon.get("/smtp").text
        self.assertNotIn("A-Relay-Secret", page)
        self.assertNotIn("A secret subject", anon.get("/vault").text)

    def test_transactional_cannot_use_other_users_relay(self):
        res = self.b.post("/api/v1/send", json={
            "recipient_email": "t@x.com", "subject": "s", "body_html": "<p>x</p>", "smtp_config_id": self.smtp_a,
        })
        self.assertEqual(res.status_code, 404, res.text)

    def test_logs_are_admin_only(self):
        self.assertEqual(self.b.get("/api/logs").status_code, 403)
        self.assertEqual(self.admin.get("/api/logs").status_code, 200)

    def test_suppression_check_is_per_user(self):
        async def check(uid):
            set_current_user_id(uid)
            return (await PreSendSafetyGuard.check_suppression("blocked@x.com"))[0]
        self.assertTrue(asyncio.run(check(self.uid_a)))
        self.assertFalse(asyncio.run(check(self.uid_b)))


if __name__ == "__main__":
    unittest.main()
