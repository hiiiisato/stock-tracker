import unittest

from financial_metric_engine import (
    FINANCIAL_FORMULA_VERSION,
    calculate_annual_metrics,
    forecast_roe_pct,
    growth_pct,
)


class FinancialMetricEngineTests(unittest.TestCase):
    def setUp(self):
        self.current = {
            "period_end": "2026-03-31",
            "revenue": 500,
            "operating_income": 50,
            "ordinary_income": 45,
            "net_income": 40,
            "total_assets": 1_100,
            "net_assets": 420,
            "total_debt": 180,
        }
        self.prior = {
            "period_end": "2025-03-31",
            "revenue": 400,
            "operating_income": 32,
            "ordinary_income": 30,
            "net_income": 28,
            "total_assets": 900,
            "net_assets": 380,
        }

    def test_all_shared_ratios_use_one_definition(self):
        metrics = calculate_annual_metrics(self.current, self.prior)

        self.assertEqual(10.0, metrics["roe_actual_pct"])
        self.assertEqual(4.0, metrics["roa_actual_pct"])
        self.assertEqual(10.0, metrics["operating_margin_pct"])
        self.assertEqual(9.0, metrics["ordinary_margin_pct"])
        self.assertEqual(8.0, metrics["net_margin_pct"])
        self.assertEqual(38.1818, metrics["equity_ratio_pct"])
        self.assertEqual(42.8571, metrics["debt_to_equity_pct"])
        self.assertEqual(8.3333, metrics["roic_pct"])
        self.assertEqual(FINANCIAL_FORMULA_VERSION, metrics["formula_version"])
        self.assertEqual(64, len(metrics["input_digest"]))

    def test_reported_roe_uses_edinet_fraction_contract(self):
        fraction = calculate_annual_metrics({**self.current, "roe_official": 0.101}, self.prior)

        self.assertEqual(10.1, fraction["roe_actual_pct"])
        self.assertEqual("reported", fraction["roe_source"])

    def test_missing_prior_balance_has_explicit_fallback(self):
        metrics = calculate_annual_metrics(self.current)

        self.assertEqual(round(40 / 420 * 100, 4), metrics["roe_actual_pct"])
        self.assertEqual("calculated_ending_balance_fallback", metrics["roe_source"])

    def test_semantically_different_metrics_remain_separate(self):
        self.assertEqual(25.0, growth_pct(500, 400))
        self.assertEqual(12.0, forecast_roe_pct(48, 400))

    def test_input_digest_normalizes_pit_aliases(self):
        canonical = calculate_annual_metrics(self.current, self.prior)
        aliased = calculate_annual_metrics(
            {
                "period_end": "2026-03-31", "rev": 500, "opi": 50,
                "ord": 45, "ni": 40, "ta": 1_100, "teq": 420,
                "tdebt": 180,
            },
            {
                "period_end": "2025-03-31", "rev": 400, "opi": 32,
                "ord": 30, "ni": 28, "ta": 900, "teq": 380,
            },
        )

        self.assertEqual(canonical["input_digest"], aliased["input_digest"])

        changed = calculate_annual_metrics({**self.current, "net_income": 41}, self.prior)
        self.assertNotEqual(canonical["input_digest"], changed["input_digest"])


if __name__ == "__main__":
    unittest.main()
