"""
Contacts & lists data model: server-side paging, filters, bulk actions, imports
that respect opt-outs, and the one-time merge of the legacy duplicate tables.
"""

import asyncio
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db
from app.main import app


class TestContacts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(cls.tmp.name)
        cls.db_path = data_dir / "contacts.db"
        cls.orig = (settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR)
        settings.DATABASE_PATH = cls.db_path
        settings.EML_ARCHIVE_DIR = settings.EML_STORAGE_DIR = data_dir / "eml"
        settings.EML_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

        cls._seed_legacy_tables()
        asyncio.run(init_db())

        cls.client = TestClient(app)
        res = cls.client.post("/api/auth/login", json={"login": "admin@bitmail.com", "password": "admin123"})
        assert res.status_code == 200, res.text
        cls.client.headers["Authorization"] = f"Bearer {res.json()['token']}"

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR = cls.orig
        cls.tmp.cleanup()

    @classmethod
    def _seed_legacy_tables(cls):
        """A pre-migration database: memberships and suppressions only in the old duplicate tables."""
        con = sqlite3.connect(cls.db_path)
        con.executescript("""
            CREATE TABLE subscribers (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                first_name TEXT, last_name TEXT, tags TEXT DEFAULT '[]', custom_fields TEXT DEFAULT '{}',
                status TEXT DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE subscriber_lists (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                schema_fields TEXT DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE list_subscribers (list_id TEXT NOT NULL, subscriber_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', subscribed_at TEXT NOT NULL, PRIMARY KEY (list_id, subscriber_id));
            CREATE TABLE suppression_list (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                campaign_id TEXT, reason TEXT NOT NULL DEFAULT 'user_unsubscribed', created_at TEXT NOT NULL);
            INSERT INTO subscribers VALUES ('sub_legacy', 'legacy@example.com', 'Leg', 'Acy', '[]', '{}', 'active', '2020-01-01 00:00:00', '2020-01-01 00:00:00');
            INSERT INTO subscriber_lists VALUES ('list_legacy', 'Legacy', NULL, '[]', '2020-01-01 00:00:00', '2020-01-01 00:00:00');
            INSERT INTO list_subscribers VALUES ('list_legacy', 'sub_legacy', 'active', '2020-01-01 00:00:00');
            INSERT INTO suppression_list VALUES ('sup_legacy', 'gone@example.com', NULL, 'user_unsubscribed', '2020-01-01 00:00:00');
        """)
        con.commit()
        con.close()

    def _create(self, email, **extra):
        res = self.client.post("/api/subscribers", json={"email": email, **extra})
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    def _list(self, name):
        res = self.client.post("/api/lists", json={"name": name})
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["id"]

    def test_01_legacy_tables_are_merged_and_dropped(self):
        con = sqlite3.connect(self.db_path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        membership = con.execute("SELECT 1 FROM subscriber_list_memberships WHERE subscriber_id='sub_legacy'").fetchone()
        suppressed = con.execute("SELECT 1 FROM suppressions WHERE email='gone@example.com'").fetchone()
        con.close()

        self.assertNotIn("list_subscribers", tables)
        self.assertNotIn("suppression_list", tables)
        self.assertIsNotNone(membership, "legacy list membership was lost")
        self.assertIsNotNone(suppressed, "legacy suppression was lost")

        lists = {l["id"]: l for l in self.client.get("/api/lists").json()}
        self.assertEqual(lists["list_legacy"]["subscriber_count"], 1)

    def test_02_paging_sorting_total_header(self):
        for i in range(7):
            self._create(f"page-{i}@example.com", first_name=f"P{i}")

        res = self.client.get("/api/subscribers", params={"search": "page-", "per_page": 3, "page": 3, "sort": "email", "order": "asc"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Total-Count"], "7")
        self.assertEqual([s["email"] for s in res.json()], ["page-6@example.com"])

    def test_03_search_custom_fields_and_tag_filter(self):
        self._create("cf@example.com", custom_fields={"Order ID": "ORD-7781"}, tags=["VIP", "vip", " webinar "])
        hit = self.client.get("/api/subscribers", params={"search": "ord-7781"}).json()
        self.assertEqual([s["email"] for s in hit], ["cf@example.com"])
        self.assertEqual(hit[0]["custom_fields"], {"order_id": "ORD-7781"})
        self.assertEqual(hit[0]["tags"], ["vip", "webinar"])

        by_tag = self.client.get("/api/subscribers", params={"tag": "webinar"}).json()
        self.assertEqual([s["email"] for s in by_tag], ["cf@example.com"])

        # A '%' in the search must be literal, not a wildcard that matches everyone.
        self.assertEqual(self.client.get("/api/subscribers", params={"search": "%"}).headers["X-Total-Count"], "0")

    def test_04_bulk_by_ids_and_select_all_matching(self):
        list_id = self._list("Bulk Target")
        ids = [self._create(f"bulk-{i}@example.com")["id"] for i in range(3)]

        res = self.client.post("/api/subscribers/bulk", json={"action": "add_to_list", "ids": ids, "list_id": list_id})
        self.assertEqual(res.json()["affected"], 3)
        # Adding again is idempotent.
        res = self.client.post("/api/subscribers/bulk", json={"action": "add_to_list", "ids": ids, "list_id": list_id})
        self.assertEqual(res.json()["affected"], 0)

        res = self.client.post("/api/subscribers/bulk", json={
            "action": "add_tag", "tag": "q3", "filter": {"list_id": list_id}
        })
        self.assertEqual(res.json()["affected"], 3)
        self.assertEqual(self.client.get("/api/subscribers", params={"tag": "q3"}).headers["X-Total-Count"], "3")

        res = self.client.post("/api/subscribers/bulk", json={"action": "remove_tag", "tag": "q3", "ids": ids[:1]})
        self.assertEqual(res.json()["affected"], 1)
        self.assertEqual(self.client.get(f"/api/subscribers/{ids[0]}").json()["tags"], [])

        res = self.client.post("/api/subscribers/bulk", json={"action": "remove_from_list", "ids": ids[:2], "list_id": list_id})
        self.assertEqual(res.json()["affected"], 2)
        lists = {l["id"]: l for l in self.client.get("/api/lists").json()}
        self.assertEqual(lists[list_id]["subscriber_count"], 1)

        res = self.client.post("/api/subscribers/bulk", json={"action": "delete", "filter": {"search": "bulk-"}})
        self.assertEqual(res.json()["affected"], 3)

        bad = self.client.post("/api/subscribers/bulk", json={"action": "delete", "ids": ids, "filter": {}})
        self.assertEqual(bad.status_code, 422)

    def test_05_status_change_syncs_suppressions(self):
        sub = self._create("status@example.com")
        self.client.post("/api/subscribers/bulk", json={"action": "set_status", "status": "unsubscribed", "ids": [sub["id"]]})
        con = sqlite3.connect(self.db_path)
        self.assertIsNotNone(con.execute("SELECT 1 FROM suppressions WHERE email='status@example.com'").fetchone())

        self.client.put(f"/api/subscribers/{sub['id']}", json={"status": "active"})
        self.assertIsNone(con.execute("SELECT 1 FROM suppressions WHERE email='status@example.com'").fetchone())
        con.close()

    def test_06_imports_never_resubscribe(self):
        sub = self._create("optout@example.com")
        self.client.put(f"/api/subscribers/{sub['id']}", json={"status": "unsubscribed"})

        res = self.client.post("/api/subscribers/bulk-text", json={"raw_text": "optout@example.com, fresh@example.com"})
        self.assertEqual(res.json()["inactive_count"], 1)

        csv_body = "Email,First Name,Plan\noptout@example.com,Opt,Gold\nfresh2@example.com,New,Silver\nfresh2@example.com,Dup,Silver\n"
        res = self.client.post(
            "/api/subscribers/import-csv",
            files={"file": ("c.csv", io.BytesIO(csv_body.encode()), "text/csv")},
            data={"new_list_name": "CSV Import", "tags": "imported"},
        )
        data = res.json()
        self.assertEqual((data["added_count"], data["updated_count"], data["failed_count"]), (1, 1, 1))
        self.assertEqual(data["custom_fields_detected"], ["plan"])

        after = self.client.get(f"/api/subscribers/{sub['id']}").json()
        self.assertEqual(after["status"], "unsubscribed")
        self.assertEqual(after["custom_fields"]["plan"], "Gold")
        self.assertIn("imported", after["tags"])

    def test_07_export_has_custom_field_columns(self):
        self._create("export@example.com", custom_fields={"city": "Pune"}, tags=["x"])
        res = self.client.get("/api/subscribers/export", params={"search": "export@"})
        header, row = res.text.strip().splitlines()[:2]
        self.assertIn("city", header.split(","))
        self.assertIn("Pune", row)

    def test_08_summary_and_lists_route_not_shadowed(self):
        summary = self.client.get("/api/subscribers/summary").json()
        self.assertGreaterEqual(summary["total"], 1)
        self.assertIn("by_status", summary)
        # Used to be swallowed by /api/subscribers/{subscriber_id} and 404.
        self.assertEqual(self.client.get("/api/subscribers/lists").status_code, 200)

    def test_09_duplicate_list_name_rejected(self):
        self._list("Dup Name")
        self.assertEqual(self.client.post("/api/lists", json={"name": "dup name"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
