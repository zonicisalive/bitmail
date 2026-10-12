"""
Contacts, lists and templates are private per user: user B can never see, change,
delete or use user A's rows, and both users can hold the same email address.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db
from app.main import app


class TestContactsIsolation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(cls.tmp.name)
        cls.orig = (settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR)
        settings.DATABASE_PATH = data_dir / "iso_contacts.db"
        settings.EML_ARCHIVE_DIR = settings.EML_STORAGE_DIR = data_dir / "eml"
        settings.EML_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        asyncio.run(init_db())

        admin = TestClient(app)
        cls.admin_token = cls._login(admin, "admin@bitmail.com", "admin123")
        admin.headers["Authorization"] = f"Bearer {cls.admin_token}"
        for email in ("a@iso.test", "b@iso.test"):
            res = admin.post("/api/auth/users", json={"email": email, "password": "Passw0rd!x", "role": "user"})
            assert res.status_code in (200, 201), res.text
        cls.a, cls.b = TestClient(app), TestClient(app)
        cls.a.headers["Authorization"] = f"Bearer {cls._login(cls.a, 'a@iso.test', 'Passw0rd!x')}"
        cls.b.headers["Authorization"] = f"Bearer {cls._login(cls.b, 'b@iso.test', 'Passw0rd!x')}"

        # User A's data.
        cls.list_a = cls.a.post("/api/lists", json={"name": "Shared Name"}).json()["id"]
        res = cls.a.post("/api/subscribers", json={
            "email": "same@x.com", "first_name": "Alice", "custom_fields": {"secret": "a-only"},
            "tags": ["vip"], "list_ids": [cls.list_a],
        })
        assert res.status_code == 201, res.text
        cls.sub_a = res.json()["id"]
        res = cls.a.post("/api/templates", json={"name": "A tpl", "subject": "Hi", "body_html": "<p>A</p>"})
        assert res.status_code == 201, res.text
        cls.tpl_a = res.json()["id"]

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR = cls.orig
        cls.tmp.cleanup()

    @staticmethod
    def _login(client, login, password):
        res = client.post("/api/auth/login", json={"login": login, "password": password})
        assert res.status_code == 200, res.text
        return res.json()["token"]

    def test_contacts_hidden_from_other_user(self):
        b = self.b
        res = b.get("/api/subscribers")
        self.assertEqual(res.json(), [])
        self.assertEqual(res.headers["X-Total-Count"], "0")
        self.assertEqual(b.get(f"/api/subscribers?list_id={self.list_a}").json(), [])
        self.assertEqual(b.get(f"/api/subscribers/{self.sub_a}").status_code, 404)
        self.assertEqual(b.put(f"/api/subscribers/{self.sub_a}", json={"first_name": "X"}).status_code, 404)
        self.assertEqual(b.delete(f"/api/subscribers/{self.sub_a}").status_code, 404)
        summary = b.get("/api/subscribers/summary").json()
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["tags"], [])
        export = b.get(f"/api/subscribers/export?ids={self.sub_a}").text
        self.assertNotIn("same@x.com", export)
        self.assertNotIn("same@x.com", b.get("/api/subscribers/export").text)
        ph = b.get("/api/subscribers/placeholders").json()
        self.assertNotIn("secret", ph["custom_tags"])
        self.assertNotIn("Shared Name", ph["list_names"])

    def test_bulk_actions_cannot_touch_other_user(self):
        for action in (
            {"action": "set_status", "status": "unsubscribed"},
            {"action": "add_tag", "tag": "hacked"},
            {"action": "delete"},
        ):
            res = self.b.post("/api/subscribers/bulk", json={"ids": [self.sub_a], **action})
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.json()["affected"], 0)
        res = self.b.post("/api/subscribers/bulk", json={"filter": {}, "action": "add_tag", "tag": "hacked"})
        self.assertEqual(res.json()["matched"], 0)
        res = self.b.post("/api/bulk-delete", json={"resource": "subscribers", "ids": [self.sub_a]})
        self.assertEqual(res.json()["deleted"], 0)

        sub = self.a.get(f"/api/subscribers/{self.sub_a}").json()
        self.assertEqual(sub["status"], "active")
        self.assertNotIn("hacked", sub["tags"])

    def test_lists_hidden_and_not_usable(self):
        b = self.b
        self.assertEqual(b.get("/api/lists").json(), [])
        self.assertEqual(b.get(f"/api/lists/{self.list_a}").status_code, 404)
        self.assertEqual(b.put(f"/api/lists/{self.list_a}", json={"name": "pwn"}).status_code, 404)
        self.assertEqual(b.delete(f"/api/lists/{self.list_a}").status_code, 404)
        self.assertEqual(b.post("/api/subscribers", json={"email": "z@x.com", "list_ids": [self.list_a]}).status_code, 404)
        self.assertEqual(b.post("/api/subscribers/bulk-text", json={"raw_text": "y@x.com", "list_id": self.list_a}).status_code, 404)
        res = b.post("/api/subscribers/import-csv", data={"list_id": self.list_a},
                     files={"file": ("c.csv", b"email\nq@x.com\n", "text/csv")})
        self.assertEqual(res.status_code, 404)
        own = b.post("/api/subscribers", json={"email": "own-b@x.com"}).json()["id"]
        res = b.post("/api/subscribers/bulk", json={"ids": [own], "action": "add_to_list", "list_id": self.list_a})
        self.assertEqual(res.status_code, 404)
        # Same list name is fine for a different user.
        self.assertEqual(b.post("/api/lists", json={"name": "Shared Name"}).status_code, 201)
        self.assertEqual(self.a.get(f"/api/lists/{self.list_a}").json()["subscriber_count"], 1)

    def test_same_email_coexists_per_user(self):
        res = self.b.post("/api/subscribers", json={"email": "same@x.com", "status": "unsubscribed"})
        self.assertEqual(res.status_code, 201, res.text)
        res = self.b.post("/api/subscribers/import-csv",
                          files={"file": ("c.csv", b"email,first_name\nsame@x.com,Bob\n", "text/csv")})
        self.assertEqual(res.json()["updated_count"], 1)
        self.assertEqual(res.json()["added_count"], 0)
        self.b.post("/api/subscribers/bulk-text", json={"raw_text": "same@x.com"})

        a_sub = self.a.get(f"/api/subscribers/{self.sub_a}").json()
        self.assertEqual(a_sub["first_name"], "Alice")
        self.assertEqual(a_sub["status"], "active")
        b_rows = [s for s in self.b.get("/api/subscribers").json() if s["email"] == "same@x.com"]
        self.assertEqual(len(b_rows), 1)
        self.assertEqual(b_rows[0]["first_name"], "Bob")

        # B's suppression for the address must not block or be lifted by A.
        import sqlite3
        con = sqlite3.connect(settings.DATABASE_PATH)
        owners = {r[0] for r in con.execute("SELECT user_id FROM suppressions WHERE email = 'same@x.com'")}
        con.close()
        self.assertEqual(len(owners), 1)
        self.assertEqual(self.a.put(f"/api/subscribers/{self.sub_a}", json={"status": "active"}).status_code, 200)
        con = sqlite3.connect(settings.DATABASE_PATH)
        still = con.execute("SELECT COUNT(*) FROM suppressions WHERE email = 'same@x.com'").fetchone()[0]
        con.close()
        self.assertEqual(still, 1)

    def test_templates_isolated(self):
        b = self.b
        self.assertEqual(b.get("/api/templates").json(), [])
        self.assertEqual(b.get(f"/api/templates/{self.tpl_a}").status_code, 404)
        self.assertEqual(b.put(f"/api/templates/{self.tpl_a}", json={"name": "pwn"}).status_code, 404)
        self.assertEqual(b.post(f"/api/templates/{self.tpl_a}/clone").status_code, 404)
        self.assertEqual(b.delete(f"/api/templates/{self.tpl_a}").status_code, 404)
        res = b.post("/api/bulk-delete", json={"resource": "templates", "ids": [self.tpl_a]})
        self.assertEqual(res.json()["deleted"], 0)
        prev = b.post("/api/templates/preview", json={"template_id": self.tpl_a, "body_html": "{{secret}}"}).json()
        self.assertNotIn("a-only", prev["rendered_body_html"])
        self.assertEqual(self.a.get(f"/api/templates/{self.tpl_a}").json()["name"], "A tpl")


if __name__ == "__main__":
    unittest.main()
