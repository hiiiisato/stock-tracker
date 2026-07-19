"""
株価変動イベントの「理由」を機械分類する（price_events.reason_category）。

自由文の要約だけでは集計・持続日数分析・戦略検証ができないため、理由を
機械判定可能なタクソノミーに分類する。分類は信頼度の高い順に確定する:

  ① 開示由来（最も確実・コストゼロ）: 当日/前営業日引け後の適時開示(disclosures)と
     業績修正(forecast_revisions)を照合。TOB・自社株買い・決算・上方修正等を機械確定。
  ② テーマ物色・地合い連動・前日の継続（機械判定）: 所属テーマの当日変動(theme_daily_stats)、
     TOPIX大幅変動、直近の同一銘柄イベント(price_events)から判定。
  ③ 残りは None を返し、Gemini（event_researcher）が構造化出力でカテゴリを補完する。

機械で確定したカテゴリは Gemini の推測より優先する（誤りゼロ・一次データ由来）。
"""

from datetime import date, timedelta

# カテゴリ → (絵文字, 表示名)。表示・集計の単一の真実
REASON_CATEGORIES: dict[str, tuple[str, str]] = {
    "earnings_beat":  ("🎯", "決算(好感)"),
    "earnings_miss":  ("📉", "決算(失望)"),
    "guidance_up":    ("📈", "上方修正"),
    "guidance_down":  ("⚠️", "下方修正"),
    "buyback":        ("💰", "自社株買い"),
    "dividend":       ("💴", "増配・配当"),
    "tob_ma":         ("🤝", "TOB・M&A"),
    "alliance_order": ("🔗", "提携・受注"),
    "rating":         ("🏅", "レーティング"),
    "theme":          ("🌊", "テーマ物色"),
    "supply_demand":  ("📊", "需給・仕手"),
    "market":         ("🌐", "地合い連動"),
    "continuation":   ("➡️", "前日からの継続"),
    "unknown":        ("❓", "材料不明"),
}


def label_of(category: str | None) -> str:
    """'🎯 決算(好感)' のようなバッジ文字列。未分類は空。"""
    if not category or category not in REASON_CATEGORIES:
        return ""
    emo, name = REASON_CATEGORIES[category]
    return f"{emo} {name}"


# 開示カテゴリ → 理由カテゴリ（方向依存のものは _map_disclosure で処理）
_DISC_PRIORITY = ["tob", "buyback", "earnings_up", "earnings_rev", "guidance",
                  "div_up", "dividend_rev", "earnings_report", "alliance", "order"]


def _load_disclosures(cur, codes: list[str], ranking_date: date) -> dict:
    """反応窓（前3日〜当日）の開示を銘柄別に返す。引け後開示→翌日反応をカバー。"""
    if not codes:
        return {}
    ph = ",".join(["%s"] * len(codes))
    cur.execute(f"""
        SELECT code, category, title, disclosed_at
        FROM disclosures
        WHERE code IN ({ph})
          AND DATE(disclosed_at) BETWEEN DATE_SUB(%s, INTERVAL 3 DAY) AND %s
          AND category NOT IN ('other', 'monthly')
        ORDER BY disclosed_at DESC
    """, (*codes, ranking_date, ranking_date))
    out: dict = {}
    for code, cat, title, dt in cur.fetchall():
        out.setdefault(code, []).append((cat, title, dt))
    return out


def _load_revisions(cur, codes: list[str], ranking_date: date) -> dict:
    """当日反応の業績修正の方向（1=上方/-1=下方）を銘柄別に返す。"""
    if not codes:
        return {}
    ph = ",".join(["%s"] * len(codes))
    cur.execute(f"""
        SELECT code, direction, op_chg_pct FROM forecast_revisions
        WHERE code IN ({ph})
          AND (reaction_date = %s OR DATE(announced_at) BETWEEN DATE_SUB(%s, INTERVAL 3 DAY) AND %s)
        ORDER BY announced_at DESC
    """, (*codes, ranking_date, ranking_date, ranking_date))
    out: dict = {}
    for code, d, op in cur.fetchall():
        if code not in out:
            out[code] = (d, op)
    return out


def _load_continuation(cur, codes: list[str], ranking_date: date) -> dict:
    """直近3営業日に同方向で動いたイベントがある銘柄（前日からの継続の判定用）。"""
    if not codes:
        return {}
    ph = ",".join(["%s"] * len(codes))
    cur.execute(f"""
        SELECT code, direction FROM price_events
        WHERE code IN ({ph}) AND period='daily'
          AND event_date BETWEEN DATE_SUB(%s, INTERVAL 5 DAY) AND DATE_SUB(%s, INTERVAL 1 DAY)
        ORDER BY event_date DESC
    """, (*codes, ranking_date, ranking_date))
    out: dict = {}
    for code, d in cur.fetchall():
        if code not in out:
            out[code] = d
    return out


