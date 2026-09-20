import hashlib
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

import app as stock_app


class WatchlistWriteProtectionTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {"PORTFOLIO_PASSCODE": "test-pass", "WATCHLIST_PASSCODE": ""},
            clear=False,
        )
        self.env.start()
        self.client = stock_app.app.test_client()
        self.token = stock_app._watchlist_token()
        self.csrf = hashlib.sha256(("wl-csrf:" + self.token).encode()).hexdigest()

    def tearDown(self):
        self.env.stop()

    def test_unauthenticated_write_redirects_to_watchlist_login(self):
        response = self.client.post("/watchlist/lists/create", data={"name": "業績良さそう"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/watchlist/login?next=/watchlist", response.headers["Location"])

    def test_authenticated_write_rejects_missing_or_invalid_csrf_token(self):
        self.client.set_cookie("wl_auth", self.token)
        response = self.client.post("/watchlist/lists/create", data={"name": "業績良さそう"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/watchlist/lists/create", data={"name": "業績良さそう", "csrf": "invalid"}
        )
        self.assertEqual(response.status_code, 403)

    def test_login_sets_a_separate_httponly_watchlist_cookie(self):
        response = self.client.post(
            "/watchlist/login", data={"passcode": "test-pass", "next": "/watchlist"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/watchlist")
        self.assertIn("wl_auth=", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])

    def test_price_alert_edit_preserves_an_unedited_triggered_direction(self):
        self.client.set_cookie("wl_auth", self.token)
        with patch("watchlist_service.set_price_alerts") as set_alerts:
            response = self.client.post("/watchlist/alert", data={
                "code": "7203", "upper": "", "lower": "2500",
                "upper_status": "triggered", "lower_status": "active", "csrf": self.csrf,
            })

        self.assertEqual(response.status_code, 302)
        set_alerts.assert_called_once_with(
            "7203", None, Decimal("2500.0000"), preserve={"upper"}
        )


if __name__ == "__main__":
    unittest.main()
