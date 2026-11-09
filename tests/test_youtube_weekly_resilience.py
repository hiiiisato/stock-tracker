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

        def fake_generate(_client, _contents, _retries=3, *, json_mode=False,
                          json_schema=None):
            json_modes.append(json_mode)
            self.assertIs(json_schema, yt.AGGREGATE_JSON_SCHEMA)
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

    def test_gemini_client_pins_v1beta_api(self):
        captured = {}
        google_stub = types.ModuleType("google")
        genai_stub = types.ModuleType("google.genai")
        genai_types_stub = types.ModuleType("google.genai.types")

        class HttpOptions:
            def __init__(self, *, api_version):
                self.api_version = api_version

        def client(**kwargs):
            captured.update(kwargs)
            return object()

        genai_stub.Client = client
        genai_stub.types = genai_types_stub
        genai_types_stub.HttpOptions = HttpOptions
        google_stub.genai = genai_stub

        with patch.dict(sys.modules, {
            "google": google_stub,
            "google.genai": genai_stub,
            "google.genai.types": genai_types_stub,
        }):
            yt._gemini()

        self.assertEqual(captured["api_key"], "test")
        self.assertEqual(captured["http_options"].api_version, "v1beta")

    def test_json_generation_uses_explicit_schema(self):
        captured = {}
        genai_types_stub = types.ModuleType("google.genai.types")

        class GenerateContentConfig:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class MediaResolution:
            MEDIA_RESOLUTION_LOW = "low"

        genai_types_stub.GenerateContentConfig = GenerateContentConfig
        genai_types_stub.MediaResolution = MediaResolution
        google_stub = types.ModuleType("google")
        google_genai_stub = types.ModuleType("google.genai")
        google_genai_stub.types = genai_types_stub
        google_stub.genai = google_genai_stub

        class Models:
            @staticmethod
            def generate_content(*, model, contents, config):
                captured.update({
                    "model": model,
                    "contents": contents,
                    "config": config,
                })
                return types.SimpleNamespace(text='{"summary":"ok"}')

        client = types.SimpleNamespace(models=Models())
        with patch.dict(sys.modules, {
            "google": google_stub,
            "google.genai": google_genai_stub,
            "google.genai.types": genai_types_stub,
        }):
            yt._gen_with_retry(
                client,
                "prompt",
                json_mode=True,
                json_schema=yt.AGGREGATE_JSON_SCHEMA,
            )

        self.assertEqual(captured["model"], yt.GEMINI_MODEL)
        self.assertEqual(captured["config"].response_mime_type, "application/json")
        self.assertIs(
            captured["config"].response_json_schema,
            yt.AGGREGATE_JSON_SCHEMA,
        )

    def test_preflight_rejects_incompatible_json_mode(self):
        with patch.object(
            yt,
            "_generate_json_with_retry",
            side_effect=RuntimeError("JSON mode is not enabled"),
        ):
            self.assertFalse(yt._gemini_json_preflight(object()))

    def test_latest_sunday(self):
        self.assertEqual(yt._latest_sunday(date(2026, 8, 3)), date(2026, 8, 2))
        self.assertEqual(yt._latest_sunday(date(2026, 8, 2)), date(2026, 8, 2))

    def test_weekly_run_uses_one_jst_sunday_key_for_save_and_notify(self):
        class Cursor:
            def close(self):
                pass

        class Connection:
            def __init__(self):
                self.commits = 0

            def cursor(self):
                return Cursor()

            def commit(self):
                self.commits += 1

            def close(self):
                pass

        seen = {"aggregate": [], "notify": []}

        def aggregate(_cur, _client, week_end):
            seen["aggregate"].append(week_end)
            return True

        def notify(_cur, week_end):
            seen["notify"].append(week_end)
            return True

        empty_stats = {"videos_found": 0, "analyzed": 0, "failed": 0, "skipped": 0}
        with patch.object(yt, "get_conn", return_value=Connection()), \
                patch.object(yt, "ensure_tables"), \
                patch.object(yt, "_gemini", return_value=object()), \
                patch.object(yt, "_gemini_json_preflight", return_value=True), \
                patch.object(yt, "_crawl_and_analyze", return_value=empty_stats), \
                patch.object(yt, "jst_today", return_value=date(2026, 9, 7)), \
                patch.object(yt, "aggregate_weekly", side_effect=aggregate), \
                patch.object(yt, "notify_weekly", side_effect=notify):
            result = yt.run_weekly(verbose=False)

        self.assertTrue(result["report_generated"])
        self.assertTrue(result["notified"])
        self.assertEqual(seen["aggregate"], [date(2026, 9, 6)])
        self.assertEqual(seen["notify"], [date(2026, 9, 6)])


if __name__ == "__main__":
    unittest.main()
