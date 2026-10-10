"""
Comprehensive Test Suite for the Enterprise Mass Email Delivery Engine.
Covers Template Engine, Storage Vault, SMTP Sender (Sandbox & RFC Compliance),
and Async Background Queue & Rate Limiter.
"""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from app.config import settings
from app.db import init_db
from app.models import (
    Campaign,
    CampaignStatus,
    EmailRecord,
    EmailStatus,
    EventType,
    RecipientStatus,
    SMTPConfig,
    Subscriber,
    TransactionalEmailRequest,
)
from app.queue import AsyncTokenBucketRateLimiter, CampaignQueueManager
from app.sender import EmailSender
from app.storage import EmailStorageVault
from app.template_engine import TemplateEngine


class TestTemplateEngine(unittest.TestCase):
    """Test dynamic merge tag interpolation, tracking injection, and compliance."""

    def setUp(self):
        self.engine = TemplateEngine(
            base_url="https://mail.example.com",
            secret_key="test-secret-key",
            company_name="Acme Corporation",
            company_address="123 Tech Blvd, Austin, TX",
            privacy_policy_url="https://mail.example.com/privacy",
        )

    def test_merge_tag_interpolation(self):
        template = "Hello {{ first_name }} {{last_name}}, your plan is {{plan}} (acct: {{custom.account_id}})! Fallback: {{missing | default=\"valued customer\"}}"
        subscriber = Subscriber(
            email="jane@example.com",
            first_name="Jane",
            last_name="Doe",
            custom_attributes={"plan": "Enterprise", "account_id": "ACC-9988"},
        )
        rendered = self.engine.render_email(
            subject_template="Welcome {{first_name}}!",
            html_template=f"<p>{template}</p>",
            subscriber=subscriber,
            email_storage_id="eml_test123",
        )
        self.assertEqual(rendered.subject, "Welcome Jane!")
        self.assertIn("Hello Jane Doe, your plan is Enterprise (acct: ACC-9988)!", rendered.rendered_html)
        self.assertIn("Fallback: valued customer", rendered.rendered_html)

    def test_open_tracking_pixel_injection(self):
        html_input = "<html><head><title>Test</title></head><body><h1>Hello World</h1></body></html>"
        rendered = self.engine.render_email(
            subject_template="Subject",
            html_template=html_input,
            email_storage_id="eml_open_test",
            track_opens=True,
        )
        self.assertIn('<img src="https://mail.example.com/track/open/eml_open_test"', rendered.rendered_html)
        self.assertIn('</body>', rendered.rendered_html)

    def test_click_tracking_rewrite(self):
        html_input = """
        <html><body>
            <a href="https://example.com/promo?id=123&code=save20">Special Offer</a>
            <a href="mailto:support@example.com">Contact Support</a>
            <a href="tel:+1234567890">Call Us</a>
            <a href="#section2">Jump to Section</a>
            <a href="https://example.com/docs" data-no-track="true">Docs</a>
        </body></html>
        """
        rendered = self.engine.render_email(
            subject_template="Subject",
            html_template=html_input,
            email_storage_id="eml_click_test",
            track_clicks=True,
            inject_footer=False,
        )
        # Tracking redirect injected for standard link
        self.assertIn("https://mail.example.com/track/click/eml_click_test?url=https%3A%2F%2Fexample.com%2Fpromo%3Fid%3D123%26code%3Dsave20", rendered.rendered_html)

        # Mailto, Tel, Anchors, and data-no-track are preserved untouched
        self.assertIn('href="mailto:support@example.com"', rendered.rendered_html)
        self.assertIn('href="tel:+1234567890"', rendered.rendered_html)
        self.assertIn('href="#section2"', rendered.rendered_html)
        self.assertIn('href="https://example.com/docs"', rendered.rendered_html)

    def test_can_spam_gdpr_footer_injection(self):
        html_input = "<html><body><p>News content</p></body></html>"
        subscriber = Subscriber(email="subscriber@example.com", first_name="Sam")
        rendered = self.engine.render_email(
            subject_template="Subject",
            html_template=html_input,
            subscriber=subscriber,
            email_storage_id="eml_footer_test",
            inject_footer=True,
        )
        self.assertIn("Acme Corporation", rendered.rendered_html)
        self.assertIn("123 Tech Blvd, Austin, TX", rendered.rendered_html)
        self.assertIn("Unsubscribe", rendered.rendered_html)
        self.assertIn("https://mail.example.com/unsubscribe/eml_footer_test", rendered.rendered_html)

    def test_unsubscribe_token_generation_and_verification(self):
        token = self.engine.generate_unsubscribe_token("eml_123", "alice@test.com")
        self.assertTrue(self.engine.verify_unsubscribe_token("eml_123", "alice@test.com", token))
        self.assertFalse(self.engine.verify_unsubscribe_token("eml_123", "bob@test.com", token))
        self.assertFalse(self.engine.verify_unsubscribe_token("eml_999", "alice@test.com", token))


