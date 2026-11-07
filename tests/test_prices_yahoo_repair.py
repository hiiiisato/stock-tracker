import unittest
from datetime import date

from prices_yahoo import _get_missing_date_range


class _Cursor:
    def __init__(self, latest):
        self.latest = latest

    def execute(self, _sql):
        return None

    def fetchone(self):
        return (self.latest,)

    def close(self):
        return None


class _Connection:
    def __init__(self, latest):
        self.latest = latest

    def cursor(self):
        return _Cursor(self.latest)


class YahooRepairRangeTest(unittest.TestCase):
    def test_current_database_still_repairs_recent_window(self):
        start, end = _get_missing_date_range(
            _Connection(date(2026, 11, 7)),
            date_to=date(2026, 11, 7),
            repair_days=14,
        )
        self.assertEqual(start, date(2026, 10, 25))
        self.assertEqual(end, date(2026, 11, 7))

    def test_long_outage_fetches_every_missing_day(self):
        start, end = _get_missing_date_range(
            _Connection(date(2026, 10, 20)),
            date_to=date(2026, 11, 7),
            repair_days=14,
        )
        self.assertEqual(start, date(2026, 10, 21))
        self.assertEqual(end, date(2026, 11, 7))

    def test_empty_database_keeps_full_initial_backfill(self):
        start, end = _get_missing_date_range(
            _Connection(None),
            date_to=date(2026, 11, 7),
        )
        self.assertEqual(start, date(2024, 3, 30))
        self.assertEqual(end, date(2026, 11, 7))

    def test_repair_window_must_be_positive(self):
        with self.assertRaises(ValueError):
            _get_missing_date_range(
                _Connection(date(2026, 11, 7)),
                date_to=date(2026, 11, 7),
                repair_days=0,
            )


if __name__ == "__main__":
    unittest.main()
