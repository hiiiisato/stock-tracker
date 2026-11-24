from datetime import date, datetime
from decimal import Decimal as D
import sqlite3
import unittest

from dividend_metrics import _normalize_notice, extract_tdnet_notices
from financials_tdnet import _Summary
from per_share_metrics import resolve_applied_actions
from price_adjustments import Action, actions_digest, stable_action_id
from price_store import _resolve_duplicate_pending_actions
from valuation_metrics import _validate_current_shares


def action(code, ex_date, after, *, kind="split", status="confirmed"):
    item = Action(code, ex_date, D(1), D(str(after)), kind=kind, status=status,
                  record_date=ex_date)
    return Action(**{**vars(item), "action_id": stable_action_id(item)})


class PerShareNormalizationTest(unittest.TestCase):
    def test_daiwa_house_applies_only_the_new_split(self):
        split = action("1925", date(2026, 9, 29), 2)
        applied, implied, quality, _ = resolve_applied_actions(
            numerator=350_568_000_000, raw_value=D("566.47"),
            current_shares=1_239_080_524, actions=[split],
        )
        self.assertEqual(quality, "verified")
        self.assertEqual([a.action_id for a in applied], [split.action_id])
        self.assertAlmostEqual(float(implied * 2), 1_237_728_388, delta=2)

    def test_already_retrospectively_adjusted_value_is_not_adjusted_twice(self):
        split = action("2801", date(2024, 3, 28), 5)
        applied, _implied, quality, _ = resolve_applied_actions(
            numerator=26_827_000_000, raw_value=D("27.95"),
            current_shares=926_834_697, actions=[split],
        )
        self.assertEqual(quality, "verified")
        self.assertEqual(applied, [])

    def test_two_actions_use_only_the_unreflected_suffix_and_replay_is_identical(self):
        first = action("4062", date(2025, 12, 29), 2)
        second = action("4062", date(2026, 9, 29), 2)
        kwargs = dict(numerator=63_713_000_000, raw_value=D("228.16"),
                      current_shares=570_655_095, actions=[first, second])
        one = resolve_applied_actions(**kwargs)
        two = resolve_applied_actions(**kwargs)
        self.assertEqual(one, two)
        self.assertEqual(one[2], "verified")
        self.assertEqual([a.action_id for a in one[0]], [second.action_id])

    def test_unresolved_basis_stays_null_quality_instead_of_using_another_formula(self):
        split = action("9999", date(2026, 9, 29), 2)
        applied, _implied, quality, reason = resolve_applied_actions(
            numerator=100, raw_value=10, current_shares=1_000_000, actions=[split],
        )
        self.assertEqual(applied, [])
        self.assertEqual(quality, "needs_review")
        self.assertIn("unresolved", reason)

    def test_missing_current_shares_does_not_block_a_period_after_all_actions(self):
        old_split = action("2801", date(2024, 3, 28), 5)
        applied, _implied, quality, reason = resolve_applied_actions(
            numerator=26_827_000_000, raw_value=D("27.95"), current_shares=None,
            actions=[old_split], source_basis_date=date(2025, 3, 31),
        )
        self.assertEqual(applied, [])
        self.assertEqual(quality, "verified")
        self.assertEqual(reason, "no_post_basis_corporate_action")


