import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import price_pipeline


class OfficialValueCacheTest(unittest.TestCase):
    def test_preloaded_official_values_avoid_per_code_query(self):
        rows = [{
            "code": "1234",
            "date": "2026-11-06",
            "open": Decimal("100"),
            "high": Decimal("110"),
            "low": Decimal("90"),
            "close": Decimal("105"),
            "volume": Decimal("1000"),
        }]
        official = {
            "2026-11-06": {
                "open": Decimal("101"),
                "high": Decimal("111"),
                "low": Decimal("91"),
                "close": Decimal("106"),
                "volume": Decimal("1001"),
            }
        }

        with patch.object(price_pipeline, "db", side_effect=AssertionError("DB query is not expected")):
            result = price_pipeline._with_official_values(
                "1234", rows, [], date(2026, 11, 6), "raw", "raw", official,
            )

        self.assertEqual(result[0]["close"], Decimal("106"))
        self.assertEqual(result[0]["volume"], Decimal("1001"))


if __name__ == "__main__":
    unittest.main()
