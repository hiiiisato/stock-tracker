import unittest
from datetime import date

from stock_listings import (
    normalize_jpx_code,
    parse_issues_csv,
    parse_new_listings_html,
)


class StockListingsTest(unittest.TestCase):
    def test_normalizes_jpx_five_character_codes(self):
        self.assertEqual(normalize_jpx_code("30030"), "3003")
        self.assertEqual(normalize_jpx_code("130A0"), "130A")
        self.assertEqual(normalize_jpx_code("584A"), "584A")
        self.assertIsNone(normalize_jpx_code("13050T"))

    def test_parses_official_csv_and_excludes_etf(self):
        text = "\n".join(
            [
                '"更新日","銘柄コード","銘柄名称","国内外区分","商品分類","市場区分","東証上場日","東証上場廃止日"',
                '"2026-11-21","30030","ヒューリック","国内","株式","プライム","1949-05-16",""',
                '"2026-11-21","13050","ETF","国内","ETF","その他","2001-07-13",""',
            ]
        )
        rows = parse_issues_csv(text.encode("cp932"))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "3003")
        self.assertEqual(rows[0][2], date(1949, 5, 16))
        self.assertEqual(rows[0][6], "プライム")

    def test_new_listing_delta_uses_only_dates_already_reached(self):
        html = b"""
        <table>
          <tr><th>\xe4\xb8\x8a\xe5\xa0\xb4\xe6\x97\xa5</th><th>\xe4\xbc\x9a\xe7\xa4\xbe\xe5\x90\x8d</th><th>\xe3\x82\xb3\xe3\x83\xbc\xe3\x83\x89</th></tr>
          <tr><td>2026/11/20<br>(2026/10/26)</td><td>HiJoJo Partners</td><td>657A</td></tr>
          <tr><td>2026/12/01<br>(2026/11/01)</td><td>Future</td><td>700A</td></tr>
        </table>
        """
        rows = parse_new_listings_html(html, as_of=date(2026, 11, 21))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "657A")
        self.assertEqual(rows[0][2], date(2026, 11, 20))


if __name__ == "__main__":
    unittest.main()
