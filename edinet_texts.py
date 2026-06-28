"""
edinetdb.jp から有価証券報告書のテキスト情報を取得して DB に保存する。

テーブル: edinet_text_blocks
  code / section が主キー。全17セクションを全文で保存。
  fiscal_year: 決算年度（例: 2025）
  report_date: 有価証券報告書の提出日（例: 2026-03-25）

実行例:
  python3 edinet_texts.py 1911             # 住友林業のみ
  python3 edinet_texts.py 1911 7203 6758   # 複数銘柄
  python3 edinet_texts.py --all            # 全アクティブ銘柄（上限あり）
  python3 edinet_texts.py --all --force    # 取得済みも強制更新

テーブルが存在しない場合は自動作成。
stocks.edinet_code / business_description も同時に更新する。
1銘柄あたり1 APIコール（text-blocks のみ）。
fiscal_year / report_date はテキスト内の「有価証券報告書提出日」記述から自動抽出。
"""

import os
import sys
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
from config import get_conn

load_dotenv()

BASE    = "https://edinetdb.jp/v1"
KEY     = os.environ.get("EDINETDB_API_KEY", "")
HEADERS = {"X-API-Key": KEY, "Accept": "application/json"}
DELAY   = 0.5    # API間隔（秒）
LIMIT   = 80     # 1日あたりの処理上限（公称100件/日の安全マージン確保）


