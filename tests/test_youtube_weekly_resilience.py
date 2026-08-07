import sys
import types
import unittest
from datetime import date
from unittest.mock import patch


def _import_youtube_insights():
    """外部API/DB設定を読まずに純粋な再試行ロジックだけをテストする。"""
    previous_config = sys.modules.get("config")
    previous_aliases = sys.modules.get("stock_aliases")
    previous_requests = sys.modules.get("requests")
    config_stub = types.ModuleType("config")
    config_stub.get_conn = None
    config_stub.GEMINI_API_KEY = "test"
    aliases_stub = types.ModuleType("stock_aliases")
    aliases_stub.normalize = lambda value: str(value or "")
    aliases_stub.resolve = lambda _cur, _name: ""
    sys.modules["config"] = config_stub
    sys.modules["stock_aliases"] = aliases_stub
    sys.modules["requests"] = types.ModuleType("requests")
    try:
        import youtube_insights
        return youtube_insights
    finally:
        if previous_config is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous_config
        if previous_aliases is None:
            sys.modules.pop("stock_aliases", None)
        else:
            sys.modules["stock_aliases"] = previous_aliases
        if previous_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = previous_requests


yt = _import_youtube_insights()


class WeeklyReportResilienceTest(unittest.TestCase):
    def test_invalid_json_is_regenerated(self):
        responses = iter([
            types.SimpleNamespace(text="JSONではありません"),
            types.SimpleNamespace(text='{"summary":"ok"}'),
        ])
        json_modes = []

        def fake_generate(_client, _contents, _retries=3, *, json_mode=False):
            json_modes.append(json_mode)
            return next(responses)

        with patch.object(yt, "_gen_with_retry", side_effect=fake_generate), \
                patch.object(yt.time, "sleep"):
            result = yt._generate_json_with_retry(None, "prompt", label="test")

        self.assertEqual(result, {"summary": "ok"})
        self.assertEqual(json_modes, [True, True])

    def test_invalid_json_exhaustion_is_explicit_failure(self):
        response = types.SimpleNamespace(text="invalid")
        with patch.object(yt, "_gen_with_retry", return_value=response) as generate, \
                patch.object(yt.time, "sleep"):
            result = yt._generate_json_with_retry(
                None, "prompt", label="test", parse_retries=2
            )

        self.assertIsNone(result)
        self.assertEqual(generate.call_count, 3)

    def test_latest_sunday(self):
        self.assertEqual(yt._latest_sunday(date(2026, 8, 3)), date(2026, 8, 2))
        self.assertEqual(yt._latest_sunday(date(2026, 8, 2)), date(2026, 8, 2))


if __name__ == "__main__":
    unittest.main()
