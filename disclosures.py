"""
適時開示（TDnet）の蓄積・分析システム
=====================================
TDnetの全開示を日次で取得・分類して蓄積し、個人投資家向けの示唆を生成する。

機能:
  1. 蓄積     : 全開示を disclosures テーブルに保存（TDnetの保持期間は約1ヶ月のため、
                蓄積しないと過去に遡れない）。タイトルからカテゴリ・ポジネガを自動分類。
  2. 好材料抽出: 上方修正・増配・自社株買い等のPDF本文をGeminiで読み、
                「修正理由の要約」と「波及しそうな関連テーマ」を抽出。
  3. 市況考察  : 業種別・テーマ別騰落と開示動向を集計し、Geminiが日次コメントを生成。

実行:
  python3 disclosures.py                # 当日+前日を取得・分類・AI付加・市況考察
  python3 disclosures.py --backfill 30  # 過去30日分を蓄積（初回・TDnet保持期間内のみ）
  python3 disclosures.py --no-ai        # 取得・分類のみ（AI処理スキップ）
"""

from __future__ import annotations
import io
import re
import sys
import json
import time
from datetime import date, datetime, timedelta

import requests

from config import get_conn, bulk_upsert, GEMINI_API_KEY

# Gemini無料枠はモデルごとに独立したRPD枠（flash系は20/日と少ない）のため、
# イベント調査(gemini-2.5-flash)とは別のモデルを使って枠を分離する。
GEMINI_MODEL = "gemini-2.5-flash-lite"
PDF_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# AI付加の対象カテゴリ（好材料・方向不明の業績/配当修正）
ENRICH_CATEGORIES = ("earnings_up", "earnings_rev", "div_up", "dividend_rev", "tob", "buyback_large")
ENRICH_MAX_PER_DAY = 24   # Gemini無料枠を守る上限
ENRICH_BATCH_SIZE  = 8    # 1コールにまとめる開示数（無料枠RPDの節約）

# ─────────────────────────────────────────────────────────────────────────────
# タイトル分類（ルールベース・APIコストゼロ）
# ─────────────────────────────────────────────────────────────────────────────
# (category, キーワード群, sentiment) を上から順に評価し最初に一致したものを採用。
# sentiment: +1=好材料 / -1=悪材料 / 0=中立・要判定
_CATEGORY_RULES = [
    ("earnings_up",   ("上方修正",), +1),
    ("earnings_down", ("下方修正",), -1),
    ("div_up",        ("増配", "復配", "記念配当", "特別配当"), +1),
    ("div_down",      ("減配", "無配転落"), -1),
    ("earnings_rev",  ("業績予想の修正", "業績予想及び", "業績予想値との差異", "通期業績予想"), 0),
    ("dividend_rev",  ("配当予想の修正", "配当予想に関する"), 0),
    ("buyback",       ("自己株式の取得", "自己株式立会外買付", "自社株買い"), +1),
    ("split",         ("株式分割",), +1),
    ("tob",           ("公開買付", "ＭＢＯ", "MBO", "完全子会社化"), +1),
    ("alliance",      ("資本業務提携", "業務提携", "資本提携", "子会社化", "合併", "買収", "株式取得"), +1),
    ("order",         ("受注", "契約締結", "採用決定", "販売開始", "承認取得", "特許取得", "共同開発"), +1),
    ("monthly",       ("月次",), 0),
    ("earnings_report", ("決算短信", "決算補足", "決算説明"), 0),
    ("guidance",      ("業績予想に関するお知らせ", "業績見通し"), 0),
]

CATEGORY_LABELS = {
    "earnings_up":   "上方修正",
    "earnings_down": "下方修正",
    "earnings_rev":  "業績修正",
    "div_up":        "増配",
    "div_down":      "減配",
    "dividend_rev":  "配当修正",
    "buyback":       "自社株買い",
    "split":         "株式分割",
    "tob":           "TOB/MBO",
    "alliance":      "提携/M&A",
    "order":         "受注/新製品",
    "monthly":       "月次",
    "earnings_report": "決算",
    "guidance":      "業績見通し",
    "other":         "その他",
}


