"""銘柄名の名寄せ（別名インデックス）。

■ 解決したい問題
  銘柄マスタ(`stocks.name`)はJPX/J-Quantsの正式表記で、英字社名は**全角**で入る
  （例: 7735 = ＳＣＲＥＥＮホールディングス）。一方、AI要約・ニュース・YouTube書き起こしは
  **読み（カタカナ）や略称**で社名を書く（例: スクリーンホールディングス / スクリーンHD）。
  正式表記との文字列一致では絶対に繋がらないため、社名→コードの解決に失敗し
  個別銘柄ページへのリンクが張られない、サイト内検索でも見つからない、という事象が起きる。

■ 方針（kabutan/Yahooファイナンス等の銘柄検索と同じモデル）
  「正式名称」だけでなく **読み・別名を持つ名寄せテーブル** を用意し、
  正規化した文字列で引く。読みは **EDINETコードリスト（金融庁・公式）の
  「提出者名（ヨミ）」** を一次ソースとする（全上場企業を無料でカバー・毎日更新）。

■ テーブル `stock_aliases`
  alias(正規化済み) → code。tier 1 = 正式名/読み、tier 2 = 接尾辞(ホールディングス等)省略形。
  引く側は「一意に定まる時だけ採用」する（曖昧な別名で誤リンクさせないため）。
  日次バッチ（daily_run の銘柄マスタ更新の直後）で再構築される。
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections import defaultdict

import requests

from config import bulk_upsert, db

# 金融庁EDINET コードリスト（APIキー不要の公開ZIP。証券コード＋提出者名＋提出者名（ヨミ））
EDINET_CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"

# 名寄せで無視する記号・空白（長音符も落として「コンピュータ/コンピューター」等の揺れを吸収）
_DROP = re.compile(r"[\s　・,，.．\-‐－―ー~〜()（）\[\]「」『』\"'／/\\&＆＋+]")
# 法人格（正式表記・読みの両方）
_CORP = re.compile(r"株式会社|㈱|\(株\)|有限会社|合同会社|カブシキガイシャ|カブシキカイシャ")
# 接尾辞（_DROP適用後の形で書く: 長音符が落ちるため「ホールディングス」→「ホルディングス」）
_SUFFIX = re.compile(r"(ホルディングス|ホルデイングス|HOLDINGS|HLDGS|HD|グルプ|GROUP|GP|"
                     r"フィナンシャルグルプ|CORPORATION|CORP|COLTD|INC|LTD)+$")


def normalize(name: str) -> str:
    """名寄せキーへ正規化。NFKC（全角英数→半角）→大文字化→法人格除去→記号・長音除去。"""
    s = unicodedata.normalize("NFKC", name or "").upper()
    return _DROP.sub("", _CORP.sub("", s))


def core(alias: str) -> str:
    """正規化済み別名から接尾辞（ホールディングス/グループ等）を除いた芯を返す。"""
    return _SUFFIX.sub("", alias)


def ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_aliases (
            alias  VARCHAR(120) NOT NULL,   -- 正規化済み別名（normalize()の出力）
            code   VARCHAR(10)  NOT NULL,
            tier   TINYINT      NOT NULL DEFAULT 1,  -- 1=正式名/読み, 2=接尾辞省略形
            source VARCHAR(12),                      -- edinet / yomi / master / master_en
            PRIMARY KEY (alias, tier, code),
            KEY idx_alias (alias),
            KEY idx_code (code)
        )
    """)


# ═══════════════════════════════════════════════════════════
#  構築
# ═══════════════════════════════════════════════════════════
def _fetch_edinet_codelist() -> list[tuple[str, str, str]]:
    """EDINETコードリストから (証券コード4桁, 提出者名, 提出者名（ヨミ）) を返す。"""
    r = requests.get(EDINET_CODELIST_URL, timeout=120,
                     headers={"User-Agent": "Mozilla/5.0 (stock-tracker)"})
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        raw = z.read(name).decode("cp932", errors="replace")
    rd = csv.reader(io.StringIO(raw))
    next(rd, None)                      # 1行目: ダウンロード実行日
    header = next(rd, None) or []
    idx = {h: i for i, h in enumerate(header)}
    try:
        i_sec, i_nm, i_ym = idx["証券コード"], idx["提出者名"], idx["提出者名（ヨミ）"]
    except KeyError:                    # 列構成が変わったら諦める（既存の別名は壊さない）
        return []
    out = []
    for row in rd:
        if len(row) <= max(i_sec, i_nm, i_ym):
            continue
        sec = row[i_sec].strip()
        if len(sec) != 5 or not sec[:4].isalnum():
            continue                    # 証券コードは5桁（末尾0埋め）。空欄=非上場
        out.append((sec[:4], row[i_nm].strip(), row[i_ym].strip()))
    return out


