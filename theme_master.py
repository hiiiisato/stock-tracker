#!/usr/bin/env python3
"""統一テーママスタ — みんかぶ(minkabu.jp)のテーマ×関連度を正として構築・維持する。

【背景（2026-07 ゼロベース再設計 → みんかぶ置換）】
kabutanタグ(1,531・バイナリ)はコングロマリット汚染があり、自前ヒューリスティック
(EDINET本文一致+Gemini)で関連度を推定していたが、みんかぶが**銘柄ごとの関連度スコア
(0-100)を公開**していると判明（例: 半導体80点=キオクシア・ルネサス・アドテスト・ローム・
東エレ）。運営は kabutan と同じミンカブ・ジ・インフォノイドで、テーマ体系は同一
（みんかぶ780テーマ中779がkabutanタグと名称一致）。編集済み関連度が取れる以上、
推定は不要 → **みんかぶを正とし、手動ロックだけを残すシンプルな設計に置換**。

【設計】
- themes        : みんかぶの全テーマ（指数構成・IPO年度などの非テーマは除外）。
                  status: active / archived。みんかぶから消えて21日で自動archive。
- theme_members : みんかぶの関連度(0-100)をそのまま relevance に保存。
                  tier: 70+=3(コア) / 50+=2(関連) / それ未満=1(周辺)。集計は tier>=2。
- 手動メンテ    : theme_members.manual_lock ('pin3'/'pin2'/'pin1'/'exclude') は
                  同期に常に勝つ（pin行は削除されない・excludeはみんかぶ収載でも除外）。
- 更新サイクル  : 毎日 DAILY_SYNC_THEMES 件ずつ古い順に巡回（全780テーマ≒週1周）。
                  テーマ一覧は毎回取得し新テーマを即日検出。取得失敗時は既存データ維持。

【アクセス上の注意】
minkabu.jp は User-Agent で bot 判定する（Chrome風UAは503・Safari系UA+Refererで200）。
ヘッダは MINKABU_HEADERS を必ず使う。サーバー(GitHub Actions)のIPで503が続く場合は
ログに残して既存データを維持する（壊れない）。

CLI:
  python theme_master.py              # 一覧同期 + 古い順に120テーマ更新（日次バッチ用）
  python theme_master.py --limit 30   # 更新テーマ数を指定
  python theme_master.py --theme 半導体  # 特定テーマのみ同期
"""
from __future__ import annotations

import html
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta

import requests

from config import get_conn

# ── 同期パラメータ ──────────────────────────────────────────
DAILY_SYNC_THEMES = 120   # 1回の実行で構成銘柄を更新するテーマ数（全~780を約1週間で一巡）
DELAY             = 0.6   # リクエスト間隔(秒)
ARCHIVE_DAYS      = 21    # みんかぶ一覧から消えてこの日数で自動archive
MAX_LIST_PAGES    = 60    # テーマ一覧の最大ページ数（暴走ガード）
MAX_MEMBER_PAGES  = 30    # 1テーマの構成銘柄最大ページ数（同上）

# tier閾値（みんかぶ関連度 → 階層）
# みんかぶは上位のみ関連度を細かく差別化し(60/70/75/80/90/100)、それ以外の「掲載はして
# いるが特に強調しない」銘柄は一律50%で表示する（差別化なしの下限値）。50を集計に含めると
# 実質フィルタが効かず「関連度の高い銘柄のみ」という要件を満たせないため、60を下限にする。
TIER3_REL = 70   # コア
TIER2_REL = 60   # 関連（集計対象の下限。みんかぶの非差別化フロア50は周辺(tier1)に落とす）

BASE = "https://minkabu.jp"
# Chrome風UAは503になる。Safari系UA+Referer必須（実測 2026-07）。
MINKABU_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Referer": "https://minkabu.jp/",
}

# 投資テーマではないもの（指数構成・IPO年度・市場区分など）は取り込まない
EXCLUDE_EXACT = {"あえてスタンダード", "その他"}
EXCLUDE_PATTERNS = re.compile(
    r"(日経|TOPIX|JPX|読売|MSCI|S&P|コア30|^\d{4}年のIPO$|グロース市場|プライム|スタンダード)"
)


def _is_investment_theme(name: str) -> bool:
    return name not in EXCLUDE_EXACT and not EXCLUDE_PATTERNS.search(name)


