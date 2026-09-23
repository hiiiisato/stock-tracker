import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import daily_run


class DailyRunGuardTest(unittest.TestCase):
    def test_main_guard_uses_current_jst_date_after_batch_start(self):
        now = datetime(2026, 9, 9, 12, 20, tzinfo=timezone.utc)  # 9/9 21:20 JST
        with patch.object(daily_run, "_latest_price_date", return_value=date(2026, 9, 7)):
            self.assertEqual(daily_run._main_guard_target_date(now), date(2026, 9, 9))

    def test_main_guard_uses_latest_price_for_overnight_delay(self):
        now = datetime(2026, 9, 9, 15, 20, tzinfo=timezone.utc)  # 9/10 00:20 JST
        with patch.object(daily_run, "_latest_price_date", return_value=date(2026, 9, 9)):
            self.assertEqual(daily_run._main_guard_target_date(now), date(2026, 9, 9))


def _mock_conn(fetchone_return):
    """get_conn()が返すカーソルのfetchone()を固定値にするモック接続。"""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_return
    conn.cursor.return_value = cur
    return conn


class CalendarStatusTest(unittest.TestCase):
    """2026-09-22障害の再発防止: 未登録を営業日と誤判定しないことを確認する。"""

    def test_holiday_row_returns_holiday(self):
        with patch.object(daily_run, "get_conn", return_value=_mock_conn((True,))):
            self.assertEqual(daily_run._calendar_status(date(2026, 9, 23)), "holiday")

    def test_open_row_returns_open(self):
        with patch.object(daily_run, "get_conn", return_value=_mock_conn((False,))):
            self.assertEqual(daily_run._calendar_status(date(2026, 9, 24)), "open")

    def test_missing_row_returns_unregistered_not_open(self):
        with patch.object(daily_run, "get_conn", return_value=_mock_conn(None)):
            self.assertEqual(daily_run._calendar_status(date(2026, 9, 23)), "unregistered")

    def test_db_error_returns_unregistered_not_open(self):
        with patch.object(daily_run, "get_conn", side_effect=RuntimeError("db down")):
            self.assertEqual(daily_run._calendar_status(date(2026, 9, 23)), "unregistered")


if __name__ == "__main__":
    unittest.main()
