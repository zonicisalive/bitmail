"""
Automated Security Remediation Verification Suite.
Validates the defensive controls for all 8 confirmed security findings:
- SEC-AUTH-001 (Scan Approval Auth & Backdoor Removal)
- SEC-LEAK-001 (WebSocket Live Stream Auth & Scan Token Leak)
- SEC-INJ-001 (Template Engine Jinja2 Sandboxing)
- SEC-LEAK-002 (Unauthenticated Initial Page Pre-rendering Boundary)
- SEC-XSS-001 (Storage Vault Email Preview CSP Hardening)
- SEC-REDIR-001 (Click Tracking Open Redirect Prevention)
- SEC-CRYPTO-001 (At-Rest Fernet SMTP Credential Encryption)
- SEC-CONFIG-001 (Production Secret Key & Password Guard)
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.auth import decrypt_credential, encrypt_credential, hash_password
from app.config import Settings, settings
from app.db import get_db, init_db, utc_now_iso
from app.main import app
from app.routes.pages import get_initial_page_context
from app.sender import interpolate_template


class TestSecurityRemediation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orig_db = settings.DATABASE_PATH
        cls.orig_data = settings.DATA_DIR
        cls.orig_storage = settings.STORAGE_DIR
        cls.orig_eml = settings.EML_STORAGE_DIR

        cls.test_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.test_dir.name) / "test_security.db"
        settings.DATABASE_PATH = cls.db_path
        settings.DATA_DIR = Path(cls.test_dir.name)
        settings.STORAGE_DIR = Path(cls.test_dir.name) / "storage"
        settings.EML_STORAGE_DIR = Path(cls.test_dir.name) / "storage" / "eml"
        settings.ensure_directories()
        asyncio.run(init_db())

        cls.client = TestClient(app)
        # Login admin to obtain test token
        res = cls.client.post("/api/auth/login", json={
            "login": "admin@bitmail.com",
            "password": "admin123"
        })
        cls.auth_token = res.json()["token"]
        cls.auth_headers = {"Authorization": f"Bearer {cls.auth_token}"}

    @classmethod
    def tearDownClass(cls):
        settings.DATABASE_PATH = cls.orig_db
        settings.DATA_DIR = cls.orig_data
        settings.STORAGE_DIR = cls.orig_storage
        settings.EML_STORAGE_DIR = cls.orig_eml
        cls.test_dir.cleanup()

    def test_01_sec_auth_001_scan_approval_and_backdoor(self):
        """SEC-AUTH-001: Approve requires auth and backdoor route is 404."""
        unauth_client = TestClient(app)
        # Create scan session
        res = unauth_client.post("/api/auth/scan/session")
        self.assertEqual(res.status_code, 200)
        session_id = res.json()["session_id"]
        token = res.json()["token"]

        # Backdoor simulate-approval must be 404
        backdoor_res = unauth_client.post(f"/api/auth/scan/simulate-approval/{session_id}")
        self.assertEqual(backdoor_res.status_code, 404)

        # Unauthenticated approve must be 401
        unauth_approve = unauth_client.post("/api/auth/scan/approve", json={"token": token})
        self.assertEqual(unauth_approve.status_code, 401)

        # Authenticated approve succeeds
        auth_approve = self.client.post(
            "/api/auth/scan/approve",
            json={"token": token},
            headers=self.auth_headers
        )
        self.assertEqual(auth_approve.status_code, 200)

    def test_02_sec_leak_001_websocket_auth_and_token_leak(self):
        """SEC-LEAK-001: Live WebSocket endpoint rejects unauthenticated connections."""
        unauth_client = TestClient(app)
        with self.assertRaises(Exception):
            with unauth_client.websocket_connect("/ws/live") as ws:
                ws.receive_json()

        # Query status without secret scan token must not expose auth_token
        res = self.client.post("/api/auth/scan/session")
        session_id = res.json()["session_id"]
        token = res.json()["token"]

        # Approve it
        self.client.post("/api/auth/scan/approve", json={"token": token}, headers=self.auth_headers)

        status_without_token = self.client.get(f"/api/auth/scan/session/{session_id}/status")
        self.assertEqual(status_without_token.status_code, 200)
        self.assertIsNone(status_without_token.json()["auth_token"])

        status_with_token = self.client.get(f"/api/auth/scan/session/{session_id}/status?token={token}")
        self.assertEqual(status_with_token.status_code, 200)
        self.assertIsNotNone(status_with_token.json()["auth_token"])

    def test_03_sec_inj_001_ssti_sandboxed(self):
        """SEC-INJ-001: Jinja2 SandboxedEnvironment suppresses SSTI host execution."""
        ssti_payload = "{{ cycler.__init__.__globals__.os.popen('echo PWNED').read() }}"
        result = interpolate_template(ssti_payload, {"email": "victim@domain.com"})
        self.assertNotIn("PWNED", result)

    def test_04_sec_leak_002_page_context_auth_boundary(self):
        """SEC-LEAK-002: Unauthenticated initial page context does not pre-render sensitive records."""
        async def run_context_test():
            req = MagicMock()
            req.headers = {}
            req.cookies = {}
            req.query_params = {}
            ctx = await get_initial_page_context(req, "dashboard")
            return ctx

        ctx = asyncio.run(run_context_test())
        self.assertFalse(ctx["is_authenticated"])
        self.assertEqual(len(ctx["recent_vault_emails"]), 0)
        self.assertEqual(len(ctx["recent_campaigns"]), 0)
        self.assertEqual(len(ctx["smtp_configs"]), 0)

    def test_05_sec_xss_001_storage_preview_csp(self):
        """SEC-XSS-001: Storage email rendered view strictly specifies default-src 'none'."""
        # Create dummy sent email
        async def insert_email():
            now = utc_now_iso()
            async with get_db() as db:
                await db.execute("""
                    INSERT OR REPLACE INTO sent_emails (
                        id, recipient_email, recipient_name, sender_email, sender_name, subject,
                        body_html, body_text, status, created_at, sent_at
                    ) VALUES ('test_sec_eml_1', 'user@example.com', 'Test User', 'sender@example.com', 'Sender',
                              'Hello', '<h1>Hello</h1><script>alert(1)</script>', 'Hello', 'sent', ?, ?)
                """, (now, now))
                await db.commit()

        asyncio.run(insert_email())

        res = self.client.get("/api/storage/emails/test_sec_eml_1/rendered", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        csp = res.headers.get("content-security-policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("'unsafe-eval'", csp)

    def test_06_sec_redir_001_click_tracking_open_redirect(self):
        """SEC-REDIR-001: Open redirect to untrusted third-party domain is blocked."""
        # Attempt open redirect on non-existent email -> 404
        bad_req = self.client.get("/track/click/non_existent_id?url=https://malicious-phishing.com", follow_redirects=False)
        self.assertEqual(bad_req.status_code, 404)

        # Attempt open redirect on existing email with unlisted external URL -> 400
        untrusted_req = self.client.get("/track/click/test_sec_eml_1?url=https://attacker-domain.org/steal", follow_redirects=False)
        self.assertEqual(untrusted_req.status_code, 400)
        self.assertIn("Untrusted", untrusted_req.json().get("detail", ""))

    def test_07_sec_crypto_001_credential_fernet_encryption(self):
        """SEC-CRYPTO-001: SMTP passwords are encrypted with Fernet at rest."""
        secret_pass = "MySuperSecretSmtpPass!@#2026"
        encrypted = encrypt_credential(secret_pass)
        self.assertNotEqual(encrypted, secret_pass)
        self.assertTrue(encrypted.startswith("gAAAAA"))
        decrypted = decrypt_credential(encrypted)
        self.assertEqual(decrypted, secret_pass)

        # Test creating SMTP config via API
        res = self.client.post("/api/smtp", json={
            "name": "SecTest Relay",
            "host": "smtp.example.com",
            "port": 587,
            "username": "relay_user",
            "password": secret_pass,
            "use_tls": True
        }, headers=self.auth_headers)
        self.assertEqual(res.status_code, 201)
        smtp_id = res.json()["id"]

        # Check raw database value
        async def get_raw_pass():
            async with get_db() as db:
                async with db.execute("SELECT password FROM smtp_configs WHERE id = ?", (smtp_id,)) as cur:
                    row = await cur.fetchone()
                    return row[0] if row else None

        db_pass = asyncio.run(get_raw_pass())
        self.assertNotEqual(db_pass, secret_pass)
        self.assertTrue(db_pass.startswith("gAAAAA"))
        self.assertEqual(decrypt_credential(db_pass), secret_pass)

    def test_08_sec_config_001_production_secret_guard(self):
        """SEC-CONFIG-001: Settings blocks default SECRET_KEY in production mode."""
        prod_settings = Settings(
            APP_ENV="production",
            SECRET_KEY="bitmail-vault-secret-key-production-2026",
            DEFAULT_ADMIN_PASSWORD="admin123"
        )
        with self.assertRaises(RuntimeError):
            prod_settings.validate_security_config()


if __name__ == "__main__":
    unittest.main()
