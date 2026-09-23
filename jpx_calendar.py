"""JPX公式サイトの「休業日一覧」から将来の休場日を取得する。

J-Quants の取引カレンダーAPIは無料枠の窓（過去〜契約範囲まで）しか返さず、
数ヶ月先の祝日・年末年始が欠落する期間が生じる（2026-09-22 daily_run 障害の原因）。
JPXは翌々年分まで休業日を公式掲載しているため、未来分の穴埋めに使う。
https://www.jpx.co.jp/corporate/about-jpx/calendar/index.html
"""
from __future__ import annotations

import re
from datetime import date

import requests

JPX_CALENDAR_URL = "https://www.jpx.co.jp/corporate/about-jpx/calendar/index.html"

# JPXページはたまに `</td">` のような余分な引用符が混じるため（実例: 2026/12/31行）、
# 終了タグの直後は緩く許容する。
_ROW_RE = re.compile(
    r'<td class="a-center">(\d{4})/(\d{2})/(\d{2})[^<]*</td["\']?>\s*'
    r'<td class="a-center">([^<]+)</td>'
)


def fetch_future_holidays() -> dict[date, str]:
    """JPX公式ページに掲載されている休業日を {日付: 名称} で返す。

    ページ構造が変わって0件しか取れない場合は、呼び出し側が古い休場日を
    "未登録"と誤解しないよう例外を送出する（沈黙してDBを空更新しない）。
    """
    r = requests.get(JPX_CALENDAR_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text
    rows = _ROW_RE.findall(html)
    if not rows:
        raise ValueError("JPX休業日一覧のページ構造が変わった可能性があります（0件取得）")
    holidays: dict[date, str] = {}
    for y, m, d, name in rows:
        holidays[date(int(y), int(m), int(d))] = name.strip()
    return holidays


if __name__ == "__main__":
    hs = fetch_future_holidays()
    print(f"{len(hs)} 件取得")
    for d in sorted(hs)[:5]:
        print(f"  {d} {hs[d]}")
    print("  ...")
    for d in sorted(hs)[-5:]:
        print(f"  {d} {hs[d]}")
