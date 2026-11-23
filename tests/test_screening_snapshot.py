import unittest
import json
import zlib
from datetime import date

from compute_price_stats import STAT_COLS
from compute_stats_history import EXTRA_COLS, HISTORY_COLS, _pit_fm
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
            [self._history_row()],
            HISTORY_COLS,
        )

        self.assertEqual(1, len(stocks))
        self.assertTrue(matches_conditions(stocks[0]))
        self.assertEqual(7.32, stocks[0]["dev_ma75"])
        self.assertAlmostEqual(5.14, stocks[0]["dev_ma50"], places=2)

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
            ["code", "snapshot_date"] + STAT_COLS + EXTRA_COLS + ["market", "source_series_revision"],
            HISTORY_COLS,
        )

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
        stored = json.loads(zlib.decompress(params[5]))
        self.assertEqual("7203", stored[0]["code"])


if __name__ == "__main__":
    unittest.main()
