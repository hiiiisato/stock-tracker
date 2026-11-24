from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ValuationPublicationContractTest(unittest.TestCase):
    def test_same_action_digest_is_not_rejected_only_for_basis_date_difference(self):
        for name in ("app.py", "forecast_metrics.py", "valuation_metrics.py"):
            source = ROOT.joinpath(name).read_text()
            with self.subTest(name=name):
                self.assertNotIn("s.target_basis_date=p.target_basis_date", source)
                self.assertNotIn("s.target_basis_date = p.target_basis_date", source)
        for name in ("app.py", "valuation_metrics.py"):
            source = ROOT.joinpath(name).read_text()
            with self.subTest(name=name):
                self.assertNotIn("s.target_basis_date=d.target_basis_date", source)
                self.assertNotIn("s.target_basis_date = d.target_basis_date", source)

    def test_price_publication_paths_rebuild_current_valuation(self):
        for name in ("daily_run.py", "price_pipeline.py"):
            source = ROOT.joinpath(name).read_text()
            with self.subTest(name=name):
                self.assertIn("rebuild_valuation_metrics()", source)


if __name__ == "__main__":
    unittest.main()
