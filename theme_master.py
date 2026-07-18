#!/usr/bin/env python3
"""統一テーママスタ — 関連度スコア付きのテーマ×銘柄マスタを構築・維持する。

【背景（ゼロベース再設計 2026-07）】
従来は「自前キュレーション(21テーマ・196行・高品質少量)」と「kabutanタグ(1,531テーマ・
5.2万行・広量ノイズ)」の二重構造で、コングロマリット（多数テーマに緩く付与された大型株）が
ニッチテーマの指標・代表銘柄を汚染していた。本モジュールで単一のマスタに統一する。

【設計】
- themes        : 厳選テーママスタ(~100)。自前キュレーション + kabutan人気ランキング +
                  資金流入実績から選定。指数構成・市場区分・地域などの「非テーマ」は除外。
- theme_members : 銘柄×テーマの関連度スコア(0-100)とtier(3=コア/2=関連/1=周辺/0=除外)。
                  集計(資金フロー・テーマ指数)は tier>=2 のみを使う。
- 関連度 = 複数証拠の合成:
    kabutanタグ(+30) + EDINET事業内容/セグメント本文とのキーワード一致(+35〜45)
    + テーマ集中度(タグ少ない銘柄+15/コングロ-15) + Gemini判定(曖昧帯のみ) + 手動ロック(最優先)
  → コングロ問題は「本文一致」で原理的に解決（例: 三菱UFJの事業内容に消費者金融は出ない）。
- 手動メンテ: theme_members.manual_lock ('pin3'/'pin2'/'pin1'/'exclude') は自動更新に常に勝つ。
  themes.status ('active'/'candidate'/'archived') で新テーマの承認フローを表現。

【更新サイクル】
- 週次: build_themes(新テーマ候補検出) → score_members(全関連度再計算) → ai_review(曖昧帯)
- kabutanタグ自体は company_profile.py が日次収集（証拠データとして継続利用）

CLI:
  python theme_master.py            # build + score（AI判定なし）
  python theme_master.py --ai       # build + score + Gemini曖昧帯判定
  python theme_master.py --score    # スコア再計算のみ
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime

import requests

from config import get_conn, bulk_upsert, GEMINI_API_KEY

# ── テーマ選定パラメータ ──────────────────────────────────────
MAX_THEMES        = 100   # 厳選上限
MIN_MEMBERS       = 5     # テーマとして成立する最小構成銘柄数(tier>=2)
FLOW_PROVEN_WEEKS = 13    # 資金流入実績を見る週数
FLOW_PROVEN_MIN   = 2     # この回数以上 inflow になったテーマを候補にする

# スコアリング閾値
TIER3_SCORE = 70   # コア
TIER2_SCORE = 50   # 関連（集計対象の下限）
TIER1_SCORE = 32   # 周辺（表示のみ・集計外）
AI_BAND     = (32, 49)   # この帯のみGemini判定に回す

GEMINI_MODEL = "gemini-2.5-flash-lite"
AI_MAX_PAIRS = 400       # 1回の実行でAI判定する最大ペア数（コスト管理）

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
      "Accept-Language": "ja"}

# 「投資テーマではない」kabutanタグの除外。指数構成・市場区分・地域・スタイル系。
# スタイル(高配当等)・規模は money_flow の style/size 軸で既にカバーしている。
EXCLUDE_EXACT = {
    "あえてスタンダード", "ディフェンシブ", "中国関連", "アジア関連", "インド関連",
    "ベトナム関連", "インバウンド", "内需関連", "外需関連", "円安メリット", "円高メリット",
    "IT関連", "サービス業", "その他",
}
EXCLUDE_PATTERNS = re.compile(
    r"(日経|TOPIX|JPX|読売|MSCI|S&P|コア30|大型株|中型株|小型株|グロース市場|プライム|スタンダード"
    r"|高配当|株主優待|低PBR|低PER|増配|自社株買い)"
)

# 主要テーマのEDINET本文一致用キーワード（テーマ名以外の同義語）。themes.keywords に
# シードとして保存され、以後はDB側の値が正（手動で育てられる）。
KEYWORD_SEED: dict[str, str] = {
    "半導体":            "半導体,ウェーハ,ファウンドリ",
    "半導体製造装置":     "半導体製造装置,露光,エッチング,成膜,ダイシング",
    "半導体部材・部品":   "半導体,フォトレジスト,シリコンウェーハ,封止",
    "人工知能":          "人工知能,AI,機械学習,深層学習,生成AI",
    "フィジカルAI":       "ロボット,自動化,FA,組み込みAI,エッジAI",
    "データセンター":     "データセンター,クラウド基盤,サーバー",
    "サーバー冷却":       "冷却,空調,熱管理,液冷",
    "サイバーセキュリティ": "セキュリティ,ゼロトラスト,EDR,脆弱性",
    "SaaS":              "SaaS,クラウドサービス,サブスクリプション",
    "ドローン":           "ドローン,無人機,UAV",
    "宇宙開発関連":       "宇宙,衛星,ロケット",
    "防衛":              "防衛,艦艇,ミサイル,航空機,火工品",
    "蓄電池":            "電池,蓄電,バッテリー,リチウム",
    "核融合発電":         "核融合,フュージョン,プラズマ",
    "量子コンピューター":  "量子,量子計算,量子暗号",
    "ペロブスカイト太陽電池": "ペロブスカイト,太陽電池,太陽光",
    "レアアース":         "レアアース,希土類,磁石",
    "地方銀行":           "銀行業,地方銀行,信用金庫",
    "外食":              "外食,レストラン,飲食店",
    "ゲーム関連":         "ゲーム,オンラインゲーム,ソーシャルゲーム",
    "ステーブルコイン":    "ステーブルコイン,暗号資産,ブロックチェーン",
    "ロボット":           "ロボット,減速機,アクチュエータ,FA",
    "総合商社":           "総合商社,商社",
    # 自前キュレーションテーマ（theme_categories level2）
    "生成AI・LLM":        "生成AI,LLM,大規模言語モデル,AIサービス",
    "AIインフラ・DC":      "データセンター,GPU,冷却,電源装置",
    "クラウド・SaaS":      "クラウド,SaaS,サブスクリプション",
    "ロボット・FA":        "ロボット,FA,減速機,自動化",
    "防衛装備":           "防衛,艦艇,ミサイル,航空機,火工品",
    "宇宙":              "宇宙,衛星,ロケット,月面",
    "再生可能エネルギー":  "太陽光,風力,地熱,再生可能エネルギー",
    "電池・蓄電":         "電池,蓄電,バッテリー,リチウム",
    "水素・アンモニア":    "水素,アンモニア,燃料電池",
    "インバウンド消費":    "インバウンド,免税,観光,ホテル",
    "医療DX":            "電子カルテ,医療DX,遠隔医療",
    "EV・次世代車":       "EV,電気自動車,充電,車載",
}


# ═══════════════════════════════════════════════════════════
#  スキーマ
# ═══════════════════════════════════════════════════════════

def ensure_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS themes (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            name        VARCHAR(80) NOT NULL UNIQUE,   -- kabutanタグ名と照合するキー
            status      VARCHAR(12) NOT NULL DEFAULT 'candidate',  -- active/candidate/archived
            origin      VARCHAR(20),                    -- curated/kabutan_hot/flow_proven/manual
            keywords    VARCHAR(255),                   -- EDINET本文一致用（カンマ区切り・手動編集可）
            description VARCHAR(255),
            hot_rank    INT,                            -- kabutan人気ランキング順位(直近)
            created_at  DATETIME,
            updated_at  DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS theme_members (
            theme_id    INT NOT NULL,
            code        VARCHAR(10) NOT NULL,
            relevance   TINYINT,          -- 0-100スコア
            tier        TINYINT,          -- 3=コア/2=関連/1=周辺/0=除外
            evidence    VARCHAR(160),     -- 人間可読の根拠
            manual_lock VARCHAR(8) DEFAULT '',  -- ''/pin3/pin2/pin1/exclude（自動更新に常に勝つ）
            ai_verdict  TINYINT,          -- Gemini判定 1=関連/0=非関連/NULL=未判定
            updated_at  DATETIME,
            PRIMARY KEY (theme_id, code)
        )
    """)


