"""Settings page: switches are stored, validated, and actually change what is sent."""

import asyncio
import email
import tempfile
import unittest
from email import policy
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db
from app.main import app


class TestSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(cls.tmp.name)
        cls.orig = (settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR)
        settings.DATABASE_PATH = data_dir / "settings.db"
        settings.EML_ARCHIVE_DIR = settings.EML_STORAGE_DIR = data_dir / "eml"
        settings.EML_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        asyncio.run(init_db())
        cls.client = TestClient(app)
        res = cls.client.post("/api/auth/login", json={"login": "admin@bitmail.com", "password": "admin123"})
        cls.client.headers["Authorization"] = f"Bearer {res.json()['token']}"
        # A sandbox relay so sends complete without the network.
        cls.client.post("/api/smtp", json={"name": "Sandbox", "host": "sandbox", "port": 25, "is_default": True})

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR = cls.orig
        cls.tmp.cleanup()

    def _send(self, to, **extra):
        res = self.client.post("/api/v1/send", json={"recipient_email": to, "subject": "Hi", "body_html": "", "body_text": "Hello there", **extra})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def _headers(self, sent_email_id):
        eml = self.client.get(f"/api/storage/emails/{sent_email_id}/eml").text
        return email.message_from_string(eml, policy=policy.default)

    def test_defaults_and_validation(self):
        data = self.client.get("/api/settings").json()
        self.assertTrue(data["settings"]["list_unsubscribe_header"])
        self.assertEqual(data["settings"]["default_format"], "html")
        self.assertEqual(self.client.put("/api/settings", json={"nonsense": True}).status_code, 400)
        self.assertEqual(self.client.put("/api/settings", json={"default_format": "text"}).json()["settings"]["default_format"], "text")
        self.client.put("/api/settings", json={"default_format": "html"})
        self.assertEqual(TestClient(app).get("/api/settings").status_code, 401)

    def test_list_unsubscribe_switch_changes_outgoing_headers(self):
        on = self._send("on@example.com")
        self.assertIn("List-Unsubscribe", self._headers(on["sent_email_id"]))
        self.client.put("/api/settings", json={"list_unsubscribe_header": False})
        try:
            import app.app_settings as s; s._cache["values"] = None
            off = self._send("off@example.com")
            self.assertNotIn("List-Unsubscribe", self._headers(off["sent_email_id"]))
        finally:
            self.client.put("/api/settings", json={"list_unsubscribe_header": True})

    def test_text_only_send_is_single_plain_part(self):
        sent = self._send("plain@example.com")
        m = self._headers(sent["sent_email_id"])
        self.assertEqual(m.get_content_type(), "text/plain")
        self.assertNotIn("X-Campaign-ID", m)

    def test_safety_off_still_blocks_suppressed_addresses(self):
        sub = self.client.post("/api/subscribers", json={"email": "gone@example.com"}).json()
        self.client.put(f"/api/subscribers/{sub['id']}", json={"status": "unsubscribed"})
        self.client.put("/api/settings", json={"pre_send_safety": False})
        try:
            import app.app_settings as s; s._cache["values"] = None
            res = self.client.post("/api/v1/send", json={"recipient_email": "gone@example.com", "subject": "Hi", "body_text": "x", "body_html": ""})
            self.assertNotEqual(res.json().get("status"), "simulated", res.text)
            self.assertNotEqual(res.json().get("status"), "sent", res.text)
        finally:
            self.client.put("/api/settings", json={"pre_send_safety": True})


if __name__ == "__main__":
    unittest.main()
