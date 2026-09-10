import unittest
from datetime import date, datetime
from unittest.mock import patch

import prices_yahoo


class _Response:
    status_code = 200

    def json(self):
        timestamps = [
            int(datetime(2026, 9, 9, 12).timestamp()),
            int(datetime(2026, 9, 10, 12).timestamp()),
        ]
        return {
            "chart": {
                "result": [{
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [{
                            "open": [100.0, 200.0],
                            "high": [110.0, 210.0],
                            "low": [90.0, 190.0],
                            "close": [105.0, 205.0],
                            "volume": [1000, 2000],
                        }]
                    },
                }]
            }
        }


class YahooDateBoundaryTest(unittest.TestCase):
    @patch.object(prices_yahoo.requests, "get", return_value=_Response())
    def test_response_day_after_requested_end_is_discarded(self, _get):
        rows = prices_yahoo._fetch_yahoo(
            "1306",
            date(2026, 9, 9),
            date(2026, 9, 9),
        )
        self.assertEqual([row["date"] for row in rows], ["2026-09-09"])


if __name__ == "__main__":
    unittest.main()
