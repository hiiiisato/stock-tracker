import unittest
from datetime import date

from financials_store import merge_canonical_rows
from financials_view import _merge_annual_records


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class FinancialCanonicalMergeTests(unittest.TestCase):
    def test_yahoo_only_fills_existing_nulls(self):
        cur = RecordingCursor()
        columns = ["code", "period_end", "period_type", "revenue", "net_income"]
        rows = [("3774", date(2024, 3, 31), "A", None, 19_831_000_000)]

        merge_canonical_rows(cur, columns, rows, prefer_incoming=False)

        sql, _params = cur.calls[0]
        self.assertIn("`revenue`=COALESCE(`revenue`,VALUES(`revenue`))", sql)
        self.assertIn("`net_income`=COALESCE(`net_income`,VALUES(`net_income`))", sql)

    def test_official_source_updates_only_with_non_null_values(self):
        cur = RecordingCursor()
        columns = ["code", "period_end", "period_type", "revenue", "net_income"]
        rows = [("3774", date(2024, 3, 31), "A", None, 19_831_000_000)]

        merge_canonical_rows(cur, columns, rows, prefer_incoming=True)

        sql, _params = cur.calls[0]
        self.assertIn("`revenue`=COALESCE(VALUES(`revenue`),`revenue`)", sql)
        self.assertIn("`net_income`=COALESCE(VALUES(`net_income`),`net_income`)", sql)

    def test_annual_view_merges_by_year_and_metric(self):
        edinet = [{
            "code": "3774", "fiscal_year": 2024, "period_end": date(2024, 3, 31),
            "revenue": 276_080_000_000.0, "operating_income": 29_029_000_000.0,
            "net_income": 19_831_000_000.0, "ordinary_income": None,
            "profit_before_tax": 28_934_000_000.0,
            "accounting_standard": "IFRS",
        }]
        fallback = [
            {
                "code": "3774", "fiscal_year": 2024, "period_end": date(2024, 3, 31),
                "revenue": 276_080_000_000.0, "operating_income": 29_029_000_000.0,
                "net_income": None,
            },
            {
                "code": "3774", "fiscal_year": 2026, "period_end": date(2026, 3, 31),
                "revenue": 345_395_000_000.0, "operating_income": 34_835_000_000.0,
                "net_income": 24_188_000_000.0,
            },
        ]

        rows = _merge_annual_records(edinet, fallback)["3774"]

        self.assertEqual([r["fiscal_year"] for r in rows], [2024, 2026])
        self.assertEqual(rows[0]["revenue"], 276_080_000_000.0)
        self.assertEqual(rows[0]["net_income"], 19_831_000_000.0)
        self.assertEqual(rows[0]["sources"]["net_income"], "edinet")
        self.assertEqual(rows[0]["secondary_profit"], 28_934_000_000.0)
        self.assertEqual(rows[0]["secondary_profit_label"], "税引前利益")

    def test_inconsistent_edinet_scope_does_not_fill_canonical_row(self):
        edinet = [
            {
                "code": "7326", "fiscal_year": 2021,
                "period_end": date(2021, 3, 31),
                "revenue": 80_000_000_000.0, "net_income": 3_000_000_000.0,
                "accounting_standard": "JGAAP",
            },
            {
                "code": "7326", "fiscal_year": 2022,
                "period_end": date(2022, 3, 31),
                "revenue": 88_365_000_000.0, "net_income": 4_000_000_000.0,
                "accounting_standard": "JGAAP",
            },
        ]
        fallback = [{
            "code": "7326", "fiscal_year": 2022, "period_end": date(2022, 3, 31),
            "revenue": 551_000_000.0, "net_income": None,
            "accounting_standard": None,
        }]

        rows = _merge_annual_records(edinet, fallback)["7326"]
        row = rows[0]

        self.assertEqual([r["fiscal_year"] for r in rows], [2022])
        self.assertEqual(row["revenue"], 551_000_000.0)
        self.assertIsNone(row["net_income"])
        self.assertEqual(row["source"], "financials")

    def test_consistent_anchor_allows_edinet_only_history(self):
        edinet = [
            {
                "code": "3774", "fiscal_year": 2021,
                "period_end": date(2021, 3, 31),
                "revenue": 213_000_000_000.0, "net_income": 9_700_000_000.0,
                "accounting_standard": "IFRS",
            },
            {
                "code": "3774", "fiscal_year": 2022,
                "period_end": date(2022, 3, 31),
                "revenue": 226_335_000_000.0, "net_income": 15_672_000_000.0,
                "accounting_standard": "IFRS",
            },
        ]
        fallback = [{
            "code": "3774", "fiscal_year": 2022,
            "period_end": date(2022, 3, 31),
            "revenue": 226_335_000_000.0, "net_income": None,
            "accounting_standard": None,
        }]

        rows = _merge_annual_records(edinet, fallback)["3774"]

        self.assertEqual([r["fiscal_year"] for r in rows], [2021, 2022])
        self.assertEqual(rows[0]["source"], "edinet")
        self.assertEqual(rows[1]["net_income"], 15_672_000_000.0)

    def test_annual_view_calculates_missing_roe_from_average_equity(self):
        fallback = [
            {
                "code": "7203", "fiscal_year": 2025,
                "period_end": date(2025, 3, 31), "net_income": 47_650.9,
                "net_assets": 359_248.3, "total_assets": 936_013.5,
            },
            {
                "code": "7203", "fiscal_year": 2026,
                "period_end": date(2026, 3, 31), "net_income": 38_481.0,
                "net_assets": 399_188.5, "total_assets": 1_055_223.3,
            },
        ]

        latest = _merge_annual_records([], fallback)["7203"][-1]

        self.assertEqual(10.1475, latest["roe_actual_pct"])
        self.assertEqual("calculated_average_balance", latest["roe_source"])

    def test_annual_view_prefers_reported_roe(self):
        edinet = [{
            "code": "7203", "fiscal_year": 2026,
            "period_end": date(2026, 3, 31), "net_income": 38_481.0,
            "net_assets": 399_188.5, "total_assets": 1_055_223.3,
            "roe_official": 0.101,
        }]
        fallback = [{
            "code": "7203", "fiscal_year": 2026,
            "period_end": date(2026, 3, 31), "net_income": 38_481.0,
            "net_assets": 399_188.5, "total_assets": 1_055_223.3,
        }]

        latest = _merge_annual_records(edinet, fallback)["7203"][-1]

        self.assertEqual(10.1, latest["roe_actual_pct"])
        self.assertEqual("reported", latest["roe_source"])


if __name__ == "__main__":
    unittest.main()