def _topix_change(cur, ranking_date: date) -> float | None:
    cur.execute("SELECT change_pct FROM daily_prices WHERE code='1306' AND date=%s", (ranking_date,))
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _load_theme_moves(cur, codes: list[str], ranking_date: date) -> dict:
    """銘柄が所属するテーマ（統一マスタ・tier>=2）のうち当日変動が最も大きいものを返す
    {code: (theme_name, avg_change_pct, breadth_ratio)}。stock_count>=5 のテーマのみ。"""
    if not codes:
        return {}
    ph = ",".join(["%s"] * len(codes))
    cur.execute(f"""
        SELECT tm.code, t.name, tds.avg_change_pct, tds.breadth_ratio
        FROM theme_members tm
        JOIN themes t ON t.id = tm.theme_id AND t.status = 'active'
        JOIN theme_daily_stats tds ON tds.theme_id = tm.theme_id AND tds.date = %s
        WHERE tm.code IN ({ph}) AND tm.tier >= 2 AND tds.stock_count >= 5
    """, (ranking_date, *codes))
    out: dict = {}
    for code, name, avg, breadth in cur.fetchall():
        avg = float(avg or 0)
        if code not in out or abs(avg) > abs(out[code][1]):
            out[code] = (name, avg, float(breadth or 0))
    return out


def _map_disclosure(cat: str, direction: str, rev: tuple | None) -> str | None:
    """開示カテゴリ＋方向＋業績修正の方向 → 理由カテゴリ。"""
    if cat == "tob":
        return "tob_ma"
    if cat == "buyback":
        return "buyback"
    if cat in ("div_up", "dividend_rev"):
        return "dividend"
    if cat in ("earnings_up",):
        return "guidance_up"
    if cat in ("earnings_rev", "guidance"):
        # 業績修正の方向を優先、無ければ株価方向
        if rev is not None and rev[0] in (1, -1):
            return "guidance_up" if rev[0] == 1 else "guidance_down"
        return "guidance_up" if direction == "up" else "guidance_down"
    if cat == "earnings_report":
        return "earnings_beat" if direction == "up" else "earnings_miss"
    if cat in ("alliance", "order"):
        return "alliance_order"
    return None


def classify_batch(cur, ranking_date: date, targets: list[tuple]) -> dict:
    """targets: [(code, direction, pct), ...] を機械分類する。
    返り値 {code: {"category": str|None, "confidence": "high"|"med"|None, "evidence": str}}。
    category=None は Gemini が補完する対象。"""
    codes = [t[0] for t in targets]
    disc = _load_disclosures(cur, codes, ranking_date)
    revs = _load_revisions(cur, codes, ranking_date)
    cont = _load_continuation(cur, codes, ranking_date)
    topix = _topix_change(cur, ranking_date)
    themes = _load_theme_moves(cur, codes, ranking_date)

    result: dict = {}
    for code, direction, pct in targets:
        cat, conf, ev = None, None, ""

        # ① 開示由来（高信頼）: 優先度順に最初にマッチした開示で確定
        items = disc.get(code) or []
        by_cat = {}
        for c, title, dt in items:
            by_cat.setdefault(c, (title, dt))
        for dc in _DISC_PRIORITY:
            if dc in by_cat:
                mapped = _map_disclosure(dc, direction, revs.get(code))
                if mapped:
                    cat, conf, ev = mapped, "high", by_cat[dc][0][:80]
                    break

        # ② 業績修正のみ（開示テーブルに拾われていないが修正はある）
        if cat is None and code in revs and revs[code][0] in (1, -1):
            d, op = revs[code]
            cat, conf = ("guidance_up" if d == 1 else "guidance_down"), "high"
            ev = f"業績修正（営業益{f'{float(op):+.0f}%' if op is not None else ''}）"

        # ③ 前日からの継続（新規開示が無く、直近同方向に動いていた）
        if cat is None and cont.get(code) == direction:
            cat, conf, ev = "continuation", "med", "直近営業日から同方向の動きが継続"

        # ④ テーマ物色（所属テーマが当日同方向に強く動き、広がりもある）
        if cat is None and code in themes:
            tname, avg, breadth = themes[code]
            same = (avg > 0) == (direction == "up")
            if same and abs(avg) >= 1.0 and (breadth >= 0.6 if direction == "up" else breadth <= 0.4):
                cat, conf, ev = "theme", "med", f"テーマ「{tname}」が当日{avg:+.1f}%で連動"

        # ⑤ 地合い連動（TOPIXが同方向に大きく動き、個別の変動が過大でない）
        if cat is None and topix is not None and abs(topix) >= 1.5:
            same = (topix > 0) == (direction == "up")
            if same and abs(pct) < 8.0:
                cat, conf, ev = "market", "med", f"TOPIXが当日{topix:+.1f}%（地合い連動）"

        result[code] = {"category": cat, "confidence": conf, "evidence": ev}
    return result
