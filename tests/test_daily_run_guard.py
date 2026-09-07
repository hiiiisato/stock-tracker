import unittest
from datetime import date
from unittest.mock import patch

import daily_run


class DailyRunGuardTest(unittest.TestCase):
    def test_latest_business_date_allows_main_batch_to_continue(self):
        with patch.object(
            daily_run,
            "_evening_target_date",
            return_value=date(2026, 9, 7),
        ):
            self.assertTrue(daily_run._has_processable_prices())

    def test_missing_business_date_stops_main_batch(self):
        with patch.object(daily_run, "_evening_target_date", return_value=None):
            self.assertFalse(daily_run._has_processable_prices())


if __name__ == "__main__":
    unittest.main()
