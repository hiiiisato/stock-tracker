import unittest
from datetime import date
from decimal import Decimal

from forecast_metrics import _latest_before, assess_quality, calculate_forecast_metrics


class ForecastMetricsTest(unittest.TestCase):
    def test_hulic_fixed_formula(self):
        forecast_eps, forecast_roe, shares, missing = calculate_forecast_metrics(
            126_000_000_000,
            114_334_000_000,
            Decimal("150.5"),
            Decimal("1202.76"),
        )

        self.assertIsNone(missing)
        self.assertAlmostEqual(float(shares), 759_694_352.159, places=3)
        self.assertAlmostEqual(float(forecast_eps), 165.8561758, places=6)
        self.assertAlmostEqual(float(forecast_roe), 0.1378963, places=6)

    def test_missing_inputs_are_not_filled_by_another_formula(self):
        forecast_eps, forecast_roe, shares, missing = calculate_forecast_metrics(
            10_000,
            None,
            None,
            100,
        )

        self.assertIsNone(forecast_eps)
        self.assertIsNone(shares)
        self.assertIsNone(forecast_roe)
        self.assertEqual(missing, "actual_net_income,actual_eps")

    def test_nonpositive_bps_is_not_forced_into_roe(self):
        forecast_eps, forecast_roe, shares, missing = calculate_forecast_metrics(
            100,
            100,
            10,
            -20,
        )
        self.assertEqual(forecast_eps, Decimal("10"))
        self.assertEqual(shares, Decimal("10"))
        self.assertIsNone(forecast_roe)
        self.assertEqual(missing, "actual_bps_nonpositive")

    def test_corporate_action_and_share_gap_require_review(self):
        status, flags = assess_quality(
            eligible=True,
            forecast_net_income=100,
            actual_period_end=date(2025, 3, 31),
            actual_net_income=80,
            canonical_actual_net_income=80,
            actual_eps=8,
            actual_bps=50,
            forecast_eps=10,
            forecast_roe=Decimal("0.2"),
            share_gap=Decimal("0.5"),
            equity_gap=Decimal("0.3"),
            has_corporate_action=True,
        )
        self.assertEqual(status, "needs_review")
        self.assertEqual(
            flags,
            "share_basis_mismatch,corporate_action_since_basis,canonical_equity_mismatch",
        )

    def test_no_company_guidance_is_incomplete_not_zero(self):
        status, flags = assess_quality(
            eligible=True,
            forecast_net_income=None,
            actual_period_end=date(2025, 3, 31),
            actual_net_income=80,
            canonical_actual_net_income=80,
            actual_eps=8,
            actual_bps=50,
            forecast_eps=None,
            forecast_roe=None,
            share_gap=Decimal("0.01"),
            equity_gap=Decimal("0.01"),
            has_corporate_action=False,
        )
        self.assertEqual(status, "incomplete")
        self.assertEqual(flags, "no_company_guidance")

    def test_cross_source_net_income_conflict_requires_review(self):
        status, flags = assess_quality(
            eligible=True,
            forecast_net_income=100,
            actual_period_end=date(2025, 3, 31),
            actual_net_income=-10,
            canonical_actual_net_income=1000,
            actual_eps=-1,
            actual_bps=50,
            forecast_eps=10,
            forecast_roe=Decimal("0.2"),
            share_gap=Decimal("0.01"),
            equity_gap=Decimal("0.01"),
            has_corporate_action=False,
        )
        self.assertEqual(status, "needs_review")
        self.assertIn("actual_net_income_source_mismatch", flags)

    def test_edinet_missing_net_income_is_explicit(self):
        status, flags = assess_quality(
            eligible=True,
            forecast_net_income=100,
            actual_period_end=date(2025, 3, 31),
            actual_net_income=None,
            canonical_actual_net_income=80,
            actual_eps=8,
            actual_bps=50,
            forecast_eps=None,
            forecast_roe=None,
            share_gap=None,
            equity_gap=Decimal("0.01"),
            has_corporate_action=False,
        )
        self.assertEqual(status, "incomplete")
        self.assertEqual(flags, "missing_actual_net_income,edinet_net_income_missing")

    def test_latest_actual_must_precede_forecast_year_end(self):
        rows = [
            (date(2024, 12, 31), 1, 1),
            (date(2025, 12, 31), 2, 2),
            (date(2026, 12, 31), 3, 3),
        ]
        self.assertEqual(
            _latest_before(rows, date(2026, 12, 31)),
            rows[1],
        )

    def test_latest_actual_must_not_be_from_after_announcement(self):
        rows = [
            (date(2024, 12, 31), 1, 1),
            (date(2025, 12, 31), 2, 2),
        ]
        self.assertEqual(
            _latest_before(
                rows,
                date(2026, 12, 31),
                date(2025, 5, 1),
            ),
            rows[0],
        )


if __name__ == "__main__":
    unittest.main()
