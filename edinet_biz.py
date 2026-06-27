"""
edinetdb.jp API を使って「事業の内容」を一括取得して stocks テーブルに保存する。

━━━━ 事前準備 ━━━━
1. edinetdb.jp でAPIキーを取得 → https://edinetdb.jp/developers
2. .env に追記:
   EDINETDB_API_KEY=your_key_here

━━━━ 実行方法 ━━━━
# 全銘柄（無料プランは 100件/日。毎日実行で数週間かかります）
python3 edinet_biz.py

# 特定銘柄だけ試す
python3 edinet_biz.py 7203 6758 9984

# 取得済みも強制更新
python3 edinet_biz.py --force

━━━━ 処理フロー ━━━━
1. edinetdb.jp 検索API（認証不要）で証券コード → EDINETコード を取得・DB保存
2. edinetdb.jp text-blocks API で「事業の内容」テキストを取得（要APIキー）
3. 失敗時は kabutan.jp からフォールバック取得
"""

import os
import sys
import time
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from config import get_conn

load_dotenv()

# ─── 設定 ──────────────────────────────────────────────────────────────────
EDINETDB_BASE = "https://edinetdb.jp/v1"
EDINETDB_KEY  = os.environ.get("EDINETDB_API_KEY", "")
EDB_HEADERS   = {"X-API-Key": EDINETDB_KEY, "Accept": "application/json"}
UA            = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
SEARCH_DELAY  = 0.3      # 検索API呼び出し間隔（秒）
API_DELAY     = 0.5      # text-blocks API 呼び出し間隔（秒）
FREE_LIMIT    = 95       # 無料プランの安全マージン（公称100件/日）
MAX_BIZ_LEN   = 6000     # 事業内容の最大文字数

# 取得したいセクション（優先順、複数あれば連結）
WANT_SECTIONS = ["事業の内容", "事業方針・経営環境"]


# ─── DB マイグレーション ─────────────────────────────────────────────────────
def _ensure_columns():
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
            pass
    cur.close()
    conn.close()


# ─── edinetdb.jp 検索で edinet_code を取得 ──────────────────────────────────
def _search_edinet_code(code4: str) -> str | None:
    """
    edinetdb.jp の検索API（認証不要）で証券コードを検索し、
    EDINETコード（E+5桁）を返す。見つからない場合は None。
    """
    try:
        time.sleep(SEARCH_DELAY)
        r = requests.get(
            f"{EDINETDB_BASE}/search",
            params={"q": code4},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        sec_code_target = code4 + "0"   # 例: 7203 → 72030
        for item in r.json().get("data", []):
            if item.get("sec_code") == sec_code_target:
                return item.get("edinet_code")
    except Exception as e:
        print(f"    [search] {code4}: {e}")
    return None


def populate_edinet_codes(codes: list[str]) -> dict:
    """
    edinet_code が未設定の銘柄について検索APIで取得し DB に保存する。
    {code4: edinet_code} を返す。
    """
    conn = get_conn()
    cur  = conn.cursor()
    ph   = ",".join(["%s"] * len(codes))
    cur.execute(
        f"SELECT code, edinet_code FROM stocks WHERE code IN ({ph})", codes
    )
    existing = {r[0]: r[1] for r in cur.fetchall()}
    cur.close()
    conn.close()

    missing = [c for c in codes if not existing.get(c)]
    if not missing:
        return existing

    print(f"  edinet_code 検索: {len(missing)} 銘柄...")
    result = dict(existing)
    updated = 0

    for code4 in missing:
        ec = _search_edinet_code(code4)
        if ec:
            result[code4] = ec
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute(
                "UPDATE stocks SET edinet_code = %s WHERE code = %s",
                (ec, code4),
            )
            conn.commit()
            cur.close()
            conn.close()
            updated += 1

    print(f"  edinet_code 取得・更新: {updated} 件")
    return result


# ─── edinetdb.jp text-blocks API ─────────────────────────────────────────────
def fetch_biz_from_edinetdb(edinet_code: str) -> str | None:
    """
    edinetdb.jp から事業内容テキストを取得する。
    WANT_SECTIONS の順で連結して返す。失敗時は None。
    """
    try:
        time.sleep(API_DELAY)
        r = requests.get(
            f"{EDINETDB_BASE}/companies/{edinet_code}/text-blocks",
            params={"section": "business-overview"},
            headers=EDB_HEADERS,
            timeout=20,
        )
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            print("    [!] レート制限到達（429）")
            return None
        r.raise_for_status()

        blocks = {b["section"]: b["text"] for b in r.json().get("data", [])}

        parts = []
        for section in WANT_SECTIONS:
            if section in blocks and blocks[section].strip():
                parts.append(f"【{section}】\n{blocks[section].strip()}")

        if parts:
            combined = "\n\n".join(parts)
            return combined[:MAX_BIZ_LEN]

    except Exception as e:
        print(f"    [edinetdb] {edinet_code}: {e}")
    return None


# ─── フォールバック: kabutan.jp ─────────────────────────────────────────────
def _kabutan_fallback(code4: str) -> str | None:
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


# ─── DB 保存 ─────────────────────────────────────────────────────────────────
def _save_biz(code4: str, text: str):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE stocks SET business_description = %s, biz_updated_at = NOW() WHERE code = %s",
        (text, code4),
    )
    conn.commit()
    cur.close()
    conn.close()