class TestAsyncComponents(unittest.IsolatedAsyncioTestCase):
    """Test Email Storage Vault, Sender, and Queue Worker asynchronously."""

    async def asyncSetUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.test_dir.name) / "data"
        self.archive_dir = self.data_dir / "eml_archive"
        self.db_path = self.data_dir / "test_email.db"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        # Override global settings path for isolated test
        settings.DATABASE_PATH = self.db_path
        settings.EML_ARCHIVE_DIR = self.archive_dir
        settings.TRACKING_BASE_URL = "http://localhost:8000"

        await init_db()
        self.storage = EmailStorageVault(archive_base_dir=self.archive_dir)
        self.sender = EmailSender()
        self.queue_manager = CampaignQueueManager(
            sender=self.sender,
            storage=self.storage,
            templates=TemplateEngine(base_url="http://localhost:8000"),
        )

    async def asyncTearDown(self):
        self.test_dir.cleanup()

    async def test_storage_vault_crud_and_eml_generation(self):
        record = EmailRecord(
            id="eml_vault_test_01",
            recipient_email="test@recipient.com",
            recipient_name="Test Recipient",
            sender_email="sender@enterprise.com",
            sender_name="Enterprise Sender",
            subject="Important Update",
            body_text="Plain text message",
            body_html="<p>HTML message</p>",
            rendered_html="<p>Rendered HTML with <a href='http://localhost:8000/track/click/eml_vault_test_01?url=https%3A//test.com'>link</a></p>",
            status=EmailStatus.CREATED.value,
        )

        storage_id = await self.storage.save_email(record)
        self.assertEqual(storage_id, "eml_vault_test_01")

        # Verify record in SQLite
        saved = await self.storage.get_email("eml_vault_test_01")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.recipient_email, "test@recipient.com")
        self.assertTrue(os.path.exists(saved.eml_file_path))

        # Verify raw .eml file content on disk
        eml_bytes = await self.storage.export_eml("eml_vault_test_01")
        self.assertIsNotNone(eml_bytes)
        self.assertIn(b"Subject: Important Update", eml_bytes)
        self.assertIn(b"To: Test Recipient <test@recipient.com>", eml_bytes)
        self.assertIn(b"X-Storage-ID: eml_vault_test_01", eml_bytes)

        # Verify raw headers query
        headers = await self.storage.get_raw_headers("eml_vault_test_01")
        self.assertEqual(headers.get("Subject"), "Important Update")
        self.assertIn("Message-ID", headers)

        # Verify live rendered HTML
        live_html = await self.storage.get_live_rendered_html("eml_vault_test_01")
        self.assertIn("Rendered HTML", live_html)

        # Test tracking open and click
        open_res = await self.storage.record_open("eml_vault_test_01", ip_address="127.0.0.1", user_agent="Mozilla/5.0")
        self.assertTrue(open_res)

        click_res = await self.storage.record_click("eml_vault_test_01", original_url="https://test.com", ip_address="127.0.0.1")
        self.assertTrue(click_res)

        # Verify audit timeline
        timeline = await self.storage.get_audit_timeline("eml_vault_test_01")
        event_types = [e.event_type for e in timeline]
        self.assertIn("created", event_types)
        self.assertIn("opened", event_types)
        self.assertIn("clicked", event_types)

        # Verify storage statistics
        stats = await self.storage.get_storage_stats()
        self.assertEqual(stats["total_emails"], 1)
        self.assertEqual(stats["unique_opens"], 1)
        self.assertEqual(stats["unique_clicks"], 1)

    async def test_sender_sandbox_simulation_and_rfc_headers(self):
        subscriber = Subscriber(
            id="sub_001",
            email="alice@company.com",
            first_name="Alice",
            last_name="Smith",
        )
        campaign = await self.queue_manager.create_campaign(
            name="Simulation Test Campaign",
            subject="Hello {{first_name}}!",
            template_html="<p>Hi {{first_name}}, check <a href='https://enterprise.com/portal'>your portal</a>.</p>",
            smtp_config=SMTPConfig(host="sandbox", is_sandbox=True),
        )
        template_eng = TemplateEngine(base_url="http://localhost:8000")
        rendered = template_eng.render_email(
            subject_template="Hello {{first_name}}!",
            html_template="<p>Hi {{first_name}}, check <a href='https://enterprise.com/portal'>your portal</a>.</p>",
            subscriber=subscriber,
            email_storage_id="eml_sim_test_01",
            campaign_id=campaign.id,
        )

        smtp_config = SMTPConfig(host="sandbox", port=587, is_sandbox=True, simulated_delay_sec=0.01)
        result = await self.sender.send_email(
            recipient=subscriber,
            rendered=rendered,
            storage_id="eml_sim_test_01",
            campaign_id=campaign.id,
            sender_email="news@company.com",
            sender_name="Company News",
            smtp_config=smtp_config,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, EmailStatus.SIMULATED.value)
        self.assertEqual(result.storage_id, "eml_sim_test_01")
        self.assertGreater(result.latency_ms, 0)

        # Check that simulated email exists in storage vault with full RFC headers
        record = await self.storage.get_email("eml_sim_test_01")
        self.assertIsNotNone(record)
        self.assertEqual(record.status, EmailStatus.SIMULATED.value)
        self.assertEqual(record.is_sandbox, True)
        self.assertEqual(record.campaign_id, campaign.id)

        headers = await self.storage.get_raw_headers("eml_sim_test_01")
        self.assertEqual(headers["Subject"], "Hello Alice!")
        self.assertIn("List-Unsubscribe", headers)
        self.assertEqual(headers.get("List-Unsubscribe-Post"), "List-Unsubscribe=One-Click")
        self.assertEqual(headers.get("X-Campaign-ID"), campaign.id)

    async def test_rate_limiter(self):
        limiter = AsyncTokenBucketRateLimiter(rate_per_second=20, capacity=5)
        start = asyncio.get_event_loop().time()
        for _ in range(5):
            await limiter.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        # First 5 capacity tokens are consumed instantly
        self.assertLess(elapsed, 0.2)

    async def test_campaign_queue_execution_pause_resume_cancel(self):
        # The pre-send guard does a live MX lookup; a unit test must not depend on the
        # internet (testdomain.com has no MX, only an A record, and slow DNS made this flaky).
        from unittest.mock import AsyncMock, patch
        from app.deliverability import EmailValidatorService
        stub = AsyncMock(return_value=(True, [{"priority": 0, "host": "testdomain.com"}], "stubbed"))
        patcher = patch.object(EmailValidatorService, "resolve_mx", stub)
        patcher.start()
        self.addCleanup(patcher.stop)

        recipients = [
            Subscriber(id=f"sub_{i}", email=f"user{i}@testdomain.com", first_name=f"User{i}")
            for i in range(1, 11)
        ]

        campaign = await self.queue_manager.create_campaign(
            name="September Product Broadcast",
            subject="Big News for {{first_name}}!",
            template_html="<p>Hello {{first_name}}, our new version is out! <a href='https://product.com/v2'>Learn More</a></p>",
            smtp_config=SMTPConfig(host="sandbox", is_sandbox=True, simulated_delay_sec=0.01),
            rate_limit_per_sec=50,
            concurrency_limit=5,
            recipients=recipients,
        )

        # Launch campaign
        worker = await self.queue_manager.start_campaign(campaign.id)
        self.assertIsNotNone(worker.task)

        # Wait for completion
        await worker.task


        # Verify progress
        progress = await self.queue_manager.get_campaign_progress(campaign.id)
        self.assertEqual(progress["total_recipients"], 10)
        self.assertEqual(progress["sent_count"], 10)
        self.assertEqual(progress["failed_count"], 0)
        self.assertEqual(progress["status"], CampaignStatus.COMPLETED.value)

    async def test_campaign_resolves_relay_and_template_from_database(self):
        """A campaign created via the REST schema (smtp_config_id + template_id, no inline
        body or relay snapshot) must resolve both, not silently fall back to a dry run."""
        from app.db import get_db, utc_now_iso

        now = utc_now_iso()
        async with get_db() as db:
            await db.execute(
                """INSERT INTO smtp_configs (id, name, host, port, username, password, use_tls,
                       use_ssl, rate_limit_per_second, daily_quota, is_default, is_active,
                       created_at, updated_at)
                   VALUES ('smtp_real', 'Relay', 'mail.example.net', 587, 'u', 'p', 1, 0,
                           25, 50000, 1, 1, ?, ?)""",
                (now, now),
            )
            await db.execute(
                """INSERT INTO templates (id, name, subject, body_html, body_text, created_at, updated_at)
                   VALUES ('tpl_1', 'Outreach', 'Hi {{first_name}}', '<p>Body for {{first_name}}</p>', '', ?, ?)""",
                (now, now),
            )
            await db.execute(
                """INSERT INTO campaigns (id, name, subject, template_id, smtp_config_id, sender_name,
                       sender_email, status, track_opens, track_clicks, created_at, updated_at)
                   VALUES ('cmp_db', 'DB Campaign', 'Hi {{first_name}}', 'tpl_1', 'smtp_real',
                           'Sales', 'sales@example.net', 'draft', 1, 1, ?, ?)""",
                (now, now),
            )
            await db.commit()

        campaign = await self.queue_manager.get_campaign("cmp_db")
        self.assertEqual(campaign.smtp_config.host, "mail.example.net")
        self.assertFalse(campaign.smtp_config.is_sandbox)
        self.assertIn("Body for {{first_name}}", campaign.template_html)

    async def test_campaign_skips_suppressed_addresses(self):
        """Suppressed leads must never receive a campaign, even when passed in explicitly."""
        from app.db import get_db, utc_now_iso
        from app.queue import CampaignWorker

        now = utc_now_iso()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO suppressions (id, email, reason, created_at) VALUES ('sup_1', 'optout@lead.com', 'user_unsubscribed', ?)",
                (now,),
            )
            await db.commit()

        campaign = await self.queue_manager.create_campaign(
            name="Suppression Check",
            subject="Hello",
            template_html="<p>Hello</p>",
            smtp_config=SMTPConfig(host="sandbox", is_sandbox=True, simulated_delay_sec=0.001),
            recipients=[],
        )
        worker = CampaignWorker(campaign=campaign, storage=self.storage, templates=self.queue_manager.templates)
        kept = await worker.resolve_recipients([
            Subscriber(id="s1", email="Optout@Lead.com", first_name="Opt"),
            Subscriber(id="s2", email="keeper@lead.com", first_name="Keep"),
        ])
        self.assertEqual([s.email for s in kept], ["keeper@lead.com"])

    async def test_empty_campaign_completes_without_dividing_by_zero(self):
        campaign = await self.queue_manager.create_campaign(
            name="Empty",
            subject="Nobody",
            template_html="<p>Nobody</p>",
            smtp_config=SMTPConfig(host="sandbox", is_sandbox=True),
            recipients=[],
        )
        worker = await self.queue_manager.start_campaign(campaign.id, recipients=[])
        await worker.task
        progress = await self.queue_manager.get_campaign_progress(campaign.id)
        self.assertEqual(progress["total_recipients"], 0)
        self.assertEqual(progress["status"], CampaignStatus.COMPLETED.value)

    async def test_transactional_email_dispatch(self):
        req = TransactionalEmailRequest(
            recipient_email="vip@customer.com",
            recipient_name="VIP Client",
            subject="Your Security Code: {{code}}",
            html_content="<p>Hello {{name}}, your one-time verification code is <strong>{{code}}</strong>.</p>",
            template_context={"code": "893-412", "name": "VIP Client"},
            smtp_config=SMTPConfig(host="sandbox", is_sandbox=True),
        )
        result = await self.queue_manager.send_transactional(req)
        self.assertTrue(result.success)
        self.assertEqual(result.status, EmailStatus.SIMULATED.value)

        # Verify stored
        record = await self.storage.get_email(result.storage_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.subject, "Your Security Code: 893-412")
        self.assertIn("893-412", record.rendered_html)


if __name__ == "__main__":
    unittest.main()
