import unittest
from datetime import date

from portfolio_analytics import dividend_projection


class DividendProjectionTest(unittest.TestCase):
    def test_aggregates_accounts_and_uses_recent_ex_date_pattern(self):
        holdings = [
            {
                "code": "1111",
                "name": "テスト株",
                "asset_class": "stock",
                "qty": 100,
                "value": 200_000,
            },
            {
                "code": "1111",
                "name": "テスト株",
                "asset_class": "stock",
                "qty": 50,
                "value": 100_000,
            },
        ]
        result = dividend_projection(
            holdings,
            {"1111": 100},
            {
                "1111": [
                    (date(2025, 3, 28), 40),
                    (date(2025, 9, 29), 60),
                ]
            },
        )

        self.assertEqual(result["annual_total"], 15_000)
        self.assertEqual(result["monthly"][2], 6_000)
        self.assertEqual(result["monthly"][8], 9_000)
        self.assertEqual(result["undated"], 0)
        self.assertEqual(result["contributors"][0]["share"], 100.0)

    def test_ignores_funds_and_keeps_unknown_month_separate(self):
        holdings = [
            {
                "code": "2222",
                "name": "履歴なし株",
                "asset_class": "stock",
                "qty": 100,
                "value": 100_000,
            },
            {
                "code": "FUND:X",
                "name": "投資信託",
                "asset_class": "fund",
                "qty": 10_000,
                "value": 500_000,
            },
        ]
        result = dividend_projection(
            holdings,
            {"2222": 20, "FUND:X": 999},
            {},
        )

        self.assertEqual(result["annual_total"], 2_000)
        self.assertEqual(sum(result["monthly"]), 0)
        self.assertEqual(result["undated"], 2_000)
        self.assertEqual(len(result["contributors"]), 1)


if __name__ == "__main__":
    unittest.main()