def classify_title(title: str) -> tuple[str, int]:
    """開示タイトルから (category, sentiment) を判定する。"""
    for cat, keywords, senti in _CATEGORY_RULES:
        if any(kw in title for kw in keywords):
            # 複合タイトルの方向補正（「業績予想及び配当予想の修正（増配）」等）
            if senti == 0:
                if any(k in title for k in ("上方", "増配", "増額")):
                    senti = +1
                elif any(k in title for k in ("下方", "減配", "減額", "無配")):
                    senti = -1
            return cat, senti
    return "other", 0


# ─────────────────────────────────────────────────────────────────────────────
# テーブル
# ─────────────────────────────────────────────────────────────────────────────

def ensure_tables():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS disclosures (
            id           BIGINT AUTO_INCREMENT PRIMARY KEY,
            code         VARCHAR(10) NOT NULL,
            disclosed_at DATETIME NOT NULL,
            title        VARCHAR(300) NOT NULL,
            pdf_url      VARCHAR(255),
            category     VARCHAR(30),
            sentiment    TINYINT DEFAULT 0,
            ai_summary   TEXT,
            ai_related   TEXT,
            created_at   DATETIME DEFAULT NOW(),
            UNIQUE KEY uq_disc (code, disclosed_at, title(100)),
            KEY idx_disc_date (disclosed_at),
            KEY idx_disc_cat (category)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_summary (
            summary_date     DATE PRIMARY KEY,
            sector_stats     TEXT,
            theme_stats      TEXT,
            disclosure_stats TEXT,
            ai_commentary    TEXT,
            created_at       DATETIME DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close(); conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 取得・蓄積
# ─────────────────────────────────────────────────────────────────────────────

def fetch_and_store_day(d: date) -> int:
    """1日分のTDnet開示を取得・分類して disclosures にupsertする。"""
    from research_strategy import _fetch_tdnet_day
    by_code = _fetch_tdnet_day(d)
    rows = []
    for code4, items in by_code.items():
        for it in items:
            cat, senti = classify_title(it["title"])
            rows.append((code4, it["dt"], it["title"][:300], it["url"][:255], cat, senti))
    if not rows:
        return 0
    conn = get_conn(); cur = conn.cursor()
    bulk_upsert(cur, "disclosures",
                ["code", "disclosed_at", "title", "pdf_url", "category", "sentiment"],
                rows, update_cols=["pdf_url", "category", "sentiment"])
    conn.commit()
    cur.close(); conn.close()
    return len(rows)


def backfill(days: int = 30) -> int:
    """TDnet保持期間内（約1ヶ月）の過去分を蓄積する。初回のみ実行。"""
    ensure_tables()
    total = 0
    today = date.today()
    for i in range(days, -1, -1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        n = fetch_and_store_day(d)
        total += n
        print(f"  {d}: {n}件", flush=True)
        time.sleep(1)
    return total


# ─────────────────────────────────────────────────────────────────────────────
# 好材料のAI付加（PDF本文 → 修正理由の要約 + 関連テーマ）
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_pdf_text(pdf_url: str, max_pages: int = 2, max_chars: int = 2500) -> str:
    try:
        import pdfplumber
        r = requests.get(pdf_url, headers=PDF_HEADERS, timeout=20)
        if r.status_code != 200 or r.content[:4] != b"%PDF":
            return ""
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            n = min(max_pages, len(pdf.pages))
            text = "\n".join(pdf.pages[i].extract_text() or "" for i in range(n))
        return text[:max_chars]
    except Exception:
        return ""


def _get_theme_names() -> list[str]:
    """Geminiの関連テーマ抽出用の語彙。統一テーママスタ(みんかぶ)のactiveテーマ名。
    プロンプト肥大を避けるため構成銘柄数(tier>=2)が多い順に300テーマまで。"""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT t.name FROM themes t
        JOIN theme_members tm ON tm.theme_id = t.id AND tm.tier >= 2
        WHERE t.status = 'active'
        GROUP BY t.id, t.name
        ORDER BY COUNT(*) DESC
        LIMIT 300
    """)
    names = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    return names


def _gemini_client():
    from google import genai
    if not GEMINI_API_KEY:
        return None
    return genai.Client(api_key=GEMINI_API_KEY)


def enrich_highlights(days_back: int = 2, max_items: int = ENRICH_MAX_PER_DAY) -> int:
    """好材料開示のPDFを読み、修正理由の要約と関連テーマをGeminiで付加する。

    出力（ai_related, JSON）:
      {"direction": "上方|下方|中立", "themes": ["半導体", ...], "ripple": "波及の考え方"}
    """
    conn = get_conn(); cur = conn.cursor()
    ph = ",".join(["%s"] * len(ENRICH_CATEGORIES))
    cur.execute(f"""
        SELECT d.id, d.code, s.name, d.title, d.pdf_url, d.category
        FROM disclosures d
        LEFT JOIN stocks s ON d.code = s.code
        LEFT JOIN stock_fundamentals f ON d.code = f.code
        WHERE d.category IN ({ph})
          AND d.disclosed_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
          AND d.ai_summary IS NULL
          AND d.pdf_url IS NOT NULL AND d.pdf_url != ''
        ORDER BY COALESCE(f.market_cap, 0) DESC
        LIMIT %s
    """, (*ENRICH_CATEGORIES, days_back, max_items))
    targets = cur.fetchall()
    cur.close(); conn.close()

    if not targets:
        print("  [enrich] 対象なし")
        return 0

    client = _gemini_client()
    if client is None:
        print("  [enrich] GEMINI_API_KEY未設定のためスキップ")
        return 0

    theme_names = _get_theme_names()
    print(f"  [enrich] {len(targets)}件のPDFを解析...")

    # PDF本文取得
    docs = []
    for did, code, name, title, pdf_url, cat in targets:
        text = _fetch_pdf_text(pdf_url)
        time.sleep(0.5)
        docs.append({"id": did, "code": code, "name": name or code,
                     "title": title, "category": cat, "text": text})

    done = 0
    for i in range(0, len(docs), ENRICH_BATCH_SIZE):
        batch = docs[i:i + ENRICH_BATCH_SIZE]
        sections = []
        for d in batch:
            body = d["text"] if d["text"] else "（PDF本文を取得できず。タイトルのみで判断）"
            sections.append(f"▼ DISC:{d['id']} {d['name']}（{d['code']}）\n"
                            f"  タイトル: {d['title']}\n  本文抜粋:\n{body}")

        prompt = f"""あなたは株式アナリストです。以下の適時開示{len(batch)}件それぞれについて、
個人投資家向けに内容を分析してJSONで出力してください。

{chr(10).join(sections)}

## 出力ルール
- 各開示につき1行のJSONを出力。先頭に「DISC:番号」を付けること。
- summary: 開示の要点を具体的な数値込みで2文以内に要約（例: 営業利益予想を50億円→70億円に40%上方修正。半導体製造装置向け部品の受注増が要因）
- direction: 業績・配当への影響が「上方」「下方」「中立」のどれか
- themes: この開示の背景にある事業テーマを以下のリストから0〜3個選ぶ（リストにないものは選ばない）:
  {"、".join(theme_names)}
- ripple: この開示理由が他のどんな銘柄群に波及しうるかの考え方を1文（例: データセンター向け電力機器の需要増が理由のため、電線・冷却・電源装置関連にも追い風）。波及が考えにくければ空文字列。
- 数値や固有名詞を創作しないこと。

## 出力フォーマット
DISC:番号 {{"summary": "...", "direction": "上方", "themes": ["半導体"], "ripple": "..."}}
"""
        raw = ""
        for attempt in range(2):
            try:
                time.sleep(8)
                resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
                raw = resp.text or ""
                break
            except Exception as e:
                if ("429" in str(e) or "503" in str(e)) and attempt == 0:
                    print("    [enrich] レート制限/過負荷 → 65秒待機してリトライ")
                    time.sleep(65)
                else:
                    print(f"    [enrich] Geminiエラー: {str(e)[:120]}")
                    break
        if not raw:
            continue

        conn = get_conn(); cur = conn.cursor()
        for m in re.finditer(r"DISC:(\d+)\s*(\{[^\n]*\})", raw):
            did = int(m.group(1))
            try:
                data = json.loads(m.group(2))
            except Exception:
                continue
            summary = (data.get("summary") or "")[:1000]
            related = json.dumps({
                "direction": data.get("direction", "中立"),
                "themes": data.get("themes", [])[:3],
                "ripple": (data.get("ripple") or "")[:300],
            }, ensure_ascii=False)
            cur.execute("UPDATE disclosures SET ai_summary=%s, ai_related=%s WHERE id=%s",
                        (summary, related, did))
            done += 1
        conn.commit()
        cur.close(); conn.close()
        print(f"    [enrich] バッチ {i//ENRICH_BATCH_SIZE + 1}: 累計{done}件付加")

    return done


# ─────────────────────────────────────────────────────────────────────────────
# 市況考察（業種・テーマ・開示動向の日次サマリー）
# ─────────────────────────────────────────────────────────────────────────────

def build_market_summary(target_date: date = None) -> bool:
    """業種別・テーマ別騰落と開示動向を集計し、Geminiで市況コメントを生成して保存する。"""
    conn = get_conn(); cur = conn.cursor()
    if target_date is None:
        cur.execute("SELECT MAX(date) FROM daily_prices WHERE close IS NOT NULL")
        target_date = cur.fetchone()[0]
    if not target_date:
        cur.close(); conn.close()
        return False

    # 業種別騰落（東証33業種）
    cur.execute("""
        SELECT sec.name, ROUND(AVG(dp.change_pct), 2) AS avg_chg, COUNT(*) AS n,
               SUM(CASE WHEN dp.change_pct > 0 THEN 1 ELSE 0 END) AS n_up
        FROM daily_prices dp
        JOIN stocks s   ON dp.code = s.code
        JOIN sectors sec ON s.sector_id = sec.id
        WHERE dp.date = %s AND dp.change_pct IS NOT NULL AND s.is_active = TRUE
        GROUP BY sec.name HAVING COUNT(*) >= 5
        ORDER BY avg_chg DESC
    """, (target_date,))
    sector_rows = [{"sector": r[0], "avg_chg": float(r[1]), "n": int(r[2]), "n_up": int(r[3])}
                   for r in cur.fetchall()]

    # テーマ別騰落（theme_daily_stats・統一テーママスタ。ノイズ回避に5銘柄以上のみ）
    cur.execute("""
        SELECT t.name, tds.avg_change_pct, tds.heat_score, tds.breadth_ratio
        FROM theme_daily_stats tds
        JOIN themes t ON tds.theme_id = t.id
        WHERE tds.date = %s AND t.status = 'active' AND tds.stock_count >= 5
        ORDER BY tds.avg_change_pct DESC
    """, (target_date,))
    theme_rows = [{"theme": r[0], "avg_chg": float(r[1] or 0), "heat": float(r[2] or 0),
                   "breadth": float(r[3] or 0)} for r in cur.fetchall()]

    # 当日の開示動向
    cur.execute("""
        SELECT category, COUNT(*) FROM disclosures
        WHERE DATE(disclosed_at) = %s GROUP BY category
    """, (target_date,))
    disc_counts = {r[0]: int(r[1]) for r in cur.fetchall()}
    cur.execute("""
        SELECT d.code, s.name, d.category, d.title
        FROM disclosures d LEFT JOIN stocks s ON d.code = s.code
        LEFT JOIN stock_fundamentals f ON d.code = f.code
        WHERE DATE(d.disclosed_at) = %s AND d.sentiment = 1
          AND d.category IN ('earnings_up','div_up','buyback','tob')
        ORDER BY COALESCE(f.market_cap,0) DESC LIMIT 15
    """, (target_date,))
    notable = [{"code": r[0], "name": r[1] or r[0], "category": r[2], "title": r[3][:60]}
               for r in cur.fetchall()]
    cur.close(); conn.close()

    # Gemini市況コメント
    commentary = None
    client = _gemini_client()
    if client:
        top_sec = sector_rows[:5]
        bot_sec = sector_rows[-5:] if len(sector_rows) > 5 else []
        top_th  = theme_rows[:6]
        cat_line = "、".join(f"{CATEGORY_LABELS.get(k,k)}{v}件" for k, v in sorted(
            disc_counts.items(), key=lambda x: -x[1]) if k != "other")[:200]
        notable_line = "\n".join(f"  {n['name']}({n['code']}): [{CATEGORY_LABELS.get(n['category'])}] {n['title']}"
                                 for n in notable[:10])
        prompt = f"""あなたは株式市場の専門アナリストです。{target_date}の東京株式市場について、
以下の実データだけを根拠に、個人投資家向けの市況考察を書いてください。

【業種別騰落率 上位】
{chr(10).join(f"  {s['sector']}: {s['avg_chg']:+.2f}%（{s['n_up']}/{s['n']}銘柄が上昇）" for s in top_sec)}
【業種別騰落率 下位】
{chr(10).join(f"  {s['sector']}: {s['avg_chg']:+.2f}%（{s['n_up']}/{s['n']}銘柄が上昇）" for s in bot_sec)}
【テーマ別騰落率 上位】
{chr(10).join(f"  {t['theme']}: {t['avg_chg']:+.2f}%（上昇銘柄比率{t['breadth']:.0%}）" for t in top_th)}
【本日の適時開示】{cat_line}
【主な好材料開示（時価総額順）】
{notable_line or '  （なし）'}

## ルール
- 400字程度。見出しや箇条書きは使わず、段落2つ（①資金の流れ: どの業種・テーマに資金が向かい、どこから抜けているか ②開示動向: 上方修正・増配等の傾向と注目点）
- 提供データにない数値・出来事を創作しない。指数の水準や海外市況には言及しない（データがないため）
- 「〜とみられる」等の推測表現は、データから直接読み取れない解釈にのみ使う
"""
        for attempt in range(2):
            try:
                resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
                commentary = (resp.text or "").strip()[:2000]
                break
            except Exception as e:
                if ("429" in str(e) or "503" in str(e)) and attempt == 0:
                    print("  [市況] レート制限/過負荷 → 65秒待機してリトライ")
                    time.sleep(65)
                else:
                    print(f"  [市況] Geminiエラー: {str(e)[:120]}")
                    break

    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO market_summary (summary_date, sector_stats, theme_stats, disclosure_stats, ai_commentary)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          sector_stats=VALUES(sector_stats), theme_stats=VALUES(theme_stats),
          disclosure_stats=VALUES(disclosure_stats),
          ai_commentary=COALESCE(VALUES(ai_commentary), ai_commentary),
          created_at=NOW()
    """, (target_date,
          json.dumps(sector_rows, ensure_ascii=False),
          json.dumps(theme_rows, ensure_ascii=False),
          json.dumps({"counts": disc_counts, "notable": notable}, ensure_ascii=False),
          commentary))
    conn.commit()
    cur.close(); conn.close()
    print(f"  [市況] {target_date} 保存完了（コメント{'あり' if commentary else 'なし'}）")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────────────────────────────────────

def backfill_missing_commentaries(days_back: int = 5) -> int:
    """直近N日で ai_commentary が欠損している市況考察を再生成する（自己修復）。
    Gemini枠切れ・APIキー未設定の実行環境で生成に失敗した日を翌日以降に埋める。"""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT summary_date FROM market_summary
        WHERE ai_commentary IS NULL
          AND summary_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
        ORDER BY summary_date
    """, (days_back,))
    missing = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    fixed = 0
    for d in missing:
        print(f"  [市況] {d} のコメント欠損を再生成...")
        if build_market_summary(d):
            fixed += 1
    return fixed


def run_daily(with_ai: bool = True) -> dict:
    """日次実行: 当日+前日の開示を蓄積 → 好材料AI付加 → 市況考察（欠損自己修復つき）。"""
    ensure_tables()
    today = date.today()
    total = 0
    for d in (today - timedelta(days=1), today):
        if d.weekday() >= 5:
            continue
        n = fetch_and_store_day(d)
        total += n
        print(f"  [開示] {d}: {n}件")
    enriched = 0
    if with_ai:
        enriched = enrich_highlights(days_back=4)
        build_market_summary()
        backfill_missing_commentaries()
    return {"stored": total, "enriched": enriched}


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--backfill" in args:
        idx = args.index("--backfill")
        days = int(args[idx + 1]) if idx + 1 < len(args) else 30
        print(f"=== 過去{days}日分をバックフィル ===")
        n = backfill(days)
        print(f"完了: {n}件")
    else:
        with_ai = "--no-ai" not in args
        result = run_daily(with_ai=with_ai)
        print("完了:", result)
