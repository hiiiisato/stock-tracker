import unittest
from datetime import date
from decimal import Decimal

from price_alerts import build_message, reached


class PriceAlertRulesTest(unittest.TestCase):
    def test_upper_and_lower_include_exact_target(self):
        self.assertTrue(reached("upper", Decimal("3000"), Decimal("3000")))
        self.assertTrue(reached("upper", Decimal("3100"), Decimal("3000")))
        self.assertFalse(reached("upper", Decimal("2999"), Decimal("3000")))
        self.assertTrue(reached("lower", Decimal("2500"), Decimal("2500")))
        self.assertTrue(reached("lower", Decimal("2400"), Decimal("2500")))
        self.assertFalse(reached("lower", Decimal("2501"), Decimal("2500")))

    def test_message_keeps_link_when_many_alerts_are_batched(self):
        rows = [
            (i, "7203", "トヨタ自動車" * 30, "upper", 3000, 3100, date(2026, 9, 18))
            for i in range(100)
        ]
        message = build_message(rows, "https://example.com")
        self.assertLessEqual(len(message), 5000)
        self.assertIn("ほか", message)
        self.assertTrue(message.endswith("https://example.com/watchlist"))


if __name__ == "__main__":
    unittest.main()
