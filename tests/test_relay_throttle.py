"""
Relay throttling: a burst above the relay's limit used to fail outright
(Stalwart dropped 14 of 20 connections with "Unexpected EOF").
"""

import asyncio
import unittest
from email.mime.multipart import MIMEMultipart
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosmtplib

from app import sender
from app.queue import effective_rate_limit


def _msg():
    m = MIMEMultipart()
    m["Message-ID"] = "<test@bitmail>"
    m["Subject"] = "hi"
    return m


class FakeSMTP:
    """Drops the first `drops` connections like a throttling relay, then accepts."""
    instances = 0
    drops = 0
    sent = 0

    def __init__(self, **kwargs):
        pass

    async def connect(self):
        FakeSMTP.instances += 1
        if FakeSMTP.instances <= FakeSMTP.drops:
            raise aiosmtplib.SMTPServerDisconnected("Unexpected EOF received")

    async def login(self, *a):
        pass

    async def send_message(self, *a, **k):
        FakeSMTP.sent += 1
        return ({}, "250 OK")

    async def quit(self):
        pass


RELAY = {"id": "r1", "host": "mail.example.net", "port": 465, "use_ssl": 1, "use_tls": 0,
         "username": "u", "password": "p", "allow_failover": False}


class TestRelayRetry(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = FakeSMTP.sent = 0

    def _send(self):
        with patch.object(sender.aiosmtplib, "SMTP", FakeSMTP), \
             patch.object(sender.asyncio, "sleep", AsyncMock()):
            return asyncio.run(sender.dispatch_smtp_message(_msg(), "a@b.com", "c@d.com", dict(RELAY)))

    def test_dropped_connections_are_retried(self):
        FakeSMTP.drops = 2
        ok, detail, _ = self._send()
        self.assertTrue(ok, detail)
        self.assertEqual(FakeSMTP.sent, 1)  # delivered exactly once

    def test_gives_up_after_retries(self):
        FakeSMTP.drops = 99
        ok, detail, _ = self._send()
        self.assertFalse(ok)
        self.assertIn("Unexpected EOF", detail)
        self.assertEqual(FakeSMTP.instances, len(sender.SMTP_RETRY_DELAYS) + 1)

    def test_retry_rules(self):
        eof = aiosmtplib.SMTPServerDisconnected("Unexpected EOF received")
        self.assertIsNotNone(sender.retry_delay(eof, "connect", 1))
        # A drop while the message is being sent may already be delivered: never retry.
        self.assertIsNone(sender.retry_delay(eof, "send", 1))
        # Bad credentials will not fix themselves.
        self.assertIsNone(sender.retry_delay(aiosmtplib.SMTPAuthenticationError(535, "bad"), "login", 1))
        # "Try again later" on MAIL FROM is safe to retry; a permanent 550 is not.
        self.assertIsNotNone(sender.retry_delay(aiosmtplib.SMTPSenderRefused(451, "later", "a@b.com"), "send", 1))
        self.assertIsNone(sender.retry_delay(aiosmtplib.SMTPSenderRefused(550, "no", "a@b.com"), "send", 1))
        self.assertIsNone(sender.retry_delay(eof, "connect", len(sender.SMTP_RETRY_DELAYS) + 1))


class TestRelayRateCap(unittest.TestCase):
    def test_campaign_never_faster_than_relay(self):
        relay = SimpleNamespace(rate_limit_per_second=4, is_sandbox=False)
        self.assertEqual(effective_rate_limit(SimpleNamespace(rate_limit_per_sec=25, smtp_config=relay)), 4)
        self.assertEqual(effective_rate_limit(SimpleNamespace(rate_limit_per_sec=2, smtp_config=relay)), 2)
        self.assertEqual(effective_rate_limit(SimpleNamespace(rate_limit_per_sec=25, smtp_config=None)), 25)


if __name__ == "__main__":
    unittest.main()
