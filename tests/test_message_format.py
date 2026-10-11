"""Outgoing message shape: plain-text part, text-only mode, no bulk fingerprint headers."""

import email
import unittest
from email import policy

from app.sender import build_mime_message
from app.template_engine import template_engine


class TestRendering(unittest.TestCase):
    def test_html_email_gets_text_alternative_with_unsubscribe(self):
        r = template_engine.render_email("Hi", "<p>Hello <a href='https://x.example/y'>there</a></p>",
                                         email_storage_id="eml_1", track_opens=True, track_clicks=True)
        self.assertIn("Hello", r.body_text)
        self.assertIn("Unsubscribe:", r.body_text)
        self.assertNotIn("track/click", r.body_text)  # text links are the real URLs
        self.assertIn("track/open", r.rendered_html)

    def test_text_only_has_no_html_tracking_or_footer_markup(self):
        r = template_engine.render_email("Hi", "", text_template="Hello {{email}}, see https://x.example",
                                         email_storage_id="eml_2", track_opens=True, track_clicks=True)
        self.assertEqual(r.rendered_html, "")
        self.assertIn("https://x.example", r.body_text)
        self.assertNotIn("track/", r.body_text)
        self.assertEqual(r.body_text.count("Unsubscribe:"), 1)


class TestMime(unittest.TestCase):
    def _parse(self, msg):
        return email.message_from_string(msg.as_string(), policy=policy.default)

    def test_text_only_is_single_plain_part(self):
        msg, _ = build_mime_message("Zonic", "z@bitnade.com", None, "a@b.com", "Hi", None, "Hello",
                                    unsubscribe_url="https://u.example/unsub")
        m = self._parse(msg)
        self.assertEqual(m.get_content_type(), "text/plain")
        self.assertFalse(m.is_multipart())
        self.assertIn("List-Unsubscribe", m)

    def test_html_mail_is_alternative_with_text_first(self):
        msg, _ = build_mime_message("Zonic", "z@bitnade.com", None, "a@b.com", "Hi", "<p>Hello</p>", "Hello")
        m = self._parse(msg)
        self.assertEqual(m.get_content_type(), "multipart/alternative")
        self.assertEqual([p.get_content_type() for p in m.iter_parts()], ["text/plain", "text/html"])


if __name__ == "__main__":
    unittest.main()