# ── ロングランテーマ（ユーザー完全手動指定・2026-07）─────────────────────
# テーマタブ上部に「値下がりしていても重要な構造テーマ」として別枠掲載する。
# featured='long' をこのリストと同期（リスト外のlongは解除。pin/banは別用途で不変）。
# 「AI」は みんかぶ名「人工知能」、「量子コンピュータ」は「量子コンピューター」に対応。
LONGRUN_THEMES = [
    "宇宙開発", "量子コンピューター", "核融合発電", "ドローン", "フィジカルAI",
    "ペロブスカイト太陽電池", "SaaS", "レアアース", "ロボット", "光デバイス",
    "防衛", "サイバーセキュリティ", "半導体", "データセンター", "人工知能",
]

# みんかぶに存在しないテーマの手動定義（origin='manual'・同期対象外・手動pinで構成）。
# 構成はみんかぶ関連テーマ＋EDINET事業内容の本文一致で検証したクリーンな銘柄のみ
# （kabutanタグのコングロ汚染〔宇宙にトヨタ・SaaSにNTT等〕は再現しない）。
# relevance: pin3(コア)=85 / pin2(関連)=65
MANUAL_THEMES: dict[str, dict] = {
    "宇宙開発": {
        "description": "ロケット・人工衛星・衛星データ・宇宙インフラ。衛星コンステレーションや月面探査など"
                       "国策と民間投資が重なる長期成長領域。",
        "core": ["186A", "290A", "402A", "464A", "9348", "9412"],   # アストロスケール/Synspective/アクセルスペース/QPS/ispace/スカパーJSAT
        "related": ["7011", "7012", "7013", "6503", "6701", "3741", "6946", "5572", "5570"],
    },
    "SaaS": {
        "description": "クラウド経由でソフトウェアを提供するサブスクリプション型ビジネス。"
                       "ストック収益の積み上げで成長する国内SaaS企業群。",
        "core": ["4478", "3994", "3923", "4776", "4443", "4071", "4475", "4194"],  # freee/マネフォ/ラクス/サイボウズ/Sansan/プラスアルファ/HENNGE/ビジョナル
        "related": ["4733", "3915", "3853", "3762", "2326", "5243", "3993", "4419", "4684"],
    },
}


def ensure_manual_and_longrun(cur, verbose: bool = True) -> None:
    """手動テーマ(MANUAL_THEMES)の作成と、ロングラン指定(featured='long')の同期。冪等。"""
    now = datetime.now().replace(microsecond=0)
    for name, spec in MANUAL_THEMES.items():
        cur.execute("SELECT id FROM themes WHERE name=%s", (name,))
        row = cur.fetchone()
        if row:
            tid = row[0]
            cur.execute("UPDATE themes SET status='active', "
                        "description=COALESCE(NULLIF(description,''), %s) WHERE id=%s",
                        (spec["description"], tid))
        else:
            cur.execute(
                "INSERT INTO themes (name, status, origin, description, created_at, updated_at) "
                "VALUES (%s,'active','manual',%s,%s,%s)", (name, spec["description"], now, now))
            tid = cur.lastrowid
        rows = ([(tid, c, 85, 3, "手動キュレーション(コア)", "pin3", now) for c in spec["core"]]
                + [(tid, c, 65, 2, "手動キュレーション(関連)", "pin2", now) for c in spec["related"]])
        cur.executemany("""
            INSERT INTO theme_members (theme_id, code, relevance, tier, evidence, manual_lock, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE relevance=VALUES(relevance), tier=VALUES(tier),
              evidence=VALUES(evidence), manual_lock=VALUES(manual_lock), updated_at=VALUES(updated_at)
        """, rows)
    # featured='long' をリストと同期（pin/banは触らない）
    ph = ",".join(["%s"] * len(LONGRUN_THEMES))
    cur.execute(f"UPDATE themes SET featured='long' WHERE name IN ({ph}) AND status='active'",
                LONGRUN_THEMES)
    cur.execute(f"UPDATE themes SET featured='' WHERE featured='long' AND name NOT IN ({ph})",
                LONGRUN_THEMES)
    if verbose:
        cur.execute("SELECT COUNT(*) FROM themes WHERE featured='long'")
        print(f"  ロングランテーマ: {cur.fetchone()[0]}件 (手動テーマ{len(MANUAL_THEMES)}件含む)")


# ═══════════════════════════════════════════════════════════
#  スキーマ
# ═══════════════════════════════════════════════════════════