class CorporateActionIdentityTest(unittest.TestCase):
    def test_source_name_does_not_change_digest(self):
        left = action("2814", date(2025, 12, 29), D("1.1"), kind="bonus")
        right = Action(**{**vars(left), "kind": "split", "source": "jquants"})
        self.assertEqual(actions_digest([left], date(2026, 1, 1)),
                         actions_digest([right], date(2026, 1, 1)))

    def test_same_day_different_economic_terms_have_different_ids(self):
        left = action("9999", date(2026, 9, 29), 2)
        right = action("9999", date(2026, 9, 29), 3)
        self.assertNotEqual(left.action_id, right.action_id)

    def test_cancellation_changes_digest(self):
        confirmed = action("9999", date(2026, 9, 29), 2)
        cancelled = Action(**{**vars(confirmed), "status": "cancelled"})
        self.assertNotEqual(actions_digest([confirmed], date(2026, 10, 1)),
                            actions_digest([cancelled], date(2026, 10, 1)))

    def test_date_only_change_keeps_digest_until_an_action_takes_effect(self):
        split = action("9999", date(2026, 12, 1), 2)
        before = actions_digest([split], date(2026, 11, 23))
        next_day = actions_digest([split], date(2026, 11, 24))
        after_action = actions_digest([split], date(2026, 12, 1))
        self.assertEqual(before, next_day)
        self.assertNotEqual(before, after_action)

    def test_resolved_schedule_placeholder_is_removed_but_distinct_events_remain(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript("""
            CREATE TABLE corporate_actions (
              action_id TEXT PRIMARY KEY,code TEXT,ex_date TEXT,kind TEXT,
              old_shares NUMERIC,new_shares NUMERIC,record_date TEXT,status TEXT
            );
            CREATE TABLE corporate_action_observations (
              observation_id TEXT PRIMARY KEY,action_id TEXT
            );
            INSERT INTO corporate_actions VALUES
              ('confirmed','6861','2026-11-19','split',1,5,'2026-11-20','confirmed'),
              ('placeholder','6861','2026-11-19','split',1,1,'2026-11-20','needs_ratio'),
              ('other-kind','6861','2026-11-19','rights',1,1,'2026-11-20','needs_ratio'),
              ('other-record','6861','2026-11-19','split',1,1,'2026-11-21','needs_ratio');
            INSERT INTO corporate_action_observations VALUES ('obs','placeholder');
        """)

        class Cursor:
            def __init__(self, conn):
                self.cursor = conn.cursor()

            def execute(self, sql, params=()):
                self.cursor.execute(sql.replace("%s", "?"), params)

            def fetchall(self):
                return self.cursor.fetchall()

        cur = Cursor(connection)
        _resolve_duplicate_pending_actions(cur)
        rows = connection.execute(
            "SELECT action_id FROM corporate_actions ORDER BY action_id"
        ).fetchall()
        self.assertEqual(rows, [("confirmed",), ("other-kind",), ("other-record",)])
        self.assertEqual(connection.execute(
            "SELECT action_id FROM corporate_action_observations WHERE observation_id='obs'"
        ).fetchone()[0], "confirmed")
        connection.close()


class ValuationShareBasisTest(unittest.TestCase):
    def test_normal_current_vs_average_share_drift_is_accepted(self):
        verified, gap = _validate_current_shares(164_598_490, 173_100_000)
        self.assertTrue(verified)
        self.assertLess(gap, D("0.15"))

    def test_split_scale_mismatch_is_rejected(self):
        verified, gap = _validate_current_shares(100_000_000, 200_000_000)
        self.assertFalse(verified)
        self.assertIsNone(gap)


class DividendNoticeTest(unittest.TestCase):
    def test_tdnet_components_are_kept_separately_and_normalized_before_sum(self):
        html = """
        <html><body>
          <ix:nonnumeric name="tse-ed-t:SecuritiesCode" contextref="meta">19250</ix:nonnumeric>
          <ix:nonnumeric name="tse-ed-t:FiscalYearEnd" contextref="meta">2027-03-31</ix:nonnumeric>
          <xbrli:context id="CurrentYearDuration_SecondQuarterMember_NonConsolidatedMember_ResultMember">
            <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2027-03-31</xbrli:endDate></xbrli:period>
          </xbrli:context>
          <xbrli:context id="CurrentYearDuration_YearEndMember_NonConsolidatedMember_ForecastMember">
            <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2027-03-31</xbrli:endDate></xbrli:period>
          </xbrli:context>
          <ix:nonfraction name="tse-ed-t:DividendPerShare" contextref="CurrentYearDuration_SecondQuarterMember_NonConsolidatedMember_ResultMember">86</ix:nonfraction>
          <ix:nonfraction name="tse-ed-t:DividendPerShare" contextref="CurrentYearDuration_YearEndMember_NonConsolidatedMember_ForecastMember">47</ix:nonfraction>
        </body></html>
        """
        rows = extract_tdnet_notices(
            _Summary(html), datetime(2026, 11, 11, 15, 30), "https://example.test/xbrl.zip"
        )
        self.assertEqual({(r[3], r[4], r[5]) for r in rows},
                         {("interim", "result", D(86)), ("final", "forecast", D(47))})
        split = action("1925", date(2026, 9, 29), 2)
        applied, status, _ = _normalize_notice(D(86), date(2026, 11, 11), "result",
                                               [split], [D(43)])
        self.assertEqual(status, "verified")
        self.assertEqual(len(applied), 1)
        applied, status, _ = _normalize_notice(D(47), date(2026, 11, 11), "forecast",
                                               [split], [D(43)])
        self.assertEqual(status, "verified")
        self.assertEqual(applied, [])
        self.assertEqual(D(86) * split.price_factor + D(47), D(90))

    def test_forecast_during_announced_but_not_effective_split_stays_unpublished(self):
        future = action("9999", date(2027, 1, 4), 2)
        future = Action(**{**vars(future), "announced_at": date(2026, 10, 1)})
        applied, quality, reason = _normalize_notice(
            D(50), date(2026, 11, 1), "forecast", [future], [],
            target_basis_date=date(2026, 11, 22),
        )
        self.assertEqual(applied, [])
        self.assertEqual(quality, "needs_review")
        self.assertIn("ambiguous", reason)

    def test_forecast_before_split_announcement_is_adjusted_only_after_ex_date(self):
        future = action("9999", date(2027, 1, 4), 2)
        future = Action(**{**vars(future), "announced_at": date(2026, 12, 1)})
        before = _normalize_notice(
            D(100), date(2026, 11, 1), "forecast", [future], [],
            target_basis_date=date(2026, 11, 22),
        )
        after = _normalize_notice(
            D(100), date(2026, 11, 1), "forecast", [future], [],
            target_basis_date=date(2027, 1, 4),
        )
        self.assertEqual(before[0], [])
        self.assertEqual(before[1], "verified")
        self.assertEqual(after[0], [future])
        self.assertEqual(after[1], "verified")


if __name__ == "__main__":
    unittest.main()
