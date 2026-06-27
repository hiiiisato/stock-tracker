"""
EDINET から有価証券報告書の「事業の内容」を一括取得して stocks テーブルに保存する。

━━━━ 事前準備 ━━━━
1. EDINET API キーを無料取得（メール登録のみ・即時発行）
   → https://api.edinet-api.fsa.go.jp/

2. .env に追記:
   EDINET_API_KEY=your_key_here

━━━━ 実行方法 ━━━━
# 全銘柄（目安: 60〜90 分）
python3 edinet_biz.py

# 特定銘柄だけ試す（動作確認に最適）
python3 edinet_biz.py 7203 6758 9984

# 取得済みも強制更新
python3 edinet_biz.py --force

━━━━ 処理フロー ━━━━
1. EDINET コードリスト ZIP をダウンロード
   → 証券コード（4桁）↔ EDINET コード の対応表を stocks.edinet_code に保存

2. 過去15ヶ月の書類一覧 API を日単位で取得
   → 各社の最新有価証券報告書 docID をインデックス化

3. 各社の有価証券報告書 XBRL ZIP をダウンロード
   → 「事業の内容」セクションをパースして stocks.business_description に保存

4. XBRL パース失敗時は kabutan.jp からフォールバック取得
"""

import io
import os
import re
import sys
import time
import zipfile
import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup
from config import get_conn

# ─── 設定 ──────────────────────────────────────────────────────────────────
EDINET_BASE = "https://api.edinet-api.fsa.go.jp/api/v2"
CDL_URL     = "https://disclosure2dl.edinet-api.fsa.go.jp/searchdocument/codelist/Edinetcode_jp.zip"
API_KEY     = os.environ.get("EDINET_API_KEY", "")
A_HEADERS   = {"Subscription-Key": API_KEY}
UA          = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DOC_ANNUAL  = "120"    # 有価証券報告書
INDEX_MONTHS = 15      # 何ヶ月分の書類一覧を検索するか
API_DELAY   = 0.35     # EDINET API 呼び出し間隔（秒）
MAX_BIZ_LEN = 4000     # 事業内容の最大文字数


# ─── DB マイグレーション ─────────────────────────────────────────────────────
def _ensure_columns():
    """必要なカラムが stocks テーブルになければ追加する。"""
    conn = get_conn()
    cur  = conn.cursor()
    for col, definition in [
        ("edinet_code",          "VARCHAR(8)  DEFAULT NULL"),
        ("business_description", "TEXT        DEFAULT NULL"),
        ("biz_updated_at",       "DATETIME    DEFAULT NULL"),
    ]:
        try:
            cur.execute(f"ALTER TABLE stocks ADD COLUMN {col} {definition}")
            conn.commit()
            print(f"  [DB] カラム追加: stocks.{col}")
        except Exception:
            pass   # 既存カラムは無視
    cur.close()
    conn.close()


# ─── EDINET コードマップ ─────────────────────────────────────────────────────
def load_edinet_codemap() -> dict:
    """
    EDINET コードリスト ZIP をダウンロードして
    {4桁証券コード: EDINETコード（E+5桁）} の辞書を返す。
    """
    print("  EDINET コードリスト取得中...")
    r = requests.get(CDL_URL, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))

    for fname in z.namelist():
        if not fname.endswith(".csv"):
            continue
        raw = z.read(fname).decode("cp932", errors="replace")
        result = {}
        for i, line in enumerate(raw.splitlines()):
            if i < 2:        # ヘッダー2行をスキップ
                continue
            parts = line.split(",")
            if len(parts) < 13:
                continue
            edinet_code = parts[0].strip().strip('"')   # 例: E01234
            stock_code  = parts[12].strip().strip('"')  # 4桁証券コード
            if edinet_code and stock_code and len(stock_code) == 4:
                result[stock_code] = edinet_code
        print(f"  コードマップ: {len(result)} 件")
        return result
    return {}


def update_edinet_codes(codemap: dict):
    """stocks.edinet_code を一括更新する。"""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT code FROM stocks WHERE is_active = TRUE")
    codes = [r[0] for r in cur.fetchall()]

    updated = 0
    for code4 in codes:
        ec = codemap.get(code4)
        if ec:
            cur.execute(
                "UPDATE stocks SET edinet_code = %s "
                "WHERE code = %s AND (edinet_code IS NULL OR edinet_code != %s)",
                (ec, code4, ec),
            )
            updated += cur.rowcount

    conn.commit()
    cur.close()
    conn.close()
    print(f"  edinet_code 更新: {updated} 件")