def build(cur, rebuild: bool = False) -> int:
    """別名インデックスを更新して投入件数を返す。

    ソース: ①EDINET「提出者名（ヨミ）」= カタカナ読み ②EDINET「提出者名」
            ③stocks.name（マスタ正式名） ④stocks.name_en（英語名）
    上場中(is_active=1)の銘柄に限定し、存在しないコードへのリンクを作らない。

    更新は**追記型**。社名変更（例: 日本ガイシ→ＮＧＫ）があっても旧社名の別名を残し、
    過去記事・過去要約の社名からも引けるようにする（kabutan等の「旧社名で検索できる」挙動）。
    掃除は「上場廃止コードの行を消す」だけ。正規化ルールを変えた時は rebuild=True で全張り替え。
    """
    ensure_table(cur)
    cur.execute("SELECT code, name, name_en FROM stocks WHERE is_active = 1")
    master = cur.fetchall()
    active = {r[0] for r in master}

    # alias -> {(tier, source, code)} を組み立て
    pairs: dict[tuple[str, int], set[str]] = defaultdict(set)
    src: dict[tuple[str, int, str], str] = {}

    def add(name: str, code: str, source: str) -> None:
        a = normalize(name)
        if len(a) < 2:
            return
        pairs[(a, 1)].add(code)
        src[(a, 1, code)] = source
        c = core(a)
        if len(c) >= 2 and c != a:
            pairs[(c, 2)].add(code)
            src.setdefault((c, 2, code), source)

    for code, nm, nm_en in master:
        add(nm or "", code, "master")
        if nm_en:
            add(nm_en, code, "master_en")

    try:
        edinet = _fetch_edinet_codelist()
    except Exception as e:  # noqa: BLE001  取得失敗時はマスタ由来だけで再構築（欠落させない）
        print(f"  EDINETコードリスト取得失敗（マスタ名のみで構築）: {str(e)[:80]}")
        edinet = []
    for code, nm, yomi in edinet:
        if code not in active:
            continue
        add(nm, code, "edinet")
        add(yomi, code, "yomi")

    rows = [(a, code, tier, src.get((a, tier, code), "master"))
            for (a, tier), codes in pairs.items() for code in codes]
    if not rows:
        return 0
    if rebuild:
        cur.execute("DELETE FROM stock_aliases")
    else:
        # 上場廃止コードの別名だけ掃除（旧社名の別名は残す＝追記型）
        cur.execute("DELETE a FROM stock_aliases a "
                    "LEFT JOIN stocks s ON s.code = a.code AND s.is_active = 1 "
                    "WHERE s.code IS NULL")
    bulk_upsert(cur, "stock_aliases", ["alias", "code", "tier", "source"], rows,
                update_cols=["source"])
    return len(rows)


def refresh(rebuild: bool = False) -> int:
    """バッチ用エントリポイント（自前で接続を張る）。"""
    with db() as cur:
        n = build(cur, rebuild=rebuild)
    return n


# ═══════════════════════════════════════════════════════════
#  参照
# ═══════════════════════════════════════════════════════════
def resolve(cur, name: str) -> str:
    """社名（表記ゆれ・読み・略称）から銘柄コードを解決する。
    一意に定まらない別名（例: 「フジ」＝8278/6134）は誤リンクを避けて空を返す。"""
    a = normalize(name)
    if len(a) < 2:
        return ""
    for key, tier in ((a, 1), (a, 2), (core(a), 1), (core(a), 2)):
        if len(key) < 2:
            continue
        cur.execute("SELECT DISTINCT code FROM stock_aliases WHERE alias=%s AND tier=%s LIMIT 2",
                    (key, tier))
        rows = cur.fetchall()
        if len(rows) == 1:
            return rows[0][0]
    return ""


def matches(cur, name: str) -> list[str]:
    """一意でなくてもよい候補コード一覧（サイト内検索用）。
    「スクリーンHD」のような接尾辞違いでも引けるよう、芯（core）でも前方一致させる。"""
    keys = {k for k in (normalize(name), core(normalize(name))) if len(k) >= 2}
    if not keys:
        return []
    cond = " OR ".join(["alias = %s OR alias LIKE %s"] * len(keys))
    args = [v for k in keys for v in (k, f"{k}%")]
    cur.execute(f"SELECT DISTINCT code FROM stock_aliases WHERE {cond} LIMIT 20", args)
    return [r[0] for r in cur.fetchall()]


if __name__ == "__main__":
    import sys
    full = "--rebuild" in sys.argv        # 正規化ルールを変えた時だけ全張り替え
    print(f"別名インデックス{'再構築' if full else '更新'}: {refresh(rebuild=full):,} 件投入")
