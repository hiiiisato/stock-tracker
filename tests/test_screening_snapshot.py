import unittest
import json
import zlib
from datetime import date

from compute_price_stats import STAT_COLS
from compute_stats_history import (
    EXTRA_COLS,
    HISTORY_COLS,
    _missing_formula_snapshot_dates,
    _pit_extra,
    _pit_fm,
)
from financial_metric_engine import FINANCIAL_FORMULA_VERSION
from screening_snapshot import matches_conditions, publish_snapshot, serialize_history_rows


class _MetadataCursor:
    def execute(self, _sql, _params=None):
        return None

    def fetchall(self):
        return [("7203", "トヨタ自動車", 1)]


class _PublishCursor:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return None


class _FormulaHistoryCursor:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    def execute(self, sql, params=None):
        self.statement = (sql, params)

    def fetchall(self):
        return self.rows


class ScreeningSnapshotTest(unittest.TestCase):
    def _history_row(self, **overrides):
        values = {column: None for column in HISTORY_COLS}
        values.update({
            "code": "7203",
            "snapshot_date": date(2026, 11, 20),
            "market": "プライム",
            "close": 3312,
            "ma25": 3200,
            "ma50": 3150,
            "ma75": 3086.1867,
            "per": 10.2,
            "pbr": 0.98,
            "roe": 10.1,
            "div_yield": 3.02,
            "market_cap": 50_000_000_000_000,
            "cf_positive": 1,
            "turnover_20d": 962.95,
            "dev_ma75": 7.32,
        })
        values.update(overrides)
        return tuple(values[column] for column in HISTORY_COLS)

    def test_common_serializer_produces_high_dividend_value_input(self):
        stocks = serialize_history_rows(
            _MetadataCursor(),
            [self._history_row(dev_ma75=-20)],
            HISTORY_COLS,
        )

        self.assertEqual(1, len(stocks))
        self.assertTrue(matches_conditions(stocks[0]))
        self.assertEqual(-20, stocks[0]["dev_ma75"])
        self.assertAlmostEqual(5.14, stocks[0]["dev_ma50"], places=2)

    def test_roe_uses_average_equity_like_stock_page(self):
        latest = {
            "teq": 399_188.5,
            "ta": 1_055_223.3,
            "ni": 38_481.0,
            "opi": 37_662.2,
            "rev": 506_849.5,
            "tdebt": 0,
        }
        prior = {
            "teq": 359_248.3,
            "ta": 936_013.5,
            "ni": 47_650.9,
            "opi": 47_955.9,
            "rev": 480_367.0,
            "tdebt": 0,
        }

        metrics = _pit_extra([latest, prior], 3312, 295.25, 100, 130.3)

        self.assertEqual(10.15, metrics["roe"])

    def test_reported_roe_wins_over_calculated_roe(self):
        latest = {
            "teq": 399_188.5, "ta": 1_055_223.3, "ni": 38_481.0,
            "opi": 37_662.2, "rev": 506_849.5, "tdebt": 0,
            "roe_official": 0.101,
        }
        prior = {
            "teq": 359_248.3, "ta": 936_013.5, "ni": 47_650.9,
            "opi": 47_955.9, "rev": 480_367.0, "tdebt": 0,
        }

        metrics = _pit_extra([latest, prior], 3312, 295.25, 100, 130.3)

        self.assertEqual(10.1, metrics["roe"])

    def test_precomputed_engine_metrics_keep_net_margin(self):
        latest = {
            "teq": 400, "ta": 1_000, "ni": 40, "opi": 50, "rev": 500,
            "tdebt": 100, "formula_version": FINANCIAL_FORMULA_VERSION,
            "roe_actual_pct": 10.0, "roa_actual_pct": 4.0,
            "operating_margin_pct": 10.0, "net_margin_pct": 8.0,
            "debt_to_equity_pct": 25.0,
        }

        metrics = _pit_extra([latest], 1_000, 100, 30, 100)

        self.assertEqual(8.0, metrics["net_margin"])

    def test_unknown_cashflow_is_not_treated_as_negative(self):
        fm = _pit_fm([{
            "rev": 100,
            "opi": 10,
            "ord": 9,
            "ni": 7,
            "ta": 200,
            "teq": 100,
            "tdebt": 20,
            "cfo": None,
        }], 10)

        self.assertIsNone(fm["cf_positive"])
        stocks = serialize_history_rows(
            _MetadataCursor(),
            [self._history_row(cf_positive=None)],
            HISTORY_COLS,
        )
        self.assertIsNone(stocks[0]["cf_positive"])
        self.assertFalse(matches_conditions(stocks[0]))

    def test_history_schema_is_still_single_ordered_contract(self):
        self.assertEqual(
            ["code", "snapshot_date"] + STAT_COLS + EXTRA_COLS
            + ["market", "source_series_revision", "financial_formula_version"],
            HISTORY_COLS,
        )

    def test_formula_rebuild_targets_only_published_dates_without_current_version(self):
        missing = _FormulaHistoryCursor([
            (date(2026, 7, 7),),
            (date(2026, 7, 8),),
        ])
        complete = _FormulaHistoryCursor([])

        self.assertEqual(
            [date(2026, 7, 7), date(2026, 7, 8)],
            _missing_formula_snapshot_dates(missing),
        )
        self.assertEqual([], _missing_formula_snapshot_dates(complete))
        self.assertIn("status='ready'", missing.statement[0])

    def test_publish_is_one_atomic_ready_row(self):
        cursor = _PublishCursor()
        stock = serialize_history_rows(
            _MetadataCursor(),
            [self._history_row()],
            HISTORY_COLS,
        )[0]

        snapshot_id = publish_snapshot(cursor, date(2026, 11, 20), [stock])

        self.assertEqual(64, len(snapshot_id))
        insert_sql, params = cursor.statements[-1]
        self.assertIn("'ready'", insert_sql)
        self.assertNotIn("building", insert_sql)
        self.assertEqual(FINANCIAL_FORMULA_VERSION, params[5])
        stored = json.loads(zlib.decompress(params[6]))
        self.assertEqual("7203", stored[0]["code"])


if __name__ == "__main__":
    unittest.main()
