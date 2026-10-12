"""
Tracking, unsubscribe, bounces, suppressions, webhooks, settings and live events are
private per user: an event on user A's email only touches A's data, reaches A's
webhooks and A's browsers, and user B can never see or change A's rows.
"""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.auth import set_current_user_id
from app.config import settings
from app.db import init_db
from app.main import app
from app.webhooks import WebhookDispatcher
from app.websocket import ConnectionManager, emit_event, ws_manager

NOW = "2026-10-01 10:00:00"


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(text)


class TestEventsIsolation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(cls.tmp.name)
        cls.orig = (settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR)
        settings.DATABASE_PATH = data_dir / "iso_events.db"
        settings.EML_ARCHIVE_DIR = settings.EML_STORAGE_DIR = data_dir / "eml"
        settings.EML_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        asyncio.run(init_db())

        admin = TestClient(app)
        admin.headers["Authorization"] = f"Bearer {cls._login(admin, 'admin@bitmail.com', 'admin123')}"
        cls.uid = {}
        for name in ("a", "b"):
            res = admin.post("/api/auth/users", json={"email": f"{name}@ev.test", "password": "Passw0rd!x", "role": "user"})
            assert res.status_code in (200, 201), res.text
            cls.uid[name] = res.json()["id"]
        cls.a, cls.b = TestClient(app), TestClient(app)
        cls.token_a = cls._login(cls.a, "a@ev.test", "Passw0rd!x")
        cls.a.headers["Authorization"] = f"Bearer {cls.token_a}"
        cls.b.headers["Authorization"] = f"Bearer {cls._login(cls.b, 'b@ev.test', 'Passw0rd!x')}"

        # Both users mail the same two addresses and hold them as subscribers.
        with sqlite3.connect(settings.DATABASE_PATH) as con:
            for name in ("a", "b"):
                for addr in ("same@x.com", "bounce@x.com"):
                    key = f"{name}_{addr.split('@')[0]}"
                    con.execute(
                        "INSERT INTO subscribers (id, user_id, email, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                        (f"sub_{key}", cls.uid[name], addr, NOW, NOW))
                    con.execute(
                        "INSERT INTO sent_emails (id, user_id, recipient_email, sender_email, subject, status, message_id, created_at) "
                        "VALUES (?, ?, ?, 's@ev.test', 'Hi', 'sent', ?, ?)",
                        (f"eml_{key}", cls.uid[name], addr, f"<{key}@ev.test>", NOW))
            con.commit()

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH, settings.EML_ARCHIVE_DIR, settings.EML_STORAGE_DIR = cls.orig
        cls.tmp.cleanup()

    @staticmethod
    def _login(client, login, password):
        res = client.post("/api/auth/login", json={"login": login, "password": password})
        assert res.status_code == 200, res.text
        return res.json()["token"]

    def _q(self, sql, *params):
        with sqlite3.connect(settings.DATABASE_PATH) as con:
            return con.execute(sql, params).fetchall()

    def _status(self, sub_id):
        return self._q("SELECT status FROM subscribers WHERE id = ?", sub_id)[0][0]

    def test_unsubscribe_only_touches_senders_data(self):
        res = self.a.get("/unsubscribe/eml_a_same")
        self.assertIn("same@x.com", res.text)
        self.assertEqual(self._status("sub_a_same"), "unsubscribed")
        self.assertEqual(self._status("sub_b_same"), "active")
        owners = self._q("SELECT user_id FROM suppressions WHERE email = 'same@x.com'")
        self.assertEqual(owners, [(self.uid["a"],)])

        # B sees none of it and cannot lift A's suppression.
        b_rows = self.b.get("/api/bounces/suppressions").json()["suppressions"]
        self.assertNotIn("same@x.com", [r["email"] for r in b_rows])
        self.b.delete("/api/bounces/suppressions/same@x.com")
        self.b.post("/api/bounces/suppressions/sync")  # B's subscriber is active; must not clear A's row
        self.assertEqual(len(self._q("SELECT 1 FROM suppressions WHERE email = 'same@x.com'")), 1)
        self.assertIn("same@x.com", [r["email"] for r in self.a.get("/api/bounces/suppressions").json()["suppressions"]])

        # Same address can be suppressed for B too: rows coexist per user.
        self.b.get("/unsubscribe/eml_b_same")
        self.assertEqual(len(self._q("SELECT 1 FROM suppressions WHERE email = 'same@x.com'")), 2)

        # Resubscribing via A's link restores only A.
        self.a.post("/resubscribe/eml_a_same?format=json")
        self.assertEqual(self._status("sub_a_same"), "active")
        self.assertEqual(self._status("sub_b_same"), "unsubscribed")

    def test_bounce_hits_owner_of_the_bounced_email(self):
        res = self.b.post("/api/bounces/inbound", json={
            "recipient_email": "bounce@x.com", "bounce_type": "hard", "status_code": "5.1.1",
            "message_id": "<b_bounce@ev.test>",
        })
        self.assertTrue(res.json()["suppressed"], res.text)
        self.assertEqual(self._status("sub_b_bounce"), "bounced")
        self.assertEqual(self._status("sub_a_bounce"), "active")
        self.assertEqual(self._q("SELECT user_id FROM suppressions WHERE email = 'bounce@x.com'"), [(self.uid["b"],)])

        # An address nobody mailed suppresses nothing anywhere.
        res = self.a.post("/api/bounces/inbound", json={"recipient_email": "stranger@x.com", "bounce_type": "hard"})
        self.assertFalse(res.json()["suppressed"])
        self.assertEqual(self._q("SELECT 1 FROM suppressions WHERE email = 'stranger@x.com'"), [])

    def test_bounce_reports_need_secret_or_own_mail(self):
        anon = TestClient(app)
        report = {"recipient_email": "bounce@x.com", "bounce_type": "hard", "message_id": "<a_bounce@ev.test>"}
        self.assertEqual(anon.post("/api/bounces/inbound", json=report).status_code, 401)
        self.assertEqual(anon.post("/api/bounces/inbound-raw", content=b"x").status_code, 401)
        with patch.object(settings, "BOUNCE_WEBHOOK_SECRET", "s3cret"):
            self.assertEqual(anon.post("/api/bounces/inbound", json=report, headers={"X-Bounce-Secret": "wrong"}).status_code, 401)
            soft = {**report, "bounce_type": "soft"}
            self.assertEqual(anon.post("/api/bounces/inbound", json=soft, headers={"X-Bounce-Secret": "s3cret"}).status_code, 200)
            self.assertEqual(anon.post("/api/bounces/inbound?secret=s3cret", json=soft).status_code, 200)

        # Signed in, B cannot bounce mail that A sent.
        res = self.b.post("/api/bounces/inbound", json=report)
        self.assertFalse(res.json()["suppressed"], res.text)
        self.assertEqual(self._status("sub_a_bounce"), "active")

    def test_webhooks_private_and_dispatched_to_owner_only(self):
        wh = self.a.post("/api/webhooks", json={"name": "A hook", "url": "https://a.example/hook", "events": ["*"]}).json()
        self.b.post("/api/webhooks", json={"name": "B hook", "url": "https://b.example/hook", "events": ["*"]})
        self.assertEqual([w["id"] for w in self.a.get("/api/webhooks").json()], [wh["id"]])
        self.assertNotIn(wh["id"], [w["id"] for w in self.b.get("/api/webhooks").json()])
        self.assertEqual(self.b.get(f"/api/webhooks/{wh['id']}").status_code, 404)
        self.assertEqual(self.b.put(f"/api/webhooks/{wh['id']}", json={"name": "pwned"}).status_code, 404)
        self.assertEqual(self.b.post(f"/api/webhooks/{wh['id']}/test").status_code, 404)
        self.assertEqual(self.b.delete(f"/api/webhooks/{wh['id']}").status_code, 404)
        self.assertEqual(self.a.get(f"/api/webhooks/{wh['id']}").json()["webhook"]["name"], "A hook")

        async def _dispatch(user_id):
            set_current_user_id(user_id)
            ok = httpx.Response(status_code=200, text="ok")
            with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=ok)) as post:
                res = await WebhookDispatcher.dispatch_event("email.opened", {"x": 1}, background=False)
                return res, [c.args[0] for c in post.call_args_list]

        res, urls = asyncio.run(_dispatch(self.uid["a"]))
        self.assertEqual(urls, ["https://a.example/hook"])
        self.assertEqual([r["webhook_id"] for r in res], [wh["id"]])
        self.assertEqual(asyncio.run(_dispatch(None)), ([], []))

    def test_settings_per_user(self):
        self.a.put("/api/settings", json={"default_format": "text"})
        self.assertEqual(self.a.get("/api/settings").json()["settings"]["default_format"], "text")
        self.assertEqual(self.b.get("/api/settings").json()["settings"]["default_format"], "html")
        self.b.put("/api/settings", json={"default_format": "html", "track_opens": False})
        self.assertEqual(self.a.get("/api/settings").json()["settings"]["default_format"], "text")
        self.assertTrue(self.a.get("/api/settings").json()["settings"]["track_opens"])

    def test_live_events_reach_owner_only(self):
        mgr, sock_a, sock_b = ConnectionManager(), _FakeSocket(), _FakeSocket()

        async def _run():
            await mgr.connect(sock_a, self.uid["a"])
            await mgr.connect(sock_b, self.uid["b"])
            with patch("app.websocket.ws_manager", mgr):
                set_current_user_id(self.uid["a"])
                await emit_event("email_opened", {"n": 1})
                set_current_user_id(None)
                await emit_event("email_opened", {"n": 2})

        asyncio.run(_run())
        self.assertEqual(len(sock_a.sent), 1)
        self.assertEqual(sock_b.sent, [])

        # The real endpoint tags each connection with its signed-in user.
        with self.a.websocket_connect(f"/ws/live?token={self.token_a}") as ws:
            self.assertEqual(ws.receive_json()["type"], "connection_established")
            self.assertIn(self.uid["a"], ws_manager.active_connections.values())


if __name__ == "__main__":
    unittest.main()
