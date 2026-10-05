"""
Automated Unit and Integration Test Suite for Deliverability & DNS Authenticator.
Tests:
1. RFC 5322 Syntax validation
2. Disposable / burner domain detection
3. Async DNS MX resolution & caching
4. Single & batch pre-send validation
5. SPF parsing & multiple record error detection (RFC 7208)
6. DMARC policy evaluation & apex fallback (RFC 7489)
7. DKIM selector checking
8. Health score & grade calculations (0-100, A+ to F)
9. Copyable recommended DNS record generator
10. FastAPI Deliverability REST API endpoints and page route
"""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db
from app.deliverability import (
    DnsAuthenticatorService,
    EmailValidatorService,
    _dns_cache,
)
from app.main import app


class TestDeliverabilityEngine(unittest.IsolatedAsyncioTestCase):
    """Unit tests for deliverability core engine logic."""

    def setUp(self):
        _dns_cache.clear()

    # 1. Syntax Validation
    def test_syntax_validation_valid_cases(self):
        valid_emails = [
            "support@bitnade.com",
            "john.doe@company.org",
            "user+newsletters@service.io",
            "first_last@tech.co.uk",
            "marketing-team@enterprise.dev",
        ]
        for email in valid_emails:
            ok, err = EmailValidatorService.validate_syntax(email)
            self.assertTrue(ok, f"Expected {email} to be valid, got error: {err}")
            self.assertIsNone(err)

    def test_syntax_validation_invalid_cases(self):
        invalid_emails = [
            "",
            "   ",
            "notanemail",
            "user@",
            "@domain.com",
            "user@@domain.com",
            "user@domain",
            "user@domain.c",  # TLD < 2 chars
            "user@domain.123",  # Numeric TLD
            "user name@domain.com",
        ]
        for email in invalid_emails:
            ok, err = EmailValidatorService.validate_syntax(email)
            self.assertFalse(ok, f"Expected {email} to be invalid")
            self.assertIsNotNone(err)

    # 2. Disposable / Burner Domain Detection
    def test_disposable_domain_detection(self):
        burners = [
            "tempmail.com",
            "mailinator.com",
            "10minutemail.com",
            "yopmail.com",
            "guerrillamail.com",
            "sub.tempmail.com",
            "user.box.mailinator.com",
        ]
        for domain in burners:
            self.assertTrue(
                EmailValidatorService.is_disposable(domain),
                f"Domain '{domain}' should be flagged as disposable"
            )

    def test_legitimate_domains_not_disposable(self):
        legit = [
            "gmail.com",
            "googlemail.com",
            "bitnade.com",
            "yahoo.com",
            "outlook.com",
            "microsoft.com",
            "apple.com",
            "amazon.com",
        ]
        for domain in legit:
            self.assertFalse(
                EmailValidatorService.is_disposable(domain),
                f"Domain '{domain}' should NOT be flagged as disposable"
            )

    # 3. MX Resolution & Caching
    async def test_mx_resolution_caching(self):
        domain = "bitmail-test-cache-domain.org"
        mock_records = [{"priority": 10, "host": "mail.bitmail-test-cache-domain.org"}]
        
        # Inject into cache directly
        _dns_cache.set(f"mx:{domain}", (True, mock_records, "Cached result"))
        
        has_mx, records, reason = await EmailValidatorService.resolve_mx(domain)
        self.assertTrue(has_mx)
        self.assertEqual(records, mock_records)
        self.assertEqual(reason, "Cached result")

    # 4. Single Email Validation
    async def test_validate_email_disposable(self):
        with patch.object(EmailValidatorService, "resolve_mx", new_callable=AsyncMock) as mock_mx:
            mock_mx.return_value = (True, [{"priority": 10, "host": "mail.mailinator.com"}], "Active MX")
            res = await EmailValidatorService.validate_email("burner@mailinator.com")
            
            self.assertEqual(res["status"], "risky")
            self.assertTrue(res["is_disposable"])
            self.assertTrue(res["syntax_valid"])
            self.assertTrue(res["has_mx"])
            self.assertTrue(any("disposable" in r.lower() for r in res["reasons"]))

    async def test_validate_email_invalid_syntax(self):
        res = await EmailValidatorService.validate_email("bad-email@@domain")
        self.assertEqual(res["status"], "invalid")
        self.assertFalse(res["syntax_valid"])
        self.assertFalse(res["is_valid"])

    async def test_validate_email_clean_valid(self):
        with patch.object(EmailValidatorService, "resolve_mx", new_callable=AsyncMock) as mock_mx:
            mock_mx.return_value = (True, [{"priority": 10, "host": "smtp.company.com"}], "Found 1 MX")
            res = await EmailValidatorService.validate_email("alice@company.com")
            
            self.assertEqual(res["status"], "valid")
            self.assertTrue(res["syntax_valid"])
            self.assertFalse(res["is_disposable"])
            self.assertTrue(res["has_mx"])
            self.assertEqual(len(res["mx_records"]), 1)

    # 5. Batch Email Validation
    async def test_validate_batch_mixed_list(self):
        emails = [
            "clean1@company.com",
            '"Sarah" <clean2@company.com>',
            "burner@mailinator.com",
            "broken@@syntax",
        ]
        with patch.object(EmailValidatorService, "resolve_mx", new_callable=AsyncMock) as mock_mx:
            mock_mx.return_value = (True, [{"priority": 10, "host": "mail.company.com"}], "Active MX")
            batch = await EmailValidatorService.validate_batch(emails)
            
            self.assertEqual(batch["total"], 4)  # empty filtered out
            self.assertIn("deliverable_count", batch)
            self.assertIn("risky_count", batch)
            self.assertIn("invalid_count", batch)
            self.assertEqual(batch["disposable_count"], 1)
            self.assertEqual(batch["syntax_error_count"], 1)
            self.assertEqual(batch["deliverable_count"], 2)

    # 6. SPF Evaluation & Multiple Record RFC 7208 Error
    async def test_spf_evaluation_pass(self):
        with patch.object(DnsAuthenticatorService, "_query_txt", new_callable=AsyncMock) as mock_txt:
            mock_txt.return_value = ["v=spf1 include:_spf.google.com ~all"]
            res = await DnsAuthenticatorService.check_spf("example.com")
            
            self.assertEqual(res["status"], "pass")
            self.assertEqual(res["policy"], "~all")
            self.assertEqual(res["score"], 28)

    async def test_spf_evaluation_multiple_records_error(self):
        with patch.object(DnsAuthenticatorService, "_query_txt", new_callable=AsyncMock) as mock_txt:
            mock_txt.return_value = [
                "v=spf1 include:_spf.google.com ~all",
                "v=spf1 include:mail.protection.outlook.com -all"
            ]
            res = await DnsAuthenticatorService.check_spf("example.com")
            
            self.assertEqual(res["status"], "error")
            self.assertIn("Multiple SPF records found", res["details"])
            self.assertEqual(res["score"], 5)

    async def test_spf_evaluation_missing(self):
        with patch.object(DnsAuthenticatorService, "_query_txt", new_callable=AsyncMock) as mock_txt:
            mock_txt.return_value = []
            res = await DnsAuthenticatorService.check_spf("example.com")
            
            self.assertEqual(res["status"], "fail")
            self.assertEqual(res["score"], 0)

    # 7. DMARC Evaluation & 2024 Bulk Sender Compliance
    async def test_dmarc_evaluation_reject_pass(self):
        with patch.object(DnsAuthenticatorService, "_query_txt", new_callable=AsyncMock) as mock_txt:
            mock_txt.return_value = ["v=DMARC1; p=reject; rua=mailto:dmarc@example.com; pct=100;"]
            res = await DnsAuthenticatorService.check_dmarc("example.com")
            
            self.assertEqual(res["status"], "pass")
            self.assertEqual(res["policy"], "reject")
            self.assertTrue(res["meets_2024_bulk_requirements"])
            self.assertEqual(res["score"], 35)

    async def test_dmarc_evaluation_monitoring_none(self):
        with patch.object(DnsAuthenticatorService, "_query_txt", new_callable=AsyncMock) as mock_txt:
            mock_txt.return_value = ["v=DMARC1; p=none; sp=none;"]
            res = await DnsAuthenticatorService.check_dmarc("example.com")
            
            self.assertEqual(res["status"], "warning")
            self.assertEqual(res["policy"], "none")
            self.assertFalse(res["meets_2024_bulk_requirements"])
            self.assertEqual(res["score"], 25)

    # 8. Health Score & Grade Computation
    def test_health_score_and_grade_calculation(self):
        spf_good = {"score": 30}
        dmarc_good = {"score": 35}
        dkim_good = {"score": 25}
        mx_good = {"score": 10}
        
        score, grade = DnsAuthenticatorService.calculate_health_score(spf_good, dmarc_good, dkim_good, mx_good)
        self.assertEqual(score, 100)
        self.assertEqual(grade, "A+")

        spf_med = {"score": 20}
        dmarc_warn = {"score": 15}
        dkim_none = {"score": 0}
        mx_good = {"score": 10}
        
        score2, grade2 = DnsAuthenticatorService.calculate_health_score(spf_med, dmarc_warn, dkim_none, mx_good)
        self.assertEqual(score2, 45)
        self.assertEqual(grade2, "F")

    # 9. Recommendation Generator
    def test_recommended_records_generation(self):
        bad_results = {
            "spf": {"status": "fail", "record": None},
            "dmarc": {"status": "fail", "record": None, "policy": None},
            "dkim": {"status": "fail", "record": None},
            "mx": {"status": "fail"}
        }
        recs = DnsAuthenticatorService.generate_recommended_records("myclient.io", bad_results)
        self.assertEqual(len(recs), 3)
        types = [r["name"] for r in recs]
        self.assertIn("@", types)
        self.assertIn("_dmarc", types)
        self.assertIn("default._domainkey", types)


