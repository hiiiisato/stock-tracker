import unittest
from datetime import date, datetime
from unittest.mock import patch

import daily_report


class _NotifyCursor:
    def __init__(self, notified_at=None):
        self.notified_at = notified_at
        self.executed = []
        self.closed = False

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (self.notified_at,)

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class DailyReportNotificationTest(unittest.TestCase):
    report_date = date(2026, 9, 4)

    def _run(self, notified_at=None, *, configured=True, push_result=True):
        cursor = _NotifyCursor(notified_at)
        conn = _Connection(cursor)
        with patch.object(daily_report, "ensure_table"), \
                patch.object(daily_report, "get_conn", return_value=conn), \
                patch("line_notify.is_configured", return_value=configured), \
                patch("line_notify.push_text", return_value=push_result) as push:
            result = daily_report.notify_report_ready(self.report_date)
        return result, cursor, conn, push

    def test_success_is_persisted(self):
        result, cursor, conn, push = self._run()

        self.assertTrue(result)
        push.assert_called_once()
        updates = [(sql, params) for sql, params in cursor.executed
                   if sql.startswith("UPDATE daily_reports")]
        self.assertEqual(updates[0][1], (True, None, self.report_date))
        self.assertEqual(conn.commits, 1)
        self.assertTrue(cursor.closed)
        self.assertTrue(conn.closed)

    def test_sent_report_is_not_pushed_twice(self):
        result, cursor, conn, push = self._run(datetime(2026, 9, 4, 12, 0))

        self.assertTrue(result)
        push.assert_not_called()
        self.assertFalse(any(sql.startswith("UPDATE daily_reports")
                             for sql, _params in cursor.executed))
        self.assertEqual(conn.commits, 0)

    def test_push_failure_is_persisted_and_returned(self):
        result, cursor, conn, push = self._run(push_result=False)

        self.assertFalse(result)
        push.assert_called_once()
        updates = [(sql, params) for sql, params in cursor.executed
                   if sql.startswith("UPDATE daily_reports")]
        self.assertEqual(
            updates[0][1],
            (False, "LINE Messaging API push failed", self.report_date),
        )
        self.assertEqual(conn.commits, 1)

    def test_missing_credentials_is_a_visible_failure(self):
        result, cursor, conn, push = self._run(configured=False)

        self.assertFalse(result)
        push.assert_not_called()
        updates = [(sql, params) for sql, params in cursor.executed
                   if sql.startswith("UPDATE daily_reports")]
        self.assertEqual(
            updates[0][1],
            (False, "LINE credentials are not configured", self.report_date),
        )
        self.assertEqual(conn.commits, 1)


if __name__ == "__main__":
    unittest.main()
