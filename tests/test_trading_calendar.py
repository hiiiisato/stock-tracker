"""取引カレンダー構築ロジックのテスト（2026-09-22 障害の再発防止）。

障害の実体: J-Quantsの取引カレンダーが契約範囲の都合で7/1以降を返さず、
9/21(敬老の日)・9/22(休日)・9/23(秋分の日)が「未登録」のまま営業日として
扱われ、Yahooの価格0件で daily_run が異常終了した。
"""
import unittest
from datetime import date

from master import _build_calendar_rows, _classify_holdiv


class ClassifyHolDivTest(unittest.TestCase):
    def test_0_and_3_are_holiday(self):
        self.assertTrue(_classify_holdiv("0", "2026-09-22"))
        self.assertTrue(_classify_holdiv("3", "2026-09-23"))  # 祝日取引ありの日も株式は休場

    def test_1_and_2_are_open(self):
        self.assertFalse(_classify_holdiv("1", "2026-09-24"))
        self.assertFalse(_classify_holdiv("2", "2026-06-01"))

    def test_unknown_division_raises(self):
        with self.assertRaises(ValueError):
            _classify_holdiv("9", "2026-09-24")


class BuildCalendarRowsTest(unittest.TestCase):
    def test_empty_jq_data_raises(self):
        with self.assertRaises(ValueError):
            _build_calendar_rows([], {})

    def test_jpx_holidays_fill_the_gap_after_jquants_range(self):
        # J-Quantsは2026-07-01までしか返らない想定（今回の実際の契約範囲）
        jq_data = [{"Date": "2026-06-30", "HolDiv": "1"}, {"Date": "2026-07-01", "HolDiv": "1"}]
        jpx_holidays = {
            date(2026, 9, 21): "敬老の日",
            date(2026, 9, 22): "休日",
            date(2026, 9, 23): "秋分の日",
        }
        rows = dict(_build_calendar_rows(jq_data, jpx_holidays))
        # 9月の祝日3日は休場
        self.assertTrue(rows["2026-09-21"])
        self.assertTrue(rows["2026-09-22"])
        self.assertTrue(rows["2026-09-23"])
        # 前の平日は営業日
        self.assertFalse(rows["2026-09-18"])
        # JPX一覧の最終日（9/23）を過ぎた日は行を作らない（9/24も含む）
        self.assertNotIn("2026-09-24", rows)
        self.assertNotIn("2026-09-25", rows)

    def test_weekends_in_the_gap_are_holiday_even_if_not_in_jpx_list(self):
        # JPXの一覧には土日は載らない（祝日名だけ）。平日ルールで別途休場にする。
        jq_data = [{"Date": "2026-07-01", "HolDiv": "1"}]
        jpx_holidays = {date(2026, 7, 10): "テスト祝日"}  # 金曜、リストの最終日をここに置く
        rows = dict(_build_calendar_rows(jq_data, jpx_holidays))
        self.assertTrue(rows["2026-07-04"])   # 土曜
        self.assertTrue(rows["2026-07-05"])   # 日曜
        self.assertFalse(rows["2026-07-06"])  # 月曜(平日・非祝日)
        self.assertTrue(rows["2026-07-10"])   # JPX掲載の祝日

    def test_jpx_fetch_failure_does_not_extend_future_rows(self):
        # jpx_holidaysが空（取得失敗）なら未来分の行を追加しない＝既存DBを壊さない
        jq_data = [{"Date": "2026-07-01", "HolDiv": "1"}]
        rows = _build_calendar_rows(jq_data, {})
        self.assertEqual(rows, [("2026-07-01", False)])

    def test_unknown_holdiv_in_jquants_data_raises(self):
        jq_data = [{"Date": "2026-07-01", "HolDiv": "9"}]
        with self.assertRaises(ValueError):
            _build_calendar_rows(jq_data, {})


if __name__ == "__main__":
    unittest.main()
