"""
Multi-user isolation for campaigns, the sending pipeline and warmup: user B can never
see, change, launch or send through user A's campaigns, lists, templates or relays,
and background sends (scheduler) are recorded under the campaign owner.
"""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db
from app.main import app

NOW = "2026-01-01 00:00:00"


class TestCampaignIsolation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(cls.tmp.name)
        cls.orig = (settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR)
        settings.DATABASE_PATH = data_dir / "isolation_campaigns.db"
        settings.EML_ARCHIVE_DIR = settings.EML_STORAGE_DIR = data_dir / "eml"
        settings.EML_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        asyncio.run(init_db())

        admin = TestClient(app)
        res = admin.post("/api/auth/login", json={"login": "admin@bitmail.com", "password": "admin123"})
        assert res.status_code == 200, res.text
        admin.headers["Authorization"] = f"Bearer {res.json()['token']}"

        cls.ids, cls.clients = {}, {}
        for name in ("a", "b"):
            email = f"user_{name}@example.com"
            res = admin.post("/api/auth/users", json={"email": email, "password": "secret-pass-123", "role": "user"})
            assert res.status_code == 201, res.text
            cls.ids[name] = res.json()["id"]
            client = TestClient(app)
            res = client.post("/api/auth/login", json={"login": email, "password": "secret-pass-123"})
            assert res.status_code == 200, res.text
            client.headers["Authorization"] = f"Bearer {res.json()['token']}"
            cls.clients[name] = client
        cls.a, cls.b = cls.clients["a"], cls.clients["b"]

        # A's list (with one contact), template and sandbox relay, seeded directly so the
        # test does not depend on other routers.
        con = sqlite3.connect(settings.DATABASE_PATH)
        ua = cls.ids["a"]
        con.execute("INSERT INTO subscriber_lists (id, name, created_at, updated_at, user_id) VALUES ('list_a', 'A list', ?, ?, ?)", (NOW, NOW, ua))
        con.execute("INSERT INTO templates (id, name, subject, body_html, body_text, created_at, updated_at, user_id) "
                    "VALUES ('tpl_a', 'A tpl', 'Hi', '<p>Hi</p>', 'Hi', ?, ?, ?)", (NOW, NOW, ua))
        con.execute("INSERT INTO smtp_configs (id, name, host, port, use_tls, use_ssl, is_active, is_default, created_at, updated_at, user_id) "
                    "VALUES ('smtp_a', 'A relay', 'sandbox', 25, 0, 0, 1, 1, ?, ?, ?)", (NOW, NOW, ua))
        con.execute("INSERT INTO subscribers (id, email, status, created_at, updated_at, user_id) "
                    "VALUES ('sub_a', 'shared@example.com', 'active', ?, ?, ?)", (NOW, NOW, ua))
        con.execute("INSERT INTO subscriber_list_memberships (subscriber_id, list_id, added_at) VALUES ('sub_a', 'list_a', ?)", (NOW,))
        con.commit()
        con.close()

        res = cls.a.post("/api/campaigns", json={
            "name": "A campaign", "subject": "Hi", "template_id": "tpl_a",
            "list_id": "list_a", "smtp_config_id": "smtp_a",
        })
        assert res.status_code == 201, res.text
        cls.camp_a = res.json()["id"]

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR = cls.orig
        cls.tmp.cleanup()

    def _db(self, sql, params=()):
        con = sqlite3.connect(settings.DATABASE_PATH)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def test_campaigns_are_invisible_to_other_users(self):
        self.assertIn(self.camp_a, [c["id"] for c in self.a.get("/api/campaigns").json()])
        self.assertNotIn(self.camp_a, [c["id"] for c in self.b.get("/api/campaigns").json()])
        c = self.camp_a
        self.assertEqual(self.b.get(f"/api/campaigns/{c}").status_code, 404)
        self.assertEqual(self.b.get(f"/api/campaigns/{c}/stats").status_code, 404)
        self.assertEqual(self.b.put(f"/api/campaigns/{c}", json={"name": "pwned"}).status_code, 404)
        for action in ("launch", "pause", "resume", "cancel", "unschedule"):
            self.assertEqual(self.b.post(f"/api/campaigns/{c}/{action}").status_code, 404, action)
        self.assertEqual(self.b.post(f"/api/campaigns/{c}/schedule", json={"scheduled_at": "2030-01-01 00:00:00"}).status_code, 404)
        self.assertEqual(self.b.post(f"/api/campaigns/{c}/test-send", json={"test_email": "x@example.com"}).status_code, 404)
        self.assertEqual(self.b.delete(f"/api/campaigns/{c}").status_code, 404)
        row = self._db("SELECT name, status FROM campaigns WHERE id = ?", (c,))
        self.assertEqual(row, [("A campaign", "draft")])

    def test_cannot_reference_another_users_objects(self):
        for field, value in (("list_id", "list_a"), ("template_id", "tpl_a"), ("smtp_config_id", "smtp_a")):
            res = self.b.post("/api/campaigns", json={"name": "B", "subject": "x", field: value})
            self.assertEqual(res.status_code, 404, field)
        res = self.b.post("/api/campaigns", json={"name": "B own", "subject": "x"})
        self.assertEqual(res.status_code, 201, res.text)
        own = res.json()["id"]
        self.assertEqual(self.b.put(f"/api/campaigns/{own}", json={"smtp_config_id": "smtp_a"}).status_code, 404)
        self.assertEqual(self.b.put(f"/api/campaigns/{own}", json={"list_id": "list_a"}).status_code, 404)
        # B has no relay of their own: launching is refused instead of borrowing A's.
        res = self.b.post(f"/api/campaigns/{own}/launch")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Add an SMTP relay first", res.text)
        for extra in ({"smtp_config_id": "smtp_a"}, {"list_id": "list_a"}):
            res = self.b.post("/api/campaigns/quick-broadcast", json={
                "subject": "x", "body_html": "<p>x</p>", "recipients_text": "z@example.com", **extra})
            self.assertEqual(res.status_code, 404, extra)

    def test_same_email_per_user_and_no_borrowed_relay(self):
        # Scheduled, so the worker runs below in a loop the test controls.
        res = self.b.post("/api/campaigns/quick-broadcast", json={
            "subject": "B says hi", "body_html": "<p>hi</p>", "recipients_text": "shared@example.com",
            "scheduled_at": "2099-01-01 00:00:00"})
        self.assertEqual(res.status_code, 200, res.text)
        cid = res.json()["campaign_id"]
        subs = self._db("SELECT user_id FROM subscribers WHERE email = 'shared@example.com'")
        self.assertEqual(sorted(r[0] for r in subs), sorted([self.ids["a"], self.ids["b"]]))

        async def run():
            from app.queue import campaign_queue
            worker = await campaign_queue.start_campaign(cid)
            await worker.task

        asyncio.run(run())
        rows = self._db("SELECT user_id, status, error_message FROM sent_emails WHERE campaign_id = ?", (cid,))
        self.assertTrue(rows)
        for owner, status_, error in rows:
            self.assertEqual(owner, self.ids["b"])
            self.assertNotIn(status_, ("sent", "simulated"), "B must not send through A's relay")
            if status_ == "failed":
                self.assertIn("Add an SMTP relay first", error)

    def test_scheduled_campaign_runs_as_owner(self):
        res = self.a.post("/api/campaigns", json={
            "name": "A scheduled", "subject": "Hi", "template_id": "tpl_a", "list_id": "list_a",
            "smtp_config_id": "smtp_a", "scheduled_at": "2020-01-01 00:00:00"})
        self.assertEqual(res.status_code, 201, res.text)
        cid = res.json()["id"]

        async def run_scheduler():
            from app.queue import campaign_queue
            from app.scheduler import campaign_scheduler
            triggered = await campaign_scheduler.check_and_trigger_due_campaigns()
            await campaign_queue._active_workers[cid].task
            return triggered

        self.assertIn(cid, asyncio.run(run_scheduler()))
        rows = self._db("SELECT recipient_email, user_id FROM sent_emails WHERE campaign_id = ?", (cid,))
        self.assertEqual(rows, [("shared@example.com", self.ids["a"])])

    def test_warmup_schedules_and_relays_are_per_user(self):
        res = self.a.post("/api/warmup/schedules", json={
            "name": "A warmup", "strategy": "standard_14", "list_id": "list_a", "campaign_id": self.camp_a})
        self.assertEqual(res.status_code, 201, res.text)
        sid = res.json()["id"]
        child = self._db("SELECT user_id FROM campaigns WHERE warmup_schedule_id = ?", (sid,))
        self.assertTrue(child and all(r[0] == self.ids["a"] for r in child))

        self.assertEqual(self.b.get("/api/warmup/schedules").json(), [])
        self.assertEqual(self.b.get(f"/api/warmup/schedules/{sid}").status_code, 404)
        for action in ("pause", "resume"):
            self.assertEqual(self.b.post(f"/api/warmup/schedules/{sid}/{action}").status_code, 404)
        self.assertEqual(self.b.delete(f"/api/warmup/schedules/{sid}").status_code, 404)
        self.assertEqual(self.b.post("/api/warmup/schedules", json={
            "name": "B", "strategy": "standard_14", "campaign_id": self.camp_a}).status_code, 404)
        self.assertEqual(self.b.post("/api/warmup/schedules", json={
            "name": "B", "strategy": "standard_14", "list_id": "list_a"}).status_code, 404)

        self.assertIn("smtp_a", [r["smtp_config_id"] for r in self.a.get("/api/warmup/relays").json()])
        self.assertEqual(self.b.get("/api/warmup/relays").json(), [])
        self.assertEqual(self.b.post("/api/warmup/relays/smtp_a/toggle-pool", json={"in_pool": True}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