# ─── メイン ──────────────────────────────────────────────────────────────────
def run(target_codes: list = None, force: bool = False):
    if not EDINETDB_KEY:
        print("ERROR: EDINETDB_API_KEY が設定されていません。")
        print("edinetdb.jp でキーを取得して .env に追記してください。")
        return

    print("=" * 60)
    print("  edinetdb.jp 事業内容バッチ取得")
    print("=" * 60)

    _ensure_columns()

    # 対象銘柄を取得
    print("\n[Step 1] 対象銘柄を特定")
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
            WHERE is_active = TRUE
            ORDER BY code
        """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not force:
        rows = [r for r in rows if r[2] is None]

    total = len(rows)
    print(f"  対象: {total} 銘柄")
    if total == 0:
        print("  全銘柄取得済みです（--force で強制更新）")
        return

    print(f"  今回の上限: {FREE_LIMIT} 件（無料プラン）")
    print()

    # edinet_code を事前取得（検索API・認証不要）
    all_codes = [r[0] for r in rows[:FREE_LIMIT + 50]]  # 多少余分に取得
    edinet_map = populate_edinet_codes(all_codes)
    print()

    # 事業内容を取得・保存
    print("[Step 2] 事業内容を取得・保存")
    ok = 0; fallback = 0; fail = 0
    api_calls = 0
    start_ts = time.time()

    for i, (code4, _, _) in enumerate(rows, 1):
        if api_calls >= FREE_LIMIT:
            print(f"\n  [!] 無料プラン上限 {FREE_LIMIT} 件に到達。")
            print(f"      本日分: {ok} 件取得成功（edinetdb.jp）。明日また実行してください。")
            break

        ec       = edinet_map.get(code4)
        biz_text = None

        # edinetdb.jp から取得
        if ec:
            biz_text = fetch_biz_from_edinetdb(ec)
            api_calls += 1
            if biz_text:
                ok += 1

        # フォールバック: kabutan.jp
        if not biz_text:
            time.sleep(0.3)
            biz_text = _kabutan_fallback(code4)
            if biz_text:
                fallback += 1
            else:
                fail += 1

        if biz_text:
            _save_biz(code4, biz_text)

        if i % 10 == 0 or i == total or i == 1:
            elapsed = time.time() - start_ts
            remain  = (elapsed / i * (total - i)) / 60 if i < total else 0
            print(f"  [{i:>4}/{total}]  OK:{ok:>4}(edinet)  KB:{fallback:>4}(kabutan)  "
                  f"失敗:{fail:>3}  API使用:{api_calls}  残り約{remain:.0f}分")

    elapsed_m = (time.time() - start_ts) / 60
    print()
    print("=" * 60)
    print(f"  完了（{elapsed_m:.1f}分）")
    print(f"  edinetdb.jp 取得: {ok} 銘柄")
    print(f"  kabutan フォールバック: {fallback} 銘柄")
    print(f"  全失敗: {fail} 銘柄")
    if total > ok + fallback + fail:
        remaining = total - ok - fallback - fail
        print(f"  未処理（明日以降）: {remaining} 銘柄")
    print("=" * 60)


if __name__ == "__main__":
    args         = sys.argv[1:]
    force        = "--force" in args
    target_codes = [a for a in args if not a.startswith("--")] or None
    run(target_codes=target_codes, force=force)
