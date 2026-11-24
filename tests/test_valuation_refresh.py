from contextlib import contextmanager
import unittest
from unittest.mock import patch

import valuation_metrics


class _Cursor:
    def __init__(self, *, updated=0, rebuild_codes=()):
        self.updated = updated
        self.rebuild_codes = list(rebuild_codes)
        self.rowcount = 0
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, list(params)))
        self.rowcount = self.updated if sql.lstrip().startswith("UPDATE") else 0

    def fetchall(self):
        return [(code,) for code in self.rebuild_codes]


class ValuationPriceRefreshTest(unittest.TestCase):
    def _run(self, cursor, codes=None, rebuilt=0):
        @contextmanager
        def fake_db():
            yield cursor

        with (
            patch.object(valuation_metrics, "db", fake_db),
            patch.object(valuation_metrics, "ensure_schema"),
            patch.object(valuation_metrics, "bump_generation") as bump,
            patch.object(
                valuation_metrics,
                "rebuild_valuation_metrics",
                return_value=rebuilt,
            ) as rebuild,
        ):
            result = valuation_metrics.refresh_valuation_prices(codes)
        return result, bump, rebuild

    def test_normal_day_is_one_set_update_without_financial_joins(self):
        cursor = _Cursor(updated=4_190)
        result, bump, rebuild = self._run(cursor)
        update_sql = cursor.calls[0][0]
        self.assertEqual(result, 4_190)
        self.assertIn("UPDATE valuation_metrics_current", update_sql)
        self.assertNotIn("per_share_fact", update_sql)
        bump.assert_called_once()
        rebuild.assert_not_called()

    def test_only_missing_or_digest_changed_codes_are_fully_rebuilt(self):
        cursor = _Cursor(updated=12, rebuild_codes=("1926", "7203"))
        result, _bump, rebuild = self._run(cursor, ["1926", "7203"], rebuilt=2)
        self.assertEqual(result, 14)
        rebuild.assert_called_once_with(["1926", "7203"])
        self.assertTrue(all("s.code IN" in sql for sql, _params in cursor.calls))


if __name__ == "__main__":
    unittest.main()