class TestDeliverabilityApiEndpoints(unittest.TestCase):
    """Integration tests for FastAPI Deliverability REST endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.test_dir.name) / "data"
        cls.archive_dir = cls.data_dir / "eml_archive"
        cls.db_path = cls.data_dir / "test_deliverability_api.db"
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        cls.archive_dir.mkdir(parents=True, exist_ok=True)

        cls.orig_db = settings.DATABASE_PATH
        cls.orig_archive = settings.EML_ARCHIVE_DIR
        cls.orig_storage = settings.EML_STORAGE_DIR

        settings.DATABASE_PATH = cls.db_path
        settings.EML_ARCHIVE_DIR = cls.archive_dir
        settings.EML_STORAGE_DIR = cls.archive_dir

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
        settings.EML_ARCHIVE_DIR = cls.orig_archive
        settings.EML_STORAGE_DIR = cls.orig_storage
        cls.test_dir.cleanup()

    def test_deliverability_info_endpoint(self):
        res = self.client.get("/api/deliverability/info", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "healthy")
        self.assertIn("features", data)
        self.assertGreater(data["features"]["disposable_domains_tracked"], 200)

    def test_validate_single_email_endpoint(self):
        res = self.client.post(
            "/api/deliverability/validate-email",
            headers=self.auth_headers,
            json={"email": "throwaway@tempmail.com"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["result"]["email"], "throwaway@tempmail.com")
        self.assertTrue(data["result"]["is_disposable"])
        self.assertEqual(data["result"]["status"], "risky")

    def test_validate_batch_endpoint(self):
        emails = [
            "lead1@bitnade.com",
            "fake@mailinator.com",
            "broken-email-syntax@@@"
        ]
        res = self.client.post(
            "/api/deliverability/validate-batch",
            headers=self.auth_headers,
            json={"emails": emails}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        b = data["result"]
        self.assertEqual(b["total"], 3)
        self.assertEqual(b["disposable_count"], 1)
        self.assertEqual(b["syntax_error_count"], 1)

    def test_dns_check_endpoint(self):
        res = self.client.post(
            "/api/deliverability/dns-check",
            headers=self.auth_headers,
            json={"domain": "gmail.com"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        diag = data["result"]
        self.assertEqual(diag["domain"], "gmail.com")
        self.assertIn("score", diag)
        self.assertIn("grade", diag)
        self.assertIn("spf", diag)
        self.assertIn("dmarc", diag)
        self.assertIn("dkim", diag)
        self.assertIn("mx", diag)

    def test_deliverability_page_route(self):
        res = self.client.get("/deliverability", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        self.assertIn("panel-deliverability", res.text)
        self.assertTrue(
            "Deliverability & DNS Authenticator" in res.text or
            "Deliverability &amp; DNS Authenticator" in res.text
        )



if __name__ == "__main__":
    unittest.main()
