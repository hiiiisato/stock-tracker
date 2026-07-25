import unittest

from quarter_analysis import assess_quarter


class AssessQuarterTest(unittest.TestCase):
    def test_increase_in_revenue_but_decrease_in_profit_and_slowing(self):
        previous = {
            "op": 11_910.3,
            "prev_year_op": 12_152.7,
            "yoy_op": -2.0,
        }
        latest = {
            "op": 5_694.9,
            "prev_year_op": 11_161.0,
            "yoy_rev": 1.9,
            "yoy_op": -49.0,
            "yoy_op_mgn_delta": -4.5,
        }

        result = assess_quarter(latest, previous, momentum_threshold_pct=5.0)

        self.assertEqual(result["label"], "増収減益")
        self.assertEqual(result["tone"], "mixed")
        self.assertEqual(result["momentum"], "減速")
        self.assertEqual(result["momentum_delta"], -47.0)
        self.assertIn("営業利益 -49.0%", result["description"])

    def test_turnaround_is_not_expressed_as_percentage_momentum(self):
        previous = {"op": 20, "prev_year_op": 10, "yoy_op": 100}
        latest = {
            "op": 30,
            "prev_year_op": -10,
            "yoy_rev": 5,
            "yoy_op": 400,
            "yoy_op_mgn_delta": 3,
        }

        result = assess_quarter(latest, previous)

        self.assertEqual(result["label"], "黒字転換")
        self.assertEqual(result["tone"], "positive")
        self.assertIsNone(result["momentum"])

    def test_loss_reduction(self):
        result = assess_quarter({
            "op": -20,
            "prev_year_op": -50,
            "yoy_rev": -3,
            "yoy_op": 60,
        })

        self.assertEqual(result["label"], "赤字縮小")
        self.assertEqual(result["tone"], "positive")

    def test_missing_comparison_data_falls_back_safely(self):
        result = assess_quarter({"op": 100})

        self.assertEqual(result["label"], "判定材料不足")
        self.assertEqual(result["tone"], "neutral")
        self.assertIsNone(result["momentum"])


if __name__ == "__main__":
    unittest.main()
