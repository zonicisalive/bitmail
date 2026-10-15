"""Inspecting a sent email must not register a click, an open or an unsubscribe."""

import unittest

from app.routes.storage import strip_tracking


class TestPreviewTracking(unittest.TestCase):
    def test_tracking_removed(self):
        html = (
            '<a href="https://sender.bitnade.com/track/click/eml_1?url=https%3A%2F%2Fdemo.bitnade.com%2Fa%3Fb%3D1">x</a>'
            '<a href="https://sender.bitnade.com/unsubscribe/tok123">u</a>'
            '<img src="https://sender.bitnade.com/track/open/eml_1" width="1" />'
        )
        out = strip_tracking(html)
        self.assertNotIn("/track/", out)
        self.assertIn('href="https://demo.bitnade.com/a?b=1"', out)
        self.assertIn('href="#"', out)


if __name__ == "__main__":
    unittest.main()