# ═══════════════════════════════════════════════════════════
#  テーマ選定
# ═══════════════════════════════════════════════════════════

def _is_investment_theme(name: str, sector_names: set[str]) -> bool:
    if name in EXCLUDE_EXACT or name in sector_names:
        return False
    return not EXCLUDE_PATTERNS.search(name)


def fetch_hot_ranking() -> list[str]:
    """kabutan人気テーマランキング(3日間)TOP30を取得。失敗時は空リスト（致命的でない）。"""
    try:
        r = requests.get("https://kabutan.jp/info/accessranking/3_2", headers=UA, timeout=15)
        r.raise_for_status()
        names = []
        for m in re.finditer(r'/themes/\?theme=([^&"\']+)', r.text):
            nm = urllib.parse.unquote(m.group(1))
            if nm not in names:
                names.append(nm)
        return names
    except Exception as e:  # noqa: BLE001
        print(f"  [hot_ranking] 取得失敗(スキップ可): {str(e)[:60]}")
        return []


def build_themes(cur, verbose: bool = True) -> None:
    """厳選テーマリストを構築・更新する。既存テーマのstatusは尊重（勝手にarchiveしない）。"""
    now = datetime.now()
    cur.execute("SELECT name FROM sectors")
    sector_names = {r[0] for r in cur.fetchall()}

    # 候補を優先度順に集める: (name, origin, hot_rank)
    picks: dict[str, tuple[str, int | None]] = {}

    # ① 自前キュレーション（theme_categories level2）— 全て採用
    cur.execute("SELECT name, description FROM theme_categories WHERE level = 2")
    curated = cur.fetchall()
    for name, _desc in curated:
        picks[name] = ("curated", None)

    # ② kabutan人気ランキングTOP30（非テーマ除外）
    hot = fetch_hot_ranking()
    for i, name in enumerate(hot, 1):
        if name not in picks and _is_investment_theme(name, sector_names):
            picks[name] = ("kabutan_hot", i)
        elif name in picks:
            picks[name] = (picks[name][0], i)   # 既存でも順位は更新

    # ③ 資金流入実績（直近13週でinflowが2回以上のkabutanテーマ）
    cur.execute("""
        SELECT group_key, COUNT(*) FROM money_flow_weekly
        WHERE group_type='theme' AND flow_class='inflow'
          AND week_end > DATE_SUB(CURDATE(), INTERVAL %s WEEK)
        GROUP BY group_key HAVING COUNT(*) >= %s
        ORDER BY COUNT(*) DESC
    """, (FLOW_PROVEN_WEEKS, FLOW_PROVEN_MIN))
    for name, _cnt in cur.fetchall():
        if len(picks) >= MAX_THEMES:
            break
        if name not in picks and _is_investment_theme(name, sector_names):
            picks[name] = ("flow_proven", None)

    # upsert（新規=candidate、自前とhotはactiveで開始。既存statusは保持）
    cur.execute("SELECT name, status FROM themes")
    existing = {r[0]: r[1] for r in cur.fetchall()}
    rows = []
    for name, (origin, rank) in list(picks.items())[:MAX_THEMES]:
        status = existing.get(name) or ("active" if origin in ("curated", "kabutan_hot") else "candidate")
        rows.append([name, status, origin, KEYWORD_SEED.get(name, name), rank, now, now])
    bulk_upsert(cur, "themes",
                ["name", "status", "origin", "keywords", "hot_rank", "created_at", "updated_at"],
                rows,
                update_cols=["origin", "hot_rank", "updated_at"])   # status/keywordsは既存値を守る
    if verbose:
        print(f"  テーマ: {len(rows)}件 (自前{sum(1 for _,(o,_r) in picks.items() if o=='curated')}"
              f" / 人気{sum(1 for _,(o,_r) in picks.items() if o=='kabutan_hot')}"
              f" / 流入実績{sum(1 for _,(o,_r) in picks.items() if o=='flow_proven')})")


