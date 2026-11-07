"""実際に発生した遅延取得・二重補正と、公式情報の確定を検証する。"""
from datetime import date
from decimal import Decimal as D
import unittest

from corporate_actions import parse_disclosure_ratio
from price_adjustments import Action, PriceBasisError, factors, identify_basis, normalize


def candle(dt, close, volume=300):
    return dict(code="285A", date=dt, open=close, high=close, low=close, close=close, volume=volume)


class PriceAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.basis = date(2026, 10, 30)
        self.action = Action("285A", date(2026, 9, 29), D(1), D(3))

    def test_kioxia_delayed_fetch_uses_same_day_anchor_not_ex_day_jump(self):
        rows = [candle("2026-09-25", 55780 / 3), candle("2026-09-28", 17780), candle("2026-09-29", 17880)]
        basis = identify_basis(rows, [self.action], self.basis, {"2026-09-25": D(55780)})
        result = normalize(rows, [self.action], self.basis, basis)
        self.assertEqual(basis, "split_adjusted")
        self.assertEqual(result[1]["close"], D("53340"))
        self.assertEqual(result[1]["adj_close"], D("17780"))
        self.assertEqual(result[1]["change_pct"], D("-4.3743"))
        self.assertEqual(result[2]["change_pct"], D("0.5624"))

    def test_raw_and_already_adjusted_responses_produce_identical_price_and_volume(self):
        raw = [candle("2026-09-28", 6000, 100), candle("2026-09-29", 2100, 330)]
        adjusted = [candle("2026-09-28", 2000, 300), candle("2026-09-29", 2100, 330)]
        expected = normalize(raw, [self.action], self.basis, "raw")
        self.assertEqual(expected, normalize(adjusted, [self.action], self.basis, "split_adjusted"))
        self.assertEqual(expected[0]["volume"], 100)
        self.assertEqual(expected[0]["adj_volume"], D(300))
        self.assertEqual(expected[0]["turnover"], 600000)
        self.assertEqual(expected, normalize(raw, [self.action], self.basis, "raw"))

    def test_price_and_volume_basis_can_differ(self):
        rows = [candle("2026-09-28", 2000, 100)]
        result = normalize(rows, [self.action], self.basis, "split_adjusted", volume_basis="raw")
        self.assertEqual(result[0]["volume"], 100)
        self.assertEqual(result[0]["adj_volume"], D(300))

    def test_multiple_actions_reverse_split_and_future_schedule(self):
        actions = [self.action,
                   Action("285A", date(2026, 10, 20), D(10), D(1), "consolidation"),
                   Action("285A", date(2026, 12, 1), D(1), D(5))]
        price, volume = factors(date(2026, 9, 28), actions, self.basis)
        self.assertAlmostEqual(price, D(10) / 3)
        self.assertEqual(volume, D("0.3"))
        price, _ = factors(date(2026, 9, 29), actions, date(2026, 9, 29))
        self.assertEqual(price, 1)

    def test_small_official_split_is_not_rejected_because_move_is_small(self):
        action = Action("285A", date(2026, 9, 29), D(10), D(11))
        result = normalize([candle("2026-09-28", 1100)], [action], self.basis, "raw")
        self.assertEqual(result[0]["adj_close"], D(1000))

    def test_unconfirmed_basis_or_conflicting_same_day_prices_are_held(self):
        rows = [candle("2026-09-28", 2000)]
        with self.assertRaises(PriceBasisError):
            identify_basis(rows, [self.action], self.basis, {})
        with self.assertRaises(PriceBasisError):
            identify_basis(rows, [self.action], self.basis, {"2026-09-28": D(7000)})
        pending = Action("285A", date(2026, 9, 29), D(1), D(1), status="needs_ratio")
        with self.assertRaises(PriceBasisError):
            normalize(rows, [pending], self.basis, "raw")

    def test_real_crash_without_official_split_is_preserved(self):
        rows = [candle("2026-09-28", 6000), candle("2026-09-29", 1800)]
        result = normalize(rows, [], self.basis, "raw")
        self.assertEqual(result[1]["change_pct"], D(-70))

    def test_rights_adjusts_price_but_not_volume_and_cash_dividend_is_not_added(self):
        action = Action("285A", date(2026, 9, 29), D(1), D(2), "rights", price_ratio=D("0.8"))
        result = normalize([candle("2026-09-28", 1000, 100)], [action], self.basis, "raw")
        self.assertEqual(result[0]["adj_close"], D(800))
        self.assertEqual(result[0]["adj_volume"], D(100))

    def test_official_disclosure_requires_matching_record_date_and_unique_ratio(self):
        pending = Action("285A", date(2026, 9, 29), D(1), D(1), status="needs_ratio", record_date=date(2026, 9, 30))
        text = "2026年９月30日を基準日として当社普通株式を、１株につき３株の割合をもって分割します。"
        parsed = parse_disclosure_ratio(text, pending)
        self.assertEqual(parsed.price_factor, D(1) / 3)
        self.assertEqual(parsed.status, "confirmed")
        with self.assertRaises(PriceBasisError):
            parse_disclosure_ratio(text.replace("９月", "８月"), pending)
        with self.assertRaises(PriceBasisError):
            parse_disclosure_ratio(text + "1株につき5株の割合とします。", pending)


if __name__ == "__main__":
    unittest.main()
