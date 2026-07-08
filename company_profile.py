"""
kabutan 銘柄トップページから会社概要（簡単な事業内容）・kabutanテーマ・会社サイト等を
一括取得して DB に保存する。

保存先:
  stocks.business_summary   — 簡単な事業内容（kabutan「概要」、1〜2文）
  stocks.website            — 会社サイトURL
  stocks.profile_updated_at — 取得日時（成功・情報なしを問わず取得試行でスタンプ）
  kabutan_themes            — kabutan が付与するテーマタグ（銘柄×テーマ、資金フロー分析で使用）

※ 詳細な事業内容は EDINET 有報由来の stocks.business_description（edinet_texts.py）が担当。
   本モジュールは「簡単な事業内容」と「テーマタグ」の担当。

メンテナンス:
  daily_run.py から run(limit=150) を毎日呼ぶ。
  未取得(NULL)優先 → 取得が古い順に一巡するので、全銘柄が約1ヶ月周期で更新される。
  新規上場銘柄は profile_updated_at が NULL のため自動的に最優先で取得される。

実行例:
  python3 company_profile.py 7203 6758     # 特定銘柄のみ
  python3 company_profile.py --backfill    # 全アクティブ銘柄（初回一括、約1時間）
  python3 company_profile.py               # 日次分（150件）
"""
import sys
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from config import get_conn

UA      = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
DELAY   = 0.5           # リクエスト間隔（秒）— kabutan への礼儀（financials_kabutan.py と同じ）
DAILY_LIMIT = 150       # 日次メンテナンスの件数（約1ヶ月で全銘柄一巡）
STALE_DAYS  = 25        # これより古いものを再取得対象にする


# ─── DB マイグレーション ─────────────────────────────────────────────────────
def ensure_schema():
    conn = get_conn()
    cur  = conn.cursor()
    for col, typedef in [
        ("business_summary",   "TEXT DEFAULT NULL"),
        ("website",            "VARCHAR(255) DEFAULT NULL"),
        ("profile_updated_at", "DATETIME DEFAULT NULL"),
    ]:
        try:
            cur.execute(f"ALTER TABLE stocks ADD COLUMN {col} {typedef}")
            conn.commit()
            print(f"  [DB] カラム追加: stocks.{col}")
        except Exception:
            conn.rollback()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kabutan_themes (
            code   VARCHAR(10) NOT NULL,
            theme  VARCHAR(80) NOT NULL,
            PRIMARY KEY (code, theme),
            KEY idx_theme (theme)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ─── 1銘柄取得 ───────────────────────────────────────────────────────────────
def fetch_one(code: str) -> dict | None:
    """kabutan トップページから会社情報を取得。ページ自体が無ければ None。"""
    try:
        r = requests.get(f"https://kabutan.jp/stock/?code={code}", headers=UA, timeout=10)
        if r.status_code == 404:
            return {}   # ページ自体が無い（PRO Market等）→ 試行済みスタンプして毎日再試行しない
        if r.status_code != 200:
            return None  # 一時的なエラー → 次回リトライ
        soup = BeautifulSoup(r.text, "html.parser")
        blk  = soup.select_one(".company_block")
        if not blk:
            return {}   # ページはあるが会社情報なし（ETF等）→ 試行済みとして記録
        data: dict = {"themes": []}
        for tr in blk.find_all("tr"):
            th, td = tr.find("th"), tr.find("td")
            if not (th and td):
                continue
            key = th.text.strip()
            if key == "概要":
                data["summary"] = td.text.strip()[:1000]
            elif key == "会社サイト":
                data["website"] = td.text.strip()[:255]
            elif key == "テーマ":
                links = td.find_all("a")
                names = [a.text.strip() for a in links] if links else td.text.split()
                data["themes"] = [t[:80] for t in names if t.strip()]
        return data
    except Exception as e:
        print(f"  [company_profile] {code}: {e}")
        return None


def _save(code: str, data: dict, now: str):
    """DB保存。長時間バッチ中のTiDB接続リセットに備え、保存ごとに新規接続＋1回リトライする。"""
    for attempt in (1, 2):
        conn = None
        try:
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute(
                """UPDATE stocks SET business_summary = COALESCE(%s, business_summary),
                                     website          = COALESCE(%s, website),
                                     profile_updated_at = %s
                   WHERE code = %s""",
                (data.get("summary"), data.get("website"), now, code))
            themes = data.get("themes") or []
            if themes:
                cur.execute("DELETE FROM kabutan_themes WHERE code = %s", (code,))
                cur.executemany(
                    "INSERT IGNORE INTO kabutan_themes (code, theme) VALUES (%s, %s)",
                    [(code, t) for t in themes])
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


# ─── バッチ実行 ──────────────────────────────────────────────────────────────
def run(target_codes: list[str] | None = None, limit: int | None = DAILY_LIMIT) -> int:
    """未取得優先→古い順に limit 件取得。戻り値=更新銘柄数。"""
    ensure_schema()
    conn = get_conn()
    cur  = conn.cursor()

    if target_codes:
        codes = target_codes
    else:
        cur.execute("""
            SELECT code FROM stocks
            WHERE is_active = 1
              AND (profile_updated_at IS NULL
                   OR profile_updated_at < DATE_SUB(NOW(), INTERVAL %s DAY))
            ORDER BY (profile_updated_at IS NULL) DESC, profile_updated_at ASC, code
            {}
        """.format(f"LIMIT {int(limit)}" if limit else ""), (STALE_DAYS,))
        codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    n_ok = n_empty = n_err = 0
    for i, code in enumerate(codes, 1):
        time.sleep(DELAY)
        data = fetch_one(code)
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if data is None:
            n_err += 1
            continue
        try:
            _save(code, data, now)
        except Exception as e:
            print(f"  {code}: 保存失敗（スキップ） {e}")
            n_err += 1
            continue
        if data.get("summary"):
            n_ok += 1
        else:
            n_empty += 1
        if i % 50 == 0:
            print(f"  [{i}/{len(codes)}] 概要あり={n_ok} 情報なし={n_empty} 失敗={n_err}")
    print(f"会社概要取得 完了: 概要あり={n_ok} 情報なし={n_empty} 失敗={n_err} / 対象{len(codes)}件")
    return n_ok


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--backfill" in args:
        run(limit=None)
    else:
        codes = [a for a in args if not a.startswith("--")]
        run(target_codes=codes or None)
