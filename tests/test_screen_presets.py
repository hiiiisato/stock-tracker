import re
import unittest
from pathlib import Path

from screening_snapshot import HIGH_DIVIDEND_VALUE_CONDITIONS, SCREENING_CORE_FIELDS


APP_SOURCE = Path(__file__).resolve().parents[1].joinpath("app.py").read_text()


class ScreenPresetDefinitionTest(unittest.TestCase):
    def _block(self, start: str, end: str) -> str:
        return APP_SOURCE.split(start, 1)[1].split(end, 1)[0]

    def test_every_preset_condition_has_an_input_mapping(self):
        presets = self._block("var STRATS=[", "var COL={{")
        mapping = self._block("var IMAP={{", "\n  }};")

        condition_fields = set(re.findall(r"\{\{f:'([^']+)'", presets))
        mapped_fields = set(re.findall(r"^\s*'([^']+)':", mapping, re.MULTILINE))

        self.assertEqual(set(), condition_fields - mapped_fields)

    def test_every_preset_uses_the_common_snapshot(self):
        presets = self._block("var STRATS=[", "var COL={{")
        condition_fields = set(re.findall(r"\{\{f:'([^']+)'", presets))

        self.assertEqual(set(), condition_fields - SCREENING_CORE_FIELDS)

    def test_high_dividend_value_rule_matches_server_verification(self):
        first = self._block("{{name:'① 高配当バリュー'", "{{name:'② 高ROEグロース'")
        for field, operator, value in HIGH_DIVIDEND_VALUE_CONDITIONS:
            self.assertIn(f"f:'{field}',op:'{operator}',val:{value}", first)
        self.assertNotIn("f:'dev_ma75'", first)

    def test_last_preset_is_not_excluded_by_a_hard_coded_index(self):
        self.assertIn(
            "if(stratIdx>=0&&stratIdx<STRATS.length){{",
            APP_SOURCE,
        )
        self.assertNotIn("if(stratIdx>=0&&stratIdx<=14){{", APP_SOURCE)


if __name__ == "__main__":
    unittest.main()
