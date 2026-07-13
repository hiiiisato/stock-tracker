"""
JPX(日本取引所)公式の「決算発表予定日」Excelを取り込み、earnings_schedule を更新する。

kabutan のfinanceページ（データセンターIP遮断）に代わる公式一次データ。
JPXは毎営業日17時頃、決算期末を迎えた会社の決算発表予定日をExcelで無料公開している。
  https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html

ページ内の複数の .xlsx（決算期末月グループ別）を全て取得してマージし、
(コード, 発表予定日) を earnings_schedule に upsert する。冪等。

単体実行: python3 earnings_calendar_jpx.py
"""

import io
import re
from datetime import date, datetime

import requests

from config import get_conn

JPX_PAGE = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
JPX_ORIGIN = "https://www.jpx.co.jp"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _xlsx_links() -> list[str]:
    """JPXの決算発表予定日ページから現在掲載中の .xlsx リンクを全て返す。"""
    r = requests.get(JPX_PAGE, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    hrefs = re.findall(r'href="([^"]+\.xlsx)"', r.text, re.I)
    urls = []
    for h in dict.fromkeys(hrefs):   # 重複排除・順序保持
        if "financial-announcement" not in h:
            continue
        urls.append(h if h.startswith("http") else JPX_ORIGIN + h)
    return urls


def _norm_code(v) -> str | None:
    """Excelのコード列を4桁/4文字コードに正規化。'2753'/2753/2753.0/'296A' → '2753'/'296A'。"""
    if v is None:
        return None
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    s = s.upper()
    return s if re.fullmatch(r"[0-9A-Z]{4}", s) else None


def _parse_xlsx(content: bytes) -> list[tuple[str, date]]:
    """JPX Excel → [(code, announce_date), ...]。ヘッダー行(決算発表予定日/コード)を自動検出。"""
    import pandas as pd
    df = pd.read_excel(io.BytesIO(content), header=None, engine="openpyxl")
    # ヘッダー行（0列目に「決算発表予定日」、1列目に「コード」を含む行）を探す
    hdr = None
    for i in range(min(12, len(df))):
        c0 = str(df.iloc[i, 0]); c1 = str(df.iloc[i, 1]) if df.shape[1] > 1 else ""
        if "決算発表予定日" in c0 and "コード" in c1:
            hdr = i
            break
    if hdr is None:
        return []
    out = []
    for i in range(hdr + 1, len(df)):
        code = _norm_code(df.iloc[i, 1])
        d = df.iloc[i, 0]
        if not code or d is None:
            continue
        try:
            ann = pd.to_datetime(d).date()
        except (ValueError, TypeError):
            continue
        out.append((code, ann))
    return out


def import_schedule() -> dict:
    """JPX公式の決算発表予定日を全ファイル取り込み、earnings_schedule を更新する。
    1コード1行（次回発表予定日）。同一コードが複数ファイルにある場合は最も早い予定日を採用。"""
    links = _xlsx_links()
    best: dict[str, date] = {}
    for url in links:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=25)
            if r.status_code != 200:
                continue
            for code, ann in _parse_xlsx(r.content):
                if code not in best or ann < best[code]:
                    best[code] = ann
        except Exception as e:
            print(f"  [JPX予定日] {url} 取得失敗: {e}")

    if not best:
        print("  [JPX予定日] 取得0件（ページ構成変更の可能性）")
        return {"files": len(links), "codes": 0}

    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS earnings_schedule (
            code          VARCHAR(10) PRIMARY KEY,
            announce_date DATE,
            fetched_at    DATETIME
        )
    """)
    rows = [(code, ann) for code, ann in best.items()]
    cur.executemany("""
        INSERT INTO earnings_schedule (code, announce_date, fetched_at)
        VALUES (%s, %s, NOW())
        ON DUPLICATE KEY UPDATE announce_date = VALUES(announce_date), fetched_at = NOW()
    """, rows)
    conn.commit(); cur.close(); conn.close()
    print(f"  [JPX予定日] {len(links)}ファイル → {len(rows)}銘柄の決算発表予定日を更新")
    return {"files": len(links), "codes": len(rows)}


if __name__ == "__main__":
    import_schedule()
