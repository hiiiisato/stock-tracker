"""
EDINET 公式API（金融庁・無料）から有価証券報告書の「事業の内容」を直接取得して
stocks.business_description（詳細な事業内容）を全銘柄分メンテナンスする。

━━━━ 事前準備（1回だけ・無料） ━━━━
1. https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1 でメール登録しAPIキーを発行
2. .env / Render / GitHub Secrets に追記: EDINET_API_KEY=xxxxx

━━━━ 仕組み ━━━━
- 書類一覧API（日付ごと）から有価証券報告書（docTypeCode=120）を発見
- CSV形式（type=5）で書類をダウンロードし、jpcrp_cor:DescriptionOfBusinessTextBlock を抽出
- HTMLタグを除去したプレーンテキストを stocks.business_description に保存
  （edinet_text_blocks の「事業の内容」セクションにも upsert して既存表示と整合）

━━━━ メンテナンス ━━━━
- 日次: daily_run.py から run_incremental() — 直近7日に提出された有報だけ処理。
  各社が年1回有報を出すたびに自動で最新化される（増分なので1日数十件・数分）。
- 初回: python3 edinet_business.py --backfill で過去380日分を一括走査（数時間・1回だけ）。
  edinetdb.jp 経由（edinet_texts.py, 10件/日制限）と違い全銘柄を一気にカバーできる。

実行例:
  python3 edinet_business.py --backfill            # 過去380日ぶんの有報を一括処理
  python3 edinet_business.py --backfill --days 30  # 過去30日ぶんだけ
  python3 edinet_business.py                       # 増分（直近7日）
"""
import csv
import io
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv
from config import get_conn

load_dotenv()

BASE     = "https://api.edinet-fsa.go.jp/api/v2"
KEY      = os.environ.get("EDINET_API_KEY", "")
DELAY    = 0.3          # APIリクエスト間隔（秒）
MAX_BYTES = 63000       # TiDBのTEXT実効上限に合わせる（edinet_texts.py と同じ）
TARGET_ELEMENT = "jpcrp_cor:DescriptionOfBusinessTextBlock"   # 事業の内容


def _list_documents(day: date) -> list[dict]:
    """指定日の提出書類一覧を返す。"""
    time.sleep(DELAY)
    r = requests.get(
        f"{BASE}/documents.json",
        params={"date": day.strftime("%Y-%m-%d"), "type": 2, "Subscription-Key": KEY},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("results", []) or []


def _fetch_business_text(doc_id: str) -> str | None:
    """書類のCSV(zip)をダウンロードして「事業の内容」テキストブロックを抽出する。"""
    time.sleep(DELAY)
    r = requests.get(
        f"{BASE}/documents/{doc_id}",
        params={"type": 5, "Subscription-Key": KEY},
        timeout=60,
    )
    if r.status_code != 200:
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))   # zip以外（エラーJSON等）はここで弾かれる
    except zipfile.BadZipFile:
        return None
    for name in zf.namelist():
        if not name.lower().endswith(".csv") or "jpcrp" not in name:
            continue
        # EDINETのCSVは UTF-16LE (BOM付き)・タブ区切り
        raw = zf.read(name).decode("utf-16", errors="ignore")
        for row in csv.reader(io.StringIO(raw), delimiter="\t"):
            if len(row) >= 9 and row[0].strip('"') == TARGET_ELEMENT:
                return _strip_html(row[8].strip('"'))
    return None


def _strip_html(html_text: str) -> str:
    """XBRLテキストブロックのHTMLをプレーンテキスト化する。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text.replace("\\n", "\n"), "html.parser")
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.splitlines()]
    out, blank = [], 0
    for l in lines:
        if l:
            out.append(l); blank = 0
        else:
            blank += 1
            if blank == 1:
                out.append("")
    return "\n".join(out).strip()


def _save(code: str, edinet_code: str, text: str, report_date: str | None):
    """DB保存。TiDBの一時的な接続切断に備えて1回リトライする（長時間バッチ対策）。"""
    text_enc = text.encode("utf-8")
    if len(text_enc) > MAX_BYTES:
        text = text_enc[:MAX_BYTES].decode("utf-8", errors="ignore")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for attempt in (1, 2):
        conn = None
        try:
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute(
                "UPDATE stocks SET business_description=%s, biz_updated_at=%s, edinet_code=COALESCE(edinet_code,%s) WHERE code=%s",
                (text, now, edinet_code, code))
            cur.execute("""
                INSERT INTO edinet_text_blocks (code, section, edinet_code, report_date, text, fetched_at)
                VALUES (%s, '事業の内容', %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE edinet_code=VALUES(edinet_code),
                    report_date=VALUES(report_date), text=VALUES(text), fetched_at=VALUES(fetched_at)
            """, (code, edinet_code, report_date, text, now))
            conn.commit()
            cur.close()
            conn.close()
            return
        except Exception as e:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            if attempt == 2:
                raise
            print(f"  {code}: DB保存リトライ ({e})")
            time.sleep(5)


def _active_codes_and_freshness() -> tuple[set, dict]:
    """アクティブ銘柄と、business_description の更新日を返す。"""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT code, biz_updated_at FROM stocks WHERE is_active = 1")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return {r[0] for r in rows}, {r[0]: r[1] for r in rows}


def _process_days(days: list[date], skip_fresh_days: int | None = None) -> int:
    """指定した日々の提出一覧を走査し、有報の「事業の内容」を保存する。"""
    if not KEY:
        print("  EDINET_API_KEY が未設定のためスキップ（https://api.edinet-fsa.go.jp で無料発行）")
        return 0
    active, freshness = _active_codes_and_freshness()
    n_saved = 0
    for day in days:
        try:
            docs = _list_documents(day)
        except Exception as e:
            print(f"  [{day}] 一覧取得エラー: {e}")
            continue
        yuho = [d for d in docs
                if d.get("docTypeCode") == "120" and d.get("secCode")]
        for d in yuho:
            code = d["secCode"][:4]           # 72030 → 7203
            if code not in active:
                continue
            if skip_fresh_days is not None:
                bu = freshness.get(code)
                # 提出日以降に更新済みならスキップ（バックフィルの重複回避）
                if bu and (bu.date() if hasattr(bu, "date") else bu) >= day:
                    continue
            try:
                text = _fetch_business_text(d["docID"])
                if not text or len(text) < 100:
                    continue
                _save(code, d.get("edinetCode", ""), text, day.strftime("%Y-%m-%d"))
            except Exception as e:
                print(f"  {code}: エラー {e}")
                continue
            freshness[code] = datetime.combine(day, datetime.min.time())
            n_saved += 1
            print(f"  {code} {d.get('filerName','')[:20]} ({day}) {len(text)}字")
    return n_saved


def run_incremental(days_back: int = 7) -> int:
    """直近days_back日に提出された有報を処理する（日次バッチ用）。"""
    days = [date.today() - timedelta(days=i) for i in range(days_back)]
    n = _process_days(days, skip_fresh_days=0)
    print(f"EDINET増分: {n} 銘柄の事業内容を更新")
    return n


def run_backfill(days_back: int = 380) -> int:
    """過去days_back日の有報を一括走査する（初回のみ・数時間）。"""
    days = [date.today() - timedelta(days=i) for i in range(days_back)]
    n = _process_days(days, skip_fresh_days=0)
    print(f"EDINETバックフィル完了: {n} 銘柄")
    return n


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--backfill" in args:
        db = int(args[args.index("--days") + 1]) if "--days" in args else 380
        run_backfill(days_back=db)
    else:
        run_incremental()