def migrate_curated(cur, verbose: bool = True) -> None:
    """自前キュレーション(stock_themes 196行・relevance3/2/1)を manual_lock='pin{r}' として移行。
    冪等（既にlockがある行は触らない）。既存の手作業資産を新マスタで保護する。"""
    now = datetime.now()
    cur.execute("""
        SELECT tc.name, st.code, st.relevance
        FROM stock_themes st JOIN theme_categories tc ON tc.id = st.theme_id
    """)
    rows = cur.fetchall()
    cur.execute("SELECT name, id FROM themes")
    tid_map = dict(cur.fetchall())
    n = 0
    for tname, code, rel in rows:
        tid = tid_map.get(tname)
        if not tid:
            continue
        cur.execute("""
            INSERT INTO theme_members (theme_id, code, relevance, tier, evidence, manual_lock, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              manual_lock = IF(manual_lock='', VALUES(manual_lock), manual_lock)
        """, (tid, code, {3: 85, 2: 60, 1: 40}[int(rel)], int(rel), "自前キュレーション",
              f"pin{int(rel)}", now))
        n += 1
    if verbose:
        print(f"  自前キュレーション移行: {n}行(pin)")


# ═══════════════════════════════════════════════════════════
#  関連度スコアリング
# ═══════════════════════════════════════════════════════════

