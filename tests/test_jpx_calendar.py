"""jpx_calendar.py のHTMLパースのテスト（JPX公式ページの構造変化を検知するため）。"""
import unittest

from jpx_calendar import _ROW_RE


class JpxCalendarParseTest(unittest.TestCase):
    def test_parses_well_formed_row(self):
        html = '<tr><td class="a-center">2026/09/23（水）</td><td class="a-center">秋分の日</td></tr>'
        rows = _ROW_RE.findall(html)
        self.assertEqual(rows, [("2026", "09", "23", "秋分の日")])

    def test_tolerates_stray_quote_seen_on_jpx_page(self):
        # 実際にJPXページで観測された崩れ: </td"><td ...> (2026/12/31行)
        html = '<tr><td class="a-center">2026/12/31（木）</td"><td class="a-center">休業日</td></tr>'
        rows = _ROW_RE.findall(html)
        self.assertEqual(rows, [("2026", "12", "31", "休業日")])

    def test_no_match_on_unrelated_html_does_not_crash(self):
        rows = _ROW_RE.findall("<html><body>no calendar here</body></html>")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
