"""
edinetdb.jp から事業セグメント情報（売上・利益・構成比の時系列）を取得して DB に保存する。

テーブル: company_segments（データ本体）
  (code, fiscal_year, segment_name) が主キー。1コールで過去約7年分の全セグメントが返る。
  金額の単位は円。revenue_share / oi_margin / yoy は 0-1 の比率。

テーブル: company_segments_meta（取得状態）
  銘柄ごとの取得結果を記録する。単一セグメント企業は API が 404 を返すため、
  status='no_data' を残して毎日再取得するループを防ぐ（REFRESH_DAYS 周期でだけ再確認）。

データの性質:
  - カバレッジは複数セグメント開示企業 約3,000社（単一セグメント企業は開示自体がない）
  - IFRS商社系（三菱商事など）はセグメント段階では operating_income でなく gross_profit を開示
  - セグメント再編があった場合、過去年度は当時の区分のまま残る（銘柄単位で全量置換するため
    API 側の遡及修正にも追従する）

更新方針（メンテナンス）:
  1. 未取得銘柄を時価総額の大きい順に取得（サイトでよく見る銘柄から埋まる）
  2. 取得済みは REFRESH_DAYS(90日) 経過したものを古い順に巡回（有報の年次更新に追従）
  3. 1日 LIMIT 件まで。edinetdb.jp 無料枠は 100コール/日 で、edinet_texts.py(10件/日)と
     合算しても枠内に収まるよう 80 件/日 に設定。
  日次実行は .github/workflows/misc_batch.yml（23:45 JST）に登録。

実行例:
  python3 edinet_segments.py 7203            # トヨタのみ
  python3 edinet_segments.py 7203 6758       # 複数銘柄
  python3 edinet_segments.py --all           # 未取得優先 + 90日巡回（上限あり）
  python3 edinet_segments.py --all --force   # 鮮度を無視して全対象を再取得
"""

import os
import sys
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
from config import get_conn

load_dotenv()

BASE         = "https://edinetdb.jp/v1"
KEY          = os.environ.get("EDINETDB_API_KEY", "")
HEADERS      = {"X-API-Key": KEY, "Accept": "application/json"}
DELAY        = 0.5   # API間隔（秒）
LIMIT        = 80    # 1日の取得上限（無料枠100/日 − edinet_texts 10/日 − 余裕10）
REFRESH_DAYS = 90    # 取得済み銘柄の再確認周期（有報は年1回なので90日で十分）

# APIレスポンスのキー → DBカラム の対応（追加カラムはここに足すだけで済む）
FIELD_MAP = {
    "segmentNameEn":        "segment_name_en",
    "segmentType":          "segment_type",
    "revenue":              "revenue",
    "intersegmentRevenue":  "intersegment_revenue",
    "operatingIncome":      "operating_income",
    "grossProfit":          "gross_profit",
    "ordinaryIncome":       "ordinary_income",
    "segmentProfit":        "segment_profit",
    "assets":               "assets",
    "capex":                "capex",
    "depreciation":         "depreciation",
    "employees":            "employees",
    "impairmentLoss":       "impairment_loss",
    "goodwillAmortization": "goodwill_amortization",
    "revenueShare":         "revenue_share",
    "oiMargin":             "oi_margin",
    "revenueYoy":           "revenue_yoy",
    "oiYoy":                "oi_yoy",
}