def score_members(cur, verbose: bool = True) -> None:
    """全active/candidateテーマの構成銘柄と関連度を再計算する。manual_lock/ai_verdictは保持。"""
    now = datetime.now()
    cur.execute("SELECT id, name, keywords FROM themes WHERE status IN ('active','candidate')")
    themes = cur.fetchall()

    # 銘柄側の証拠データを一括ロード
    cur.execute("""
        SELECT s.code, CONCAT_WS(' ', s.business_summary, s.business_description) AS biz
        FROM stocks s WHERE s.is_active = 1 AND s.market_id IN (2,3,4)
    """)
    biz = {r[0]: (r[1] or "") for r in cur.fetchall()}
    # GROUP_CONCATは既定1024バイトでUTF-8を分断しdecodeエラーになるためPython側で集約
    cur.execute("SELECT DISTINCT code, segment_name FROM company_segments")
    segs: dict[str, str] = defaultdict(str)
    for code, seg in cur.fetchall():
        if seg:
            segs[code] += " " + seg
    cur.execute("SELECT code, theme FROM kabutan_themes")
    tag_map: dict[str, set] = defaultdict(set)
    for code, th in cur.fetchall():
        tag_map[code].add(th)
    tag_cnt = {c: len(ts) for c, ts in tag_map.items()}

    # 既存の manual_lock / ai_verdict を保持
    cur.execute("SELECT theme_id, code, manual_lock, ai_verdict FROM theme_members")
    locks: dict[tuple, tuple] = {(r[0], r[1]): (r[2] or "", r[3]) for r in cur.fetchall()}

    all_rows = []
    for tid, tname, keywords in themes:
        kws = [k.strip() for k in (keywords or tname).split(",") if k.strip()]
        # 候補 = kabutanタグ保持銘柄 ∪ 手動pin銘柄
        cands = {c for c, ts in tag_map.items() if tname in ts}
        cands |= {c for (t, c), (lk, _av) in locks.items() if t == tid and lk.startswith("pin")}

        for code in cands:
            lock, ai = locks.get((tid, code), ("", None))
            text = biz.get(code, "") + " " + segs.get(code, "")
            score, ev = 0, []
            if tname in tag_map.get(code, ()):
                score += 30; ev.append("tag")
            hits = sum(text.count(k) for k in kws)
            if hits >= 2:
                score += 45; ev.append(f"本文×{hits}")
            elif hits == 1:
                score += 35; ev.append("本文×1")
            tc = tag_cnt.get(code, 0)
            if tc and tc <= 8:
                score += 15; ev.append("特化")
            elif tc and tc <= 15:
                score += 8
            elif tc > 40:
                score -= 15; ev.append("コングロ")
            # AI判定の反映（過去の判定を再利用）
            if ai == 1:
                score = max(score, TIER2_SCORE); ev.append("AI✓")
            elif ai == 0:
                score = min(score, TIER1_SCORE - 1); ev.append("AI✗")
            # 手動ロック（最優先）
            if lock == "exclude":
                tier = 0; ev.append("手動除外")
            elif lock.startswith("pin"):
                tier = int(lock[3]); score = max(score, {3: TIER3_SCORE, 2: TIER2_SCORE, 1: TIER1_SCORE}[tier])
                ev.append("手動")
            else:
                tier = 3 if score >= TIER3_SCORE else 2 if score >= TIER2_SCORE else 1 if score >= TIER1_SCORE else 0
            all_rows.append([tid, code, min(max(score, 0), 100), tier, "+".join(ev)[:160], lock, ai, now])

    # 全消し→入れ直しではなく upsert + 消えた行の削除（lock行は残す）
    bulk_upsert(cur, "theme_members",
                ["theme_id", "code", "relevance", "tier", "evidence", "manual_lock", "ai_verdict", "updated_at"],
                all_rows,
                update_cols=["relevance", "tier", "evidence", "updated_at"])
    cur.execute("DELETE FROM theme_members WHERE updated_at < %s AND manual_lock = ''", (now,))
    if verbose:
        t2 = sum(1 for r in all_rows if r[3] >= 2)
        print(f"  メンバー: {len(all_rows)}行スコア済 (tier2+={t2})")


# ═══════════════════════════════════════════════════════════
#  Gemini 曖昧帯判定
# ═══════════════════════════════════════════════════════════

