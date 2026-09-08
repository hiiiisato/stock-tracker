import unittest
from datetime import date

from compute_price_stats import _latest_price_date


class _Cursor:
    def __init__(self, value):
        self.value = value
        self.query = None

    def execute(self, query):
        self.query = " ".join(query.split())

    def fetchone(self):
        return (self.value,)


class PriceStatsDateTest(unittest.TestCase):
    def test_latest_db_business_date_is_used(self):
        cursor = _Cursor(date(2026, 9, 7))
        self.assertEqual(_latest_price_date(cursor), date(2026, 9, 7))
        self.assertEqual(cursor.query, "SELECT MAX(date) FROM daily_prices")

    def test_empty_prices_returns_none(self):
        self.assertIsNone(_latest_price_date(_Cursor(None)))


if __name__ == "__main__":
    unittest.main()