def ensure_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS themes (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            name        VARCHAR(80) NOT NULL UNIQUE,
            status      VARCHAR(12) NOT NULL DEFAULT 'active',   -- active/archived
            origin      VARCHAR(20),          -- minkabu / curated / manual
            keywords    VARCHAR(255),         -- （旧設計の名残・手動メモ用に残置）
            description VARCHAR(1000),
            hot_rank    INT,
            last_seen   DATETIME,             -- みんかぶ一覧で最後に確認した日時
            last_synced DATETIME,             -- 構成銘柄を最後に同期した日時
            created_at  DATETIME,
            updated_at  DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS theme_members (
            theme_id    INT NOT NULL,
            code        VARCHAR(10) NOT NULL,
            relevance   TINYINT,          -- みんかぶ関連度(0-100)
            tier        TINYINT,          -- 3=コア/2=関連/1=周辺/0=除外
            evidence    VARCHAR(160),
            manual_lock VARCHAR(8) DEFAULT '',  -- ''/pin3/pin2/pin1/exclude（同期に常に勝つ）
            ai_verdict  TINYINT,          -- （旧設計の名残）
            updated_at  DATETIME,
            PRIMARY KEY (theme_id, code)
        )
    """)
    # 旧スキーマからの移行（列が無ければ追加・説明文は長いので拡張）
    # featured: 大テーマ(ロングラン)の手動指定。'pin'=常時掲載 / 'ban'=除外 / ''=自動判定
    for ddl in ["ADD COLUMN last_seen DATETIME", "ADD COLUMN last_synced DATETIME",
                "MODIFY description VARCHAR(1000)",
                "ADD COLUMN featured VARCHAR(4) DEFAULT ''"]:
        try:
            cur.execute(f"ALTER TABLE themes {ddl}")
        except Exception:  # noqa: BLE001  既に存在
            pass


# ═══════════════════════════════════════════════════════════
#  みんかぶ取得
# ═══════════════════════════════════════════════════════════

def _get(session: requests.Session, url: str, params: dict | None = None) -> requests.Response:
    r = session.get(url, params=params, headers=MINKABU_HEADERS, timeout=20)
    r.raise_for_status()
    return r


def fetch_theme_list(session: requests.Session) -> list[str]:
    """みんかぶの全テーマ名を一覧ページングで取得（~780件・39ページ）。"""
    names: list[str] = []
    seen: set[str] = set()
    for page in range(1, MAX_LIST_PAGES + 1):
        r = _get(session, f"{BASE}/theme", {"page": page} if page > 1 else None)
        # href内の値をURLデコード後、HTMLエンティティも復元（例: M&amp;A → M&A）。
        # エンティティのまま保存すると /theme/M&amp;A が404になり同期できない。
        found = [html.unescape(urllib.parse.unquote(m))
                 for m in re.findall(r'href="/theme/([^"?]+)"', r.text) if "ranking" not in m]
        new = [n for n in found if n not in seen]
        if not new:
            break
        for n in new:
            seen.add(n)
            names.append(n)
        time.sleep(DELAY)
    return names


def fetch_members(session: requests.Session, theme: str) -> tuple[dict[str, int], str]:
    """1テーマの構成銘柄と関連度を全ページ取得。({code: relevance}, 説明文)"""
    members: dict[str, int] = {}
    description = ""
    quoted = urllib.parse.quote(theme)
    for page in range(1, MAX_MEMBER_PAGES + 1):
        r = _get(session, f"{BASE}/theme/{quoted}", {"page": page} if page > 1 else None)
        if page == 1:
            # テーマ説明文（p.c_caution）。定型プレフィックス「株式テーマ「X」に関連する
            # 銘柄一覧です。〜掲載しています。」を除去して本文だけ残す
            m = re.search(r'<p class="c_caution[^"]*">(.*?)</p>', r.text, re.S)
            if m:
                raw = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
                raw = re.sub(r"^株式テーマ「.*?」に関連する銘柄一覧です。", "", raw)
                raw = re.sub(r"^このテーマに関連する\d+銘柄の株価、前日比、関連度を掲載しています。", "", raw)
                description = raw.strip()[:1000]
        # 行ごとに (コード, 関連度) を抽出。関連度はVueコンポーネントの :value 属性
        chunks = re.split(r"<tr[ >]", r.text)[1:]
        added = 0
        for ch in chunks:
            # 証券コードは常に4文字（旧: 4桁数字のみ／新: 3桁数字+英字 例"285A"）。
            # 旧 \d{4}[A-Z0-9]? は新形式コードにマッチせずその行を丸ごと欠落させていた。
            mc = re.search(r'/stock/([0-9A-Z]{4})"', ch)
            mv = re.search(r'relationship-percentages-graph\s+:value="(\d+)"', ch)
            if mc and mc.group(1) not in members:
                members[mc.group(1)] = int(mv.group(1)) if mv else TIER2_REL
                added += 1
        if added == 0:
            break
        time.sleep(DELAY)
    return members, description


# ═══════════════════════════════════════════════════════════
#  同期
# ═══════════════════════════════════════════════════════════

def _tier(rel: int) -> int:
    return 3 if rel >= TIER3_REL else 2 if rel >= TIER2_REL else 1


def sync(max_themes: int = DAILY_SYNC_THEMES, only_theme: str | None = None,
         verbose: bool = True) -> dict:
    """みんかぶ同期の本体。①テーマ一覧→新規検出・last_seen更新・消滅archive
    ②古い順に max_themes 件の構成銘柄を更新。失敗時は既存データを維持して続行。"""
    stats = {"themes_seen": 0, "new_themes": 0, "synced": 0, "member_rows": 0,
             "archived": 0, "failed": 0}
    # updated_at列はDATETIME(秒精度)。Pythonのdatetime.now()はマイクロ秒を持つため、
    # INSERT時に丸め/切り捨てが起き、後続のDELETE(updated_at < now)がその丸め方向次第で
    # 今入れたばかりの行を消してしまう(非決定的バグ)。秒精度に揃えて完全に無害化する。
    now = datetime.now().replace(microsecond=0)
    session = requests.Session()
    conn = get_conn()
    cur = conn.cursor()
    ensure_tables(cur)
    ensure_manual_and_longrun(cur, verbose=verbose)
    conn.commit()

    # ① テーマ一覧の同期（only_theme指定時はスキップ）
    if only_theme is None:
        try:
            names = fetch_theme_list(session)
        except Exception as e:  # noqa: BLE001
            print(f"  [一覧] 取得失敗（既存データ維持で続行）: {str(e)[:80]}")
            names = []
        stats["themes_seen"] = len(names)
        if names:
            cur.execute("SELECT name FROM themes")
            existing = {r[0] for r in cur.fetchall()}
            for n in names:
                if not _is_investment_theme(n):
                    continue
                if n in existing:
                    # 旧設計(curated/kabutan_hot/flow_proven)の同名テーマも minkabu 管理に一本化
                    cur.execute("UPDATE themes SET origin='minkabu', status='active', last_seen=%s "
                                "WHERE name=%s", (now, n))
                else:
                    cur.execute(
                        "INSERT INTO themes (name, status, origin, last_seen, created_at, updated_at) "
                        "VALUES (%s,'active','minkabu',%s,%s,%s)", (n, now, now, now))
                    stats["new_themes"] += 1
            # みんかぶから消えて ARCHIVE_DAYS 経過（かつ手動pinなし）→ archive
            cur.execute("""
                UPDATE themes t SET t.status='archived', t.updated_at=%s
                WHERE t.origin='minkabu' AND t.status='active'
                  AND (t.last_seen IS NULL OR t.last_seen < %s)
                  AND NOT EXISTS (SELECT 1 FROM theme_members tm
                                  WHERE tm.theme_id=t.id AND tm.manual_lock LIKE 'pin%%')
            """, (now, now - timedelta(days=ARCHIVE_DAYS)))
            stats["archived"] = cur.rowcount
            # みんかぶに無い旧設計テーマ: 手動pinがあれば残置(自前キュレーション)、
            # 無ければ archive。pin以外の旧ヒューリスティック行はデータ源が無いので掃除。
            cur.execute("""
                UPDATE themes t SET t.status='archived', t.updated_at=%s
                WHERE t.origin != 'minkabu' AND t.status='active'
                  AND NOT EXISTS (SELECT 1 FROM theme_members tm
                                  WHERE tm.theme_id=t.id AND tm.manual_lock LIKE 'pin%%')
            """, (now,))
            stats["archived"] += cur.rowcount
            cur.execute("""
                DELETE tm FROM theme_members tm
                JOIN themes t ON t.id = tm.theme_id
                WHERE t.origin != 'minkabu' AND tm.manual_lock NOT LIKE 'pin%%'
            """)
            conn.commit()

    # ② 構成銘柄の更新（古い順に巡回）
    if only_theme:
        cur.execute("SELECT id, name FROM themes WHERE name=%s", (only_theme,))
    else:
        cur.execute("""
            SELECT id, name FROM themes
            WHERE status='active' AND origin='minkabu'
            ORDER BY last_synced IS NULL DESC, last_synced ASC
            LIMIT %s
        """, (max_themes,))
    targets = cur.fetchall()

    for tid, tname in targets:
        try:
            members, description = fetch_members(session, tname)
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            if verbose:
                print(f"  [{tname}] 取得失敗（既存維持）: {str(e)[:60]}")
            time.sleep(DELAY * 3)
            continue
        if not members:
            stats["failed"] += 1
            continue

        rows_src = [(code, rel) for code, rel in members.items()]

        # DB書き込み部分。長時間走る一括同期では TiDB がアイドル接続を切ることがあるため、
        # 切断を検知したら再接続して同じテーマの書き込みを1回だけリトライする（壊れない設計）。
        for attempt in range(2):
            try:
                cur.execute("SELECT code, manual_lock FROM theme_members WHERE theme_id=%s", (tid,))
                locks = {r[0]: (r[1] or "") for r in cur.fetchall()}
                rows = []
                for code, rel in rows_src:
                    lock = locks.get(code, "")
                    if lock == "exclude":
                        tier = 0
                    elif lock.startswith("pin"):
                        tier = int(lock[3])
                    else:
                        tier = _tier(rel)
                    rows.append((tid, code, rel, tier, f"みんかぶ関連度{rel}", lock, now))
                cur.executemany("""
                    INSERT INTO theme_members (theme_id, code, relevance, tier, evidence, manual_lock, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      relevance=VALUES(relevance), tier=VALUES(tier),
                      evidence=VALUES(evidence), updated_at=VALUES(updated_at)
                """, rows)
                # みんかぶから消えた銘柄を削除（pin行は残し、tierをpin値で維持）
                cur.execute("""
                    DELETE FROM theme_members
                    WHERE theme_id=%s AND updated_at < %s AND manual_lock NOT LIKE 'pin%%'
                """, (tid, now))
                cur.execute("UPDATE themes SET last_synced=%s, updated_at=%s, "
                            "description=COALESCE(NULLIF(%s,''), description) WHERE id=%s",
                            (now, now, description, tid))
                conn.commit()
                stats["synced"] += 1
                stats["member_rows"] += len(rows)
                if verbose:
                    print(f"  [{tname}] {len(rows)}銘柄 (コア{sum(1 for r in rows if r[3] == 3)})")
                break
            except Exception as e:  # noqa: BLE001  接続断など
                if attempt == 0:
                    if verbose:
                        print(f"  [{tname}] DB書込失敗・再接続してリトライ: {str(e)[:60]}")
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    conn = get_conn()
                    cur = conn.cursor()
                else:
                    stats["failed"] += 1
                    if verbose:
                        print(f"  [{tname}] リトライも失敗・スキップ: {str(e)[:60]}")

    cur.close()
    conn.close()
    if verbose:
        print(f"完了: 一覧{stats['themes_seen']} / 新規{stats['new_themes']}"
              f" / 更新{stats['synced']}テーマ{stats['member_rows']}行"
              f" / archive{stats['archived']} / 失敗{stats['failed']}")
    return stats


# ═══════════════════════════════════════════════════════════
#  公開ヘルパ（他モジュール用）
# ═══════════════════════════════════════════════════════════

def load_theme_groups(cur, min_tier: int = 2) -> tuple[dict, dict]:
    """money_flow等の集計用: groups[code]=[('theme', name),...], labels。
    activeテーマ × tier>=min_tier のみ。"""
    groups: dict[str, list] = defaultdict(list)
    labels: dict[tuple, str] = {}
    cur.execute("""
        SELECT t.name, tm.code FROM theme_members tm
        JOIN themes t ON t.id = tm.theme_id
        WHERE t.status = 'active' AND tm.tier >= %s
    """, (min_tier,))
    for name, code in cur.fetchall():
        groups[code].append(("theme", name))
        labels[("theme", name)] = name
    return groups, labels


def run_daily() -> int:
    """日次バッチ用: 一覧同期 + 古い順に DAILY_SYNC_THEMES 件更新。"""
    stats = sync(verbose=False)
    return stats["synced"]


if __name__ == "__main__":
    if "--theme" in sys.argv:
        t = sys.argv[sys.argv.index("--theme") + 1]
        sync(only_theme=t)
    else:
        limit = DAILY_SYNC_THEMES
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        sync(max_themes=limit)