def ai_review(cur, max_pairs: int = AI_MAX_PAIRS, verbose: bool = True) -> int:
    """周辺帯(tier1)で未判定のペアをGeminiでバッチ判定し、ai_verdictを保存する。"""
    if not GEMINI_API_KEY:
        print("  GEMINI_API_KEY未設定のためAI判定スキップ")
        return 0
    cur.execute("""
        SELECT tm.theme_id, t.name, tm.code, s.name, LEFT(CONCAT_WS('/', s.business_summary, s.business_description), 150)
        FROM theme_members tm
        JOIN themes t ON t.id = tm.theme_id AND t.status IN ('active','candidate')
        JOIN stocks s ON s.code = tm.code
        WHERE tm.tier = 1 AND tm.ai_verdict IS NULL AND tm.manual_lock = ''
          AND tm.relevance BETWEEN %s AND %s
        LIMIT %s
    """, (AI_BAND[0], AI_BAND[1], max_pairs))
    pairs = cur.fetchall()
    if not pairs:
        if verbose:
            print("  AI判定対象なし")
        return 0

    from google import genai
    client = genai.Client(api_key=GEMINI_API_KEY)
    now = datetime.now()
    done = 0
    # テーマごとにまとめて1プロンプト
    by_theme: dict[tuple, list] = defaultdict(list)
    for tid, tname, code, sname, biz in pairs:
        by_theme[(tid, tname)].append((code, sname, biz or ""))
    for (tid, tname), items in by_theme.items():
        lines = "\n".join(f"{c}: {n} — {b}" for c, n, b in items)
        prompt = (f"投資テーマ「{tname}」に事業として実質的に関連する銘柄を判定してください。\n"
                  f"銘柄リスト(コード: 社名 — 事業内容):\n{lines}\n\n"
                  f"出力は1行1銘柄で「コード,yes」または「コード,no」のみ。説明不要。")
        try:
            resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            verdicts = dict(re.findall(r"(\w{4,5})\s*,\s*(yes|no)", resp.text or "", re.I))
            ups = []
            for c, n, _b in items:
                v = verdicts.get(c)
                if v is not None:
                    ups.append((1 if v.lower() == "yes" else 0, now, tid, c))
            if ups:
                cur.executemany(
                    "UPDATE theme_members SET ai_verdict=%s, updated_at=%s WHERE theme_id=%s AND code=%s", ups)
                done += len(ups)
            time.sleep(1.2)
        except Exception as e:  # noqa: BLE001
            print(f"  [AI] {tname}: {str(e)[:60]}")
            time.sleep(3)
    if verbose:
        print(f"  AI判定: {done}ペア")
    return done


# ═══════════════════════════════════════════════════════════
#  公開ヘルパ（他モジュール用）
# ═══════════════════════════════════════════════════════════

def auto_activate(cur, verbose: bool = True) -> int:
    """品質ゲートを満たす candidate を active に昇格する。
    ゲート: 投資テーマフィルタ通過(build時に適用済) + tier>=2 の構成銘柄が MIN_MEMBERS 以上。
    ※ archived は昇格しない（手動で葬ったテーマを復活させない）。"""
    cur.execute("""
        UPDATE themes t SET t.status='active', t.updated_at=NOW()
        WHERE t.status='candidate'
          AND (SELECT COUNT(*) FROM theme_members tm
               WHERE tm.theme_id = t.id AND tm.tier >= 2) >= %s
    """, (MIN_MEMBERS,))
    n = cur.rowcount
    if verbose and n:
        print(f"  candidate→active 昇格: {n}テーマ")
    return n


def load_theme_groups(cur, min_tier: int = 2) -> tuple[dict, dict]:
    """money_flow等の集計用: groups[code]=[('theme', name),...], labels。activeテーマ×tier>=min_tier のみ。"""
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


def run_weekly(with_ai: bool = True) -> None:
    """週次更新: テーマ選定 → スコア再計算 → AI曖昧帯判定。"""
    conn = get_conn()
    cur = conn.cursor()
    ensure_tables(cur)
    build_themes(cur)
    conn.commit()
    migrate_curated(cur)
    conn.commit()
    score_members(cur)
    conn.commit()
    if with_ai:
        ai_review(cur)
        conn.commit()
        score_members(cur, verbose=False)   # AI結果をtierに反映
        conn.commit()
    auto_activate(cur)
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    with_ai = "--ai" in sys.argv
    if "--score" in sys.argv:
        conn = get_conn(); cur = conn.cursor()
        ensure_tables(cur); score_members(cur); conn.commit()
        cur.close(); conn.close()
    else:
        run_weekly(with_ai=with_ai)
