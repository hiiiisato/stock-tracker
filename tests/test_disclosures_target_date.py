import unittest
from datetime import date
from unittest.mock import call, patch

import disclosures


class DisclosuresTargetDateTest(unittest.TestCase):
    @patch.object(disclosures, "backfill_missing_commentaries")
    @patch.object(disclosures, "build_market_summary", return_value=True)
    @patch.object(disclosures, "enrich_highlights", return_value=2)
    @patch.object(disclosures, "fetch_and_store_day", return_value=3)
    @patch.object(disclosures, "ensure_tables")
    def test_run_daily_uses_explicit_logical_date(
        self,
        ensure_tables,
        fetch_day,
        enrich,
        build_summary,
        backfill,
    ):
        result = disclosures.run_daily(with_ai=True, target_date=date(2026, 11, 6))

        ensure_tables.assert_called_once_with()
        self.assertEqual(fetch_day.call_args_list, [call(date(2026, 11, 5)), call(date(2026, 11, 6))])
        enrich.assert_called_once_with(days_back=4)
        build_summary.assert_called_once_with(date(2026, 11, 6))
        backfill.assert_called_once_with()
        self.assertEqual(result, {"stored": 6, "enriched": 2, "summary_saved": True})


if __name__ == "__main__":
    unittest.main()