# ─── 書類インデックス構築 ────────────────────────────────────────────────────
def build_doc_index() -> dict:
    """
    過去 INDEX_MONTHS ヶ月の書類一覧を EDINET API から取得し、
    {edinet_code: docID} の最新有価証券報告書インデックスを返す。

    日付降順で走査するため、最初に見つかった docID が最新になる。
    """
    today      = date.today()
    search_end = today
    # INDEX_MONTHS ヶ月前の月初
    y  = today.year - (INDEX_MONTHS // 12)
    mo = today.month - (INDEX_MONTHS % 12)
    if mo <= 0:
        mo += 12; y -= 1
    search_beg = date(y, mo, 1)
    total_days = (search_end - search_beg).days

    print(f"  書類インデックス構築: {search_beg} 〜 {search_end}（約{total_days}日）")

    index = {}   # edinet_code → docID
    d     = search_end
    done  = 0

    while d >= search_beg:
        try:
            time.sleep(API_DELAY)
            r = requests.get(
                f"{EDINET_BASE}/documents.json",
                params={"date": d.strftime("%Y-%m-%d"), "type": 2},
                headers=A_HEADERS,
                timeout=15,
            )
            if r.status_code in (400, 404):
                d -= timedelta(days=1); done += 1; continue
            r.raise_for_status()

            for doc in r.json().get("results", []):
                if doc.get("docTypeCode") != DOC_ANNUAL:
                    continue
                ec = doc.get("edinetCode", "")
                if ec and ec not in index:
                    index[ec] = doc.get("docID", "")

        except Exception as e:
            print(f"    [index] {d}: {e}")

        d -= timedelta(days=1)
        done += 1
        if done % 60 == 0 or done >= total_days:
            pct = done / total_days * 100
            print(f"    {done}/{total_days}日 ({pct:.0f}%)  {len(index):,}社分インデックス済")

    print(f"  インデックス完成: {len(index):,} 社")
    return index


# ─── XBRL パース ─────────────────────────────────────────────────────────────
_BIZ_TAG_PATTERNS = [
    re.compile(r"DescriptionOfBusinessTextBlock$",                re.I),
    re.compile(r"BusinessDescriptionAndAnalysis.*TextBlock$",     re.I),
    re.compile(r"DescriptionOfBusiness$",                         re.I),
]


def _extract_from_xbrl(xbrl_bytes: bytes) -> str | None:
    """XBRL バイト列から「事業の内容」テキストを抽出する。"""
    try:
        soup = BeautifulSoup(xbrl_bytes, "lxml-xml")

        for pat in _BIZ_TAG_PATTERNS:
            el = soup.find(name=pat)
            if not el:
                continue
            inner = el.decode_contents().strip()
            if not inner:
                continue
            # 内部 HTML をテキストに変換
            text = BeautifulSoup(inner, "html.parser").get_text("\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if len(text) > 50:
                return text[:MAX_BIZ_LEN]

    except Exception:
        pass
    return None


def fetch_biz_from_edinet(doc_id: str) -> str | None:
    """
    EDINET から有価証券報告書（XBRL ZIP, type=5）をダウンロードし
    事業の内容テキストを返す。失敗時は None。
    """
    time.sleep(API_DELAY)
    try:
        r = requests.get(
            f"{EDINET_BASE}/documents/{doc_id}",
            params={"type": 5},
            headers=A_HEADERS,
            timeout=60,
        )
        if r.status_code != 200:
            return None

        z = zipfile.ZipFile(io.BytesIO(r.content))

        # PublicDoc 以下の .xbrl ファイルを探す（名前が短い＝メイン書類を優先）
        xbrl_files = sorted(
            [n for n in z.namelist() if "PublicDoc" in n and n.endswith(".xbrl")],
            key=len,
        )
        for fname in xbrl_files:
            text = _extract_from_xbrl(z.read(fname))
            if text:
                return text

    except Exception as e:
        print(f"    [XBRL] {doc_id}: {e}")
    return None


# ─── フォールバック: kabutan.jp ─────────────────────────────────────────────
def _kabutan_fallback(code4: str) -> str | None:
    """
    kabutan.jp の事業内容テキストを取得する（EDINET 失敗時のフォールバック）。
    """
    try:
        url = f"https://kabutan.jp/stock/info?code={code4}"
        r   = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for sel in [".company_body p", ".company_body"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text("\n", strip=True)
                if len(t) > 20:
                    return t[:2000]
    except Exception:
        pass
    return None


# ─── メイン ──────────────────────────────────────────────────────────────────
def run(target_codes: list = None, force: bool = False):
    if not API_KEY:
        print("=" * 60)
        print("  ERROR: EDINET_API_KEY が設定されていません。")
        print("=" * 60)
        print()
        print("  1. 以下で無料APIキーを取得（メール登録のみ・即時発行）:")
        print("     https://api.edinet-api.fsa.go.jp/")
        print()
        print("  2. .env に追記:")
        print("     EDINET_API_KEY=<取得したキー>")
        print()
        print("  3. 再度実行:")
        print("     python3 edinet_biz.py")
        return

    print("=" * 60)
    print("  EDINET 事業内容バッチ取得")
    print("=" * 60)
    print()

    # DB カラムの確保
    _ensure_columns()

    # ─ Step 1: EDINET コードマップを取得して DB 更新 ─
    print("[Step 1] EDINET コードマップ更新")
    codemap = load_edinet_codemap()
    update_edinet_codes(codemap)
    print()

    # ─ Step 2: 書類インデックス構築 ─
    print("[Step 2] 書類インデックス構築（過去15ヶ月）")
    doc_index = build_doc_index()
    print()

    # ─ Step 3: 対象銘柄を取得 ─
    print("[Step 3] 事業内容を取得・保存")
    conn = get_conn()
    cur  = conn.cursor()

    if target_codes:
        ph = ",".join(["%s"] * len(target_codes))
        cur.execute(
            f"SELECT code, edinet_code, business_description "
            f"FROM stocks WHERE code IN ({ph})",
            target_codes,
        )
    else:
        cur.execute("""
            SELECT code, edinet_code, business_description
            FROM stocks
            WHERE is_active = TRUE AND edinet_code IS NOT NULL
            ORDER BY code
        """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not force:
        rows = [r for r in rows if r[2] is None]   # 未取得分のみ

    total = len(rows)
    print(f"  対象: {total} 銘柄")
    if total == 0:
        print("  全銘柄取得済みです（--force で強制更新）")
        return

    # 推定時間
    est_sec = total * (API_DELAY * 2 + 2)  # XBRL DL 平均2秒
    print(f"  推定時間: {est_sec/60:.0f}〜{est_sec/60*1.5:.0f} 分")
    print()

    ok = 0; fail_xbrl = 0; fail_all = 0
    start_ts = time.time()

    for i, (code4, ec, _) in enumerate(rows, 1):
        doc_id   = doc_index.get(ec, "")
        biz_text = None

        # EDINET から取得
        if doc_id:
            biz_text = fetch_biz_from_edinet(doc_id)
            if not biz_text:
                fail_xbrl += 1

        # フォールバック: kabutan.jp
        if not biz_text:
            time.sleep(0.3)
            biz_text = _kabutan_fallback(code4)

        # DB 保存
        if biz_text:
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute(
                "UPDATE stocks SET business_description = %s, biz_updated_at = NOW() "
                "WHERE code = %s",
                (biz_text, code4),
            )
            conn.commit()
            cur.close()
            conn.close()
            ok += 1
        else:
            fail_all += 1

        if i % 50 == 0 or i == total:
            elapsed = time.time() - start_ts
            remain  = (elapsed / i * (total - i)) / 60 if i < total else 0
            print(f"  [{i:>4}/{total}]  OK:{ok:>4}  XBRL失敗:{fail_xbrl:>4}  全失敗:{fail_all:>3}  "
                  f"残り約{remain:.0f}分")

    elapsed_m = (time.time() - start_ts) / 60
    print()
    print("=" * 60)
    print(f"  完了（{elapsed_m:.1f}分）")
    print(f"  取得成功: {ok} 銘柄")
    print(f"  XBRL失敗→kabutan: {fail_xbrl} 銘柄")
    print(f"  全失敗: {fail_all} 銘柄")
    print("=" * 60)


if __name__ == "__main__":
    args         = sys.argv[1:]
    force        = "--force" in args
    target_codes = [a for a in args if not a.startswith("--")] or None
    run(target_codes=target_codes, force=force)