# ─── テーブル作成 ────────────────────────────────────────────────────────────
def _ensure_table():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS edinet_text_blocks (
            code        VARCHAR(10)   NOT NULL,
            section     VARCHAR(100)  NOT NULL,
            edinet_code VARCHAR(8),
            fiscal_year SMALLINT,
            report_date DATE,
            text        MEDIUMTEXT,
            fetched_at  DATETIME,
            PRIMARY KEY (code, section)
        )
    """)
    # 既存テーブルへのカラム追加（初回マイグレーション）
    for col, typedef in [
        ("fiscal_year", "SMALLINT"),
        ("report_date", "DATE"),
    ]:
        try:
            cur.execute(f"ALTER TABLE edinet_text_blocks ADD COLUMN {col} {typedef}")
        except Exception:
            pass
    conn.commit()
    cur.close()
    conn.close()


# ─── テキストから決算年度・提出日を抽出 ─────────────────────────────────────────
def _extract_dates_from_blocks(blocks: list[dict]) -> dict:
    """
    テキストブロックの文字列から fiscal_year と report_date を正規表現で抽出する。
    追加 API コール不要。

    役員の状況に "2026年3月25日（有価証券報告書提出日）" のような記述がある。
    主要な設備の状況などに "2025年12月31日現在" のような期末日が含まれる。
    """
    import re
    import unicodedata

    # 全角数字を半角に統一してから検索
    all_text = unicodedata.normalize(
        "NFKC",
        "\n".join(b.get("text", "") or "" for b in blocks),
    )

    report_date = None
    fiscal_year = None

    # 提出日: "2026年3月25日（有価証券報告書提出日）"
    m = re.search(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日[（(]有価証券報告書提出日[)）]",
        all_text,
    )
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        report_date = f"{y:04d}-{mo:02d}-{d:02d}"

    # 決算期末日: "2025年12月31日現在" など（月末寄りの日付を探す）
    for m in re.finditer(r"(\d{4})年(\d{1,2})月(\d{1,2})日現在", all_text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if d >= 20 and 2010 <= y <= 2035:
            fiscal_year = y
            break

    # 期末日が見つからない場合は提出日の前年を使う
    # (有報は決算期末から3〜6ヶ月後に提出されるのが一般的)
    if fiscal_year is None and report_date:
        fiscal_year = int(report_date[:4]) - 1

    return {"fiscal_year": fiscal_year, "report_date": report_date}


# ─── edinet_code 取得（search API・認証不要） ────────────────────────────────
def _get_edinet_code(code: str) -> str | None:
    try:
        time.sleep(DELAY)
        r = requests.get(f"{BASE}/search", params={"q": code[:4]}, timeout=10)
        if r.status_code != 200:
            return None
        target = code[:4] + "0"  # 例: 1911 → 19110
        for item in r.json().get("data", []):
            if item.get("sec_code") == target:
                return item.get("edinet_code")
    except Exception as e:
        print(f"  [search] {code}: {e}")
    return None


# ─── テキスト取得（text-blocks API） ────────────────────────────────────────
def _fetch_text_blocks(edinet_code: str) -> list[dict]:
    """全セクションを全文（full=true）で1コールで取得する。"""
    try:
        time.sleep(DELAY)
        r = requests.get(
            f"{BASE}/companies/{edinet_code}/text-blocks",
            params={"full": "true"},
            headers=HEADERS,
            timeout=30,
        )
        if r.status_code == 429:
            print("  [!] レート制限 (429) — 本日の上限に達しました")
            return []
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"  [text-blocks] {edinet_code}: {e}")
        return []


# ─── DB 保存 ─────────────────────────────────────────────────────────────────
def _save_blocks(code: str, edinet_code: str, blocks: list[dict],
                 fiscal_year: int | None = None, report_date: str | None = None):
    if not blocks:
        return 0
    conn = get_conn()
    cur  = conn.cursor()
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    saved = 0
    MAX_BYTES = 63000  # TiDB フリープランの実効上限（65535バイト）に余裕を持たせる

    for b in blocks:
        section = b.get("section", "")
        text    = b.get("text", "")
        if not section or not text:
            continue
        # バイト超過時は文字境界で切り詰め
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_BYTES:
            text = encoded[:MAX_BYTES].decode("utf-8", errors="ignore")
        cur.execute("""
            INSERT INTO edinet_text_blocks
                (code, section, edinet_code, fiscal_year, report_date, text, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                edinet_code = VALUES(edinet_code),
                fiscal_year = VALUES(fiscal_year),
                report_date = VALUES(report_date),
                text        = VALUES(text),
                fetched_at  = VALUES(fetched_at)
        """, (code, section, edinet_code, fiscal_year, report_date, text, now))
        saved += 1

    # stocks テーブルの edinet_code を更新
    cur.execute(
        "UPDATE stocks SET edinet_code = %s WHERE code = %s",
        (edinet_code, code)
    )

    # '事業の内容' を business_description にも反映
    biz = next((b["text"] for b in blocks if b.get("section") == "事業の内容"), None)
    if biz:
        biz_enc = biz.encode("utf-8")
        if len(biz_enc) > MAX_BYTES:
            biz = biz_enc[:MAX_BYTES].decode("utf-8", errors="ignore")
        cur.execute(
            "UPDATE stocks SET business_description = %s, biz_updated_at = %s WHERE code = %s",
            (biz, now, code)
        )

    conn.commit()
    cur.close()
    conn.close()
    return saved


# ─── 1銘柄を処理 ────────────────────────────────────────────────────────────
def fetch_one(code: str, force: bool = False) -> dict:
    """
    1銘柄のテキストを取得してDBに保存する。
    戻り値: {"code": ..., "edinet_code": ..., "sections": int, "status": "ok"|"skip"|"error"}
    """
    # edinet_code を DB から取得
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT edinet_code FROM stocks WHERE code = %s",
        (code,)
    )
    row = cur.fetchone()
    ec_in_db = row[0] if row else None

    if not force:
        # 取得済みチェック
        cur.execute(
            "SELECT COUNT(*) FROM edinet_text_blocks WHERE code = %s",
            (code,)
        )
        existing = cur.fetchone()[0]
        if existing > 0:
            cur.close(); conn.close()
            return {"code": code, "edinet_code": ec_in_db, "sections": existing, "status": "skip"}

    cur.close(); conn.close()

    # edinet_code が未設定なら search API で取得
    edinet_code = ec_in_db
    if not edinet_code:
        edinet_code = _get_edinet_code(code)
        if not edinet_code:
            return {"code": code, "edinet_code": None, "sections": 0, "status": "no_edinet_code"}

    # テキスト取得（APIコール1回のみ）
    blocks = _fetch_text_blocks(edinet_code)
    if not blocks:
        return {"code": code, "edinet_code": edinet_code, "sections": 0, "status": "no_data"}

    # 提出日・決算年度をテキストから抽出（追加コール不要）
    meta = _extract_dates_from_blocks(blocks)

    saved = _save_blocks(code, edinet_code, blocks,
                         fiscal_year=meta.get("fiscal_year"),
                         report_date=meta.get("report_date"))
    return {"code": code, "edinet_code": edinet_code, "sections": saved, "status": "ok"}


# ─── バッチ実行 ─────────────────────────────────────────────────────────────
def run(target_codes: list[str] | None = None, force: bool = False):
    if not KEY:
        print("ERROR: EDINETDB_API_KEY が未設定です。")
        return

    _ensure_table()

    # 対象銘柄を決定
    if target_codes:
        codes = target_codes
    else:
        conn = get_conn()
        cur  = conn.cursor()
        if force:
            cur.execute("SELECT code FROM stocks WHERE is_active=1 ORDER BY code")
        else:
            # 未取得の銘柄を優先
            cur.execute("""
                SELECT s.code FROM stocks s
                WHERE s.is_active = 1
                  AND NOT EXISTS (
                    SELECT 1 FROM edinet_text_blocks e WHERE e.code = s.code
                  )
                ORDER BY s.code
            """)
        codes = [r[0] for r in cur.fetchall()]
        cur.close(); conn.close()

    total    = len(codes)
    api_used = 0
    ok = skip = err = 0

    print(f"対象: {total} 銘柄  上限: {LIMIT} 件/日")
    print()

    for i, code in enumerate(codes, 1):
        if not force and api_used >= LIMIT:
            print(f"\n[!] 本日の上限 {LIMIT} 件に到達。残り {total - i + 1} 銘柄は明日以降。")
            break

        result = fetch_one(code, force=force)

        if result["status"] == "ok":
            ok += 1
            api_used += 1
            label = f"OK ({result['sections']}セクション)  EC:{result['edinet_code']}"
        elif result["status"] == "skip":
            skip += 1
            label = f"SKIP（取得済み {result['sections']}件）"
        else:
            err += 1
            label = f"ERROR: {result['status']}"

        print(f"  [{i:>4}/{total}] {code}  {label}")

    print()
    print(f"完了: 取得OK={ok}  スキップ={skip}  エラー={err}  APIコール使用={api_used}")


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
        print("使い方: python3 edinet_texts.py <code> [code ...] [--force]")
        print("        python3 edinet_texts.py --all [--force]")
