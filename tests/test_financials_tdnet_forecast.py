import unittest

from financials_tdnet import _upsert_forecasts


class _Cursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


class TdnetForecastUpsertTest(unittest.TestCase):
    def test_partial_forecast_does_not_erase_existing_non_null_values(self):
        cursor = _Cursor()
        _upsert_forecasts(
            cursor,
            [("3003", "2026-12-31", "A", None, None, None, 126, None, "2026-10-28")],
        )

        sql, params = cursor.calls[0]
        self.assertIn(
            "`net_income`=COALESCE(VALUES(`net_income`),`net_income`)",
            sql,
        )
        self.assertIn(
            "`div_per_share`=COALESCE(VALUES(`div_per_share`),`div_per_share`)",
            sql,
        )
        self.assertEqual(params[0], "3003")


if __name__ == "__main__":
    unittest.main()