# ─── テーブル作成 ────────────────────────────────────────────────────────────
def _ensure_tables():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS company_segments (
            code                  VARCHAR(10)  NOT NULL,
            fiscal_year           SMALLINT     NOT NULL,
            segment_name          VARCHAR(100) NOT NULL,
            edinet_code           VARCHAR(8),
            segment_name_en       VARCHAR(150),
            segment_type          VARCHAR(20),
            revenue               DOUBLE,
            intersegment_revenue  DOUBLE,
            operating_income      DOUBLE,
            gross_profit          DOUBLE,
            ordinary_income       DOUBLE,
            segment_profit        DOUBLE,
            assets                DOUBLE,
            capex                 DOUBLE,
            depreciation          DOUBLE,
            employees             INT,
            impairment_loss       DOUBLE,
            goodwill_amortization DOUBLE,
            revenue_share         DOUBLE,
            oi_margin             DOUBLE,
            revenue_yoy           DOUBLE,
            oi_yoy                DOUBLE,
            fetched_at            DATETIME,
            PRIMARY KEY (code, fiscal_year, segment_name)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS company_segments_meta (
            code         VARCHAR(10) PRIMARY KEY,
            edinet_code  VARCHAR(8),
            status       VARCHAR(12),
            n_rows       INT,
            last_fetched DATETIME
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ─── セグメント取得 ──────────────────────────────────────────────────────────
def _fetch_segments(edinet_code: str) -> tuple[str, list[dict]]:
    """
    戻り値: (status, rows)
      status: "ok" | "no_data" | "rate_limited" | "error"
    単一セグメント企業（開示なし）は 404 が正常応答なので no_data として扱う。
    """
    try:
        time.sleep(DELAY)
        r = requests.get(
            f"{BASE}/companies/{edinet_code}/segments",
            headers=HEADERS,
            timeout=30,
        )
        if r.status_code == 429:
            return "rate_limited", []
        if r.status_code == 404:
            return "no_data", []
        r.raise_for_status()
        rows = r.json().get("data", [])
        return ("ok", rows) if rows else ("no_data", [])
    except Exception as e:
        print(f"  [segments] {edinet_code}: {e}")
        return "error", []


# ─── DB 保存 ─────────────────────────────────────────────────────────────────
def _save(code: str, edinet_code: str, status: str, rows: list[dict]) -> int:
    """
    銘柄単位で DELETE → INSERT の全量置換。
    API は毎回全年度を返すため、セグメント名の遡及修正にも追従できる。
    meta には取得結果を必ず記録する（no_data も含む — 再取得ループ防止）。
    """
    conn = get_conn()
    cur  = conn.cursor()
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    saved = 0
    if status == "ok" and rows:
        cur.execute("DELETE FROM company_segments WHERE code = %s", (code,))
        cols = ["code", "fiscal_year", "segment_name", "edinet_code"] + list(FIELD_MAP.values()) + ["fetched_at"]
        sql  = f"INSERT INTO company_segments ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
        for b in rows:
            fy   = b.get("fiscalYear")
            name = (b.get("segmentName") or b.get("segmentNameEn") or "").strip()[:100]
            if not fy or not name:
                continue
            vals = [code, fy, name, edinet_code] + [b.get(k) for k in FIELD_MAP] + [now]
            cur.execute(sql, vals)
            saved += 1

    cur.execute("""
        INSERT INTO company_segments_meta (code, edinet_code, status, n_rows, last_fetched)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            edinet_code  = VALUES(edinet_code),
            status       = VALUES(status),
            n_rows       = VALUES(n_rows),
            last_fetched = VALUES(last_fetched)
    """, (code, edinet_code, status, saved, now))

    conn.commit()
    cur.close()
    conn.close()
    return saved


# ─── 1銘柄を処理 ────────────────────────────────────────────────────────────
def fetch_one(code: str) -> dict:
    """戻り値: {"code", "edinet_code", "rows", "status"}"""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT edinet_code FROM stocks WHERE code = %s", (code,))
    row = cur.fetchone()
    cur.close(); conn.close()

    edinet_code = row[0] if row else None
    if not edinet_code:
        # edinet_code は edinet_texts.py が日次で埋めていくため、ここでは追わない
        return {"code": code, "edinet_code": None, "rows": 0, "status": "no_edinet_code"}

    status, rows = _fetch_segments(edinet_code)
    if status == "rate_limited":
        return {"code": code, "edinet_code": edinet_code, "rows": 0, "status": "rate_limited"}

    saved = _save(code, edinet_code, status, rows)
    return {"code": code, "edinet_code": edinet_code, "rows": saved, "status": status}


# ─── 対象銘柄の選定 ──────────────────────────────────────────────────────────
def _target_codes(force: bool) -> list[str]:
    """
    1. meta に無い銘柄（未取得）を時価総額の大きい順
    2. last_fetched が REFRESH_DAYS より古い銘柄を古い順（force 時は全対象）
    """
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT s.code FROM stocks s
        LEFT JOIN stock_fundamentals f ON f.code = s.code
        WHERE s.is_active = 1 AND s.edinet_code IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM company_segments_meta m WHERE m.code = s.code)
        ORDER BY f.market_cap DESC
    """)
    new_codes = [r[0] for r in cur.fetchall()]

    stale_cond = "" if force else f"AND m.last_fetched < NOW() - INTERVAL {REFRESH_DAYS} DAY"
    cur.execute(f"""
        SELECT m.code FROM company_segments_meta m
        JOIN stocks s ON s.code = m.code AND s.is_active = 1
        WHERE 1=1 {stale_cond}
        ORDER BY m.last_fetched ASC
    """)
    stale_codes = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    return new_codes + stale_codes


# ─── バッチ実行 ─────────────────────────────────────────────────────────────
def run(target_codes: list[str] | None = None, force: bool = False):
    if not KEY:
        print("ERROR: EDINETDB_API_KEY が未設定です。")
        return

    _ensure_tables()

    codes = target_codes if target_codes else _target_codes(force)
    total = len(codes)
    api_used = 0
    ok = nodata = err = 0

    print(f"対象: {total} 銘柄  上限: {LIMIT} 件/日")

    for i, code in enumerate(codes, 1):
        if not target_codes and api_used >= LIMIT:
            print(f"\n[!] 本日の上限 {LIMIT} 件に到達。残り {total - i + 1} 銘柄は明日以降。")
            break

        result = fetch_one(code)
        st = result["status"]

        if st == "rate_limited":
            print(f"  [{i:>4}/{total}] {code}  [!] レート制限 (429) — 本日はここまで")
            break
        api_used += 1
        if st == "ok":
            ok += 1
            label = f"OK ({result['rows']}行)  EC:{result['edinet_code']}"
        elif st == "no_data":
            nodata += 1
            label = "NO_DATA（単一セグメント or 未収録）"
        else:
            err += 1
            label = f"ERROR: {st}"
            if st == "no_edinet_code":
                api_used -= 1  # APIを呼んでいない失敗はカウントしない

        print(f"  [{i:>4}/{total}] {code}  {label}")

    print()
    print(f"完了: 取得OK={ok}  データなし={nodata}  エラー={err}  APIコール使用={api_used}")


if __name__ == "__main__":
    args  = sys.argv[1:]
    force = "--force" in args
    all_  = "--all" in args
    codes = [a for a in args if not a.startswith("--")] or None

    if all_:
        run(target_codes=None, force=force)
    elif codes:
        run(target_codes=codes, force=force)
    else:
        print("使い方: python3 edinet_segments.py <code> [code ...]")
        print("        python3 edinet_segments.py --all [--force]")
