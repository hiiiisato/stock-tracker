import unittest
from datetime import date, datetime, timezone

from batch_dates import (
    jst_today,
    latest_sunday,
    price_fetch_end_date,
    resolve_evening_business_date,
)


class BatchDateTest(unittest.TestCase):
    def test_jst_today_does_not_depend_on_runner_timezone(self):
        now_utc = datetime(2026, 9, 6, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(jst_today(now_utc), date(2026, 9, 7))

    def test_weekly_key_is_latest_sunday_in_jst(self):
        self.assertEqual(latest_sunday(date(2026, 9, 7)), date(2026, 9, 6))
        self.assertEqual(latest_sunday(date(2026, 9, 6)), date(2026, 9, 6))

    def test_price_fetch_before_close_is_limited_to_previous_day(self):
        now = datetime(2026, 9, 10, 3, 20, tzinfo=timezone.utc)  # 9/10 12:20 JST
        self.assertEqual(price_fetch_end_date(now), date(2026, 9, 9))

    def test_price_fetch_after_close_includes_current_day(self):
        now = datetime(2026, 9, 10, 7, 17, tzinfo=timezone.utc)  # 9/10 16:17 JST
        self.assertEqual(price_fetch_end_date(now), date(2026, 9, 10))

    def test_evening_uses_same_day_prices(self):
        now = datetime(2026, 9, 4, 11, 47, tzinfo=timezone.utc)  # 9/4 20:47 JST
        self.assertEqual(
            resolve_evening_business_date(date(2026, 9, 4), now),
            date(2026, 9, 4),
        )

    def test_evening_catches_up_after_midnight_jst(self):
        now = datetime(2026, 9, 4, 16, 30, tzinfo=timezone.utc)  # 9/5 01:30 JST
        self.assertEqual(
            resolve_evening_business_date(date(2026, 9, 4), now),
            date(2026, 9, 4),
        )

    def test_after_midnight_does_not_accept_prices_two_days_old(self):
        now = datetime(2026, 9, 9, 15, 30, tzinfo=timezone.utc)  # 9/10 00:30 JST
        self.assertIsNone(resolve_evening_business_date(date(2026, 9, 7), now))

    def test_holiday_evening_does_not_reprocess_old_prices(self):
        now = datetime(2026, 9, 7, 11, 47, tzinfo=timezone.utc)  # 9/7 20:47 JST
        self.assertIsNone(resolve_evening_business_date(date(2026, 9, 4), now))

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            jst_today(datetime(2026, 9, 7, 0, 0))


if __name__ == "__main__":
    unittest.main()
