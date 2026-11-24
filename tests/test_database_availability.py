from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import pymysql

import config
from app import _database_quota_response, _quota_retry_after_seconds, app


RETRY_WORKFLOW = Path(config.__file__).resolve().parent / ".github/workflows/retry_failed_batch.yml"


class DatabaseAvailabilityTest(unittest.TestCase):
    def test_tidb_quota_error_has_a_stable_application_error(self):
        original = pymysql.err.OperationalError(
            1105,
            "Due to the usage quota being exhausted, access has been restricted.",
        )
        with patch.object(config.pymysql, "connect", side_effect=original):
            with self.assertRaises(config.DatabaseQuotaExceeded):
                config.get_conn()

    def test_unrelated_database_error_is_not_reclassified(self):
        original = pymysql.err.OperationalError(1045, "Access denied")
        with patch.object(config.pymysql, "connect", side_effect=original):
            with self.assertRaises(pymysql.err.OperationalError):
                config.get_conn()

    def test_quota_page_is_a_clear_503_without_internal_identifiers(self):
        with app.app_context():
            body, status, headers = _database_quota_response(
                config.DatabaseQuotaExceeded("secret cluster 123")
            )
        self.assertEqual(status, 503)
        self.assertIn("メンテナンス中", body)
        self.assertNotIn("secret cluster", body)
        self.assertGreater(int(headers["Retry-After"]), 0)

    def test_retry_after_uses_next_utc_month(self):
        seconds = _quota_retry_after_seconds(
            datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc)
        )
        self.assertEqual(seconds, 60)

    def test_health_exposes_quota_state_without_cluster_details(self):
        error = config.DatabaseQuotaExceeded("secret cluster 123")
        with patch("app.get_conn", side_effect=error):
            response = app.test_client().get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.text, "DB_QUOTA_EXHAUSTED")
        self.assertNotIn("secret cluster", response.text)

    def test_quota_failure_is_not_automatically_retried(self):
        source = RETRY_WORKFLOW.read_text()
        self.assertIn("usage quota (being )?exhausted", source)
        self.assertIn("DatabaseQuotaExceeded", source)


if __name__ == "__main__":
    unittest.main()
