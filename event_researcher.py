"""
銘柄別ニュース収集・イベント記録モジュール
調査手法の詳細は research_strategy.py を参照・編集すること。

処理フロー（3フェーズ）:
  Phase 1: 全対象銘柄のニュースを並行取得（ThreadPoolExecutor）
  Phase 2: 全銘柄まとめて Gemini でバッチ要約（API 呼び出し回数を大幅削減）
  Phase 3: 全結果を DB に一括保存

使い方:
  python event_researcher.py              # ±10%超えの銘柄を調査
  python event_researcher.py 7203 6857   # 指定銘柄を調査
"""
import sys
from datetime import date, timedelta
from config import get_conn
from research_strategy import (
    fetch_news,
    fetch_news_batch,
    summarize_news_batch,
    get_strategy_description,
    RESEARCH_THRESHOLD_PCT,
    RESEARCH_MAX_PER_DIRECTION,
)


def _format_news_text(news_items: list) -> str:
    """DB保存用のニューステキスト。URLがある場合は ` | url` を末尾に付与
    （表示側 _render_news_items がリンク化する）。"""
    lines = []
    for it in news_items:
        dt_str = it["dt"].strftime("%m/%d %H:%M")
        line = f"[{dt_str}][{it['category']}] {it['title']}"
        if it.get("url"):
            line += f" | {it['url']}"
        lines.append(line)
    return "\n".join(lines)


def _ensure_ai_summary_column():
    conn = get_conn()
    cur = conn.cursor()
    for coldef in ("ai_summary TEXT",
                   "reason_category VARCHAR(20)",   # 機械分類の理由カテゴリ（event_classifier）
                   "reason_confidence VARCHAR(4)"):  # high(開示由来) / med(テーマ/地合い/継続・Gemini)
        try:
            cur.execute(f"ALTER TABLE price_events ADD COLUMN {coldef}")
            conn.commit()
        except Exception:
            pass
    cur.close()
    conn.close()


_ai_column_checked = False


def _get_company_name(code: str) -> str:
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT name FROM stocks WHERE code = %s", (code,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            return row[0][:10]
    except Exception:
        pass
    return ""


def _get_all_company_names(codes: list) -> dict:
    """複数銘柄の名前を一括取得（N+1クエリ排除）。"""
    if not codes:
        return {}
    try:
        conn = get_conn()
        cur  = conn.cursor()
        placeholders = ",".join(["%s"] * len(codes))
        cur.execute(f"SELECT code, name FROM stocks WHERE code IN ({placeholders})", codes)
        result = {str(row[0]): (row[1] or "")[:10] for row in cur.fetchall()}
        cur.close()
        conn.close()
        return result
    except Exception:
        return {}


def _get_financials_context(codes: list) -> dict:
    """
    最新2期分の業績データを一括取得し、Gemini プロンプト用テキストに整形する。
    returns: {code: "  決算期: ...\n  売上高: ...\n...", ...}
    """
    if not codes:
        return {}
    try:
        conn = get_conn()
        cur  = conn.cursor()
        placeholders = ",".join(["%s"] * len(codes))
        cur.execute(f"""
            SELECT code, period_end, revenue, operating_income, ordinary_income, net_income
            FROM (
                SELECT code, period_end, revenue, operating_income, ordinary_income, net_income,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY period_end DESC) AS rn
                FROM financials
                WHERE code IN ({placeholders}) AND period_type = 'A'
            ) t
            WHERE rn <= 2
            ORDER BY code, period_end DESC
        """, codes)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception:
        return {}

    # code → [(period_end, rev, op, ord_, net), ...] に集約
    by_code: dict = {}
    for row in rows:
        by_code.setdefault(str(row[0]), []).append(row[1:])

    def _oku(v):
        return f"{float(v)/1e8:.0f}億円" if v is not None else "不明"

    def _diff(cur_v, prev_v):
        if cur_v is None or prev_v is None or float(prev_v) == 0:
            return ""
        return f"（前期比{(float(cur_v)/float(prev_v)-1)*100:+.0f}%）"

    result = {}
    for code, periods in by_code.items():
        if len(periods) >= 2:
            c, p = periods[0], periods[1]   # c=当期, p=前期
            lines = [
                f"決算期: {str(p[0])[:7]} → {str(c[0])[:7]}",
                f"売上高: {_oku(p[1])} → {_oku(c[1])}{_diff(c[1], p[1])}",
                f"営業利益: {_oku(p[2])} → {_oku(c[2])}{_diff(c[2], p[2])}",
                f"経常利益: {_oku(p[3])} → {_oku(c[3])}{_diff(c[3], p[3])}",
                f"純利益: {_oku(p[4])} → {_oku(c[4])}{_diff(c[4], p[4])}",
            ]
        else:
            p = periods[0]
            lines = [
                f"決算期: {str(p[0])[:7]}",
                f"売上高: {_oku(p[1])}、営業利益: {_oku(p[2])}、純利益: {_oku(p[4])}",
            ]
        result[code] = "\n".join(f"  {l}" for l in lines)

    return result


def _get_market_caps(codes: list) -> dict:
    """時価総額を一括取得（円）。"""
    if not codes:
        return {}
    try:
        conn = get_conn(); cur = conn.cursor()
        ph = ",".join(["%s"] * len(codes))
        cur.execute(f"SELECT code, market_cap FROM stock_fundamentals WHERE code IN ({ph})", codes)
        result = {str(r[0]): float(r[1]) for r in cur.fetchall() if r[1]}
        cur.close(); conn.close()
        return result
    except Exception:
        return {}


def _get_themes_context(codes: list) -> dict:
    """所属テーマ名を一括取得。テーマ株物色の文脈判断用。"""
    if not codes:
        return {}
    try:
        conn = get_conn(); cur = conn.cursor()
        ph = ",".join(["%s"] * len(codes))
        # 統一テーママスタ(みんかぶ)のtier>=2。関連度の高い順に上位5テーマまで
        cur.execute(f"""
            SELECT tm.code, SUBSTRING_INDEX(
                GROUP_CONCAT(t.name ORDER BY tm.relevance DESC SEPARATOR '、'), '、', 5)
            FROM theme_members tm JOIN themes t ON t.id = tm.theme_id
            WHERE tm.code IN ({ph}) AND tm.tier >= 2 AND t.status = 'active'
            GROUP BY tm.code
        """, codes)
        result = {str(r[0]): r[1] for r in cur.fetchall() if r[1]}
        cur.close(); conn.close()
        return result
    except Exception:
        return {}


def _get_price_history_context(codes: list, ranking_date: date, days: int = 10) -> dict:
    """直近N営業日の日次騰落率系列を一括取得。連続S高・暴落後リバウンド等の文脈用。"""
    if not codes:
        return {}
    try:
        conn = get_conn(); cur = conn.cursor()
        ph = ",".join(["%s"] * len(codes))
        cur.execute(f"""
            SELECT code, date, change_pct FROM (
                SELECT code, date, change_pct,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                FROM daily_prices
                WHERE code IN ({ph}) AND date <= %s AND change_pct IS NOT NULL
            ) t WHERE rn <= %s ORDER BY code, date
        """, codes + [ranking_date, days])
        by_code: dict = {}
        for code, dt, chg in cur.fetchall():
            by_code.setdefault(str(code), []).append(f"{dt.strftime('%m/%d')} {float(chg):+.1f}%")
        cur.close(); conn.close()
        return {c: "、".join(v) for c, v in by_code.items()}
    except Exception:
        return {}


def _get_recent_events_context(codes: list, ranking_date: date, days: int = 21) -> dict:
    """同一銘柄の直近の変動イベント（過去の分析の【変動理由】1行目）を一括取得。"""
    if not codes:
        return {}
    try:
        conn = get_conn(); cur = conn.cursor()
        ph = ",".join(["%s"] * len(codes))
        cur.execute(f"""
            SELECT code, event_date, direction, change_pct, ai_summary
            FROM price_events
            WHERE code IN ({ph}) AND period = 'daily'
              AND event_date < %s AND event_date >= DATE_SUB(%s, INTERVAL {days} DAY)
            ORDER BY code, event_date
        """, codes + [ranking_date, ranking_date])
        by_code: dict = {}
        for code, dt, direction, pct, summary in cur.fetchall():
            reason = ""
            if summary:
                # 【変動理由】セクションの本文1行目だけを抜き出す
                import re as _re
                m = _re.search(r"【変動理由】\s*\n?(.+)", summary)
                if m:
                    reason = m.group(1).strip()[:80]
            sign = "+" if (pct or 0) > 0 else ""
            line = f"  {dt.strftime('%m/%d')} {sign}{float(pct or 0):.1f}%: {reason or '(要約なし)'}"
            by_code.setdefault(str(code), []).append(line)
        cur.close(); conn.close()
        return {c: "\n".join(v[-5:]) for c, v in by_code.items()}
    except Exception:
        return {}


def _save_event(code: str, event_date: date, direction: str, change_pct: float,
                ranking: int, period: str, news_text: str | None,
                ai_summary: str | None, reason_category: str | None = None,
                reason_confidence: str | None = None) -> bool:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO price_events
              (code, event_date, direction, change_pct, ranking, period,
               news_items, ai_summary, reason_category, reason_confidence, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
              direction  = VALUES(direction),
              change_pct = VALUES(change_pct),
              ranking    = VALUES(ranking),
              news_items = VALUES(news_items),
              ai_summary = VALUES(ai_summary),
              reason_category = VALUES(reason_category),
              reason_confidence = VALUES(reason_confidence),
              created_at = NOW()
        """, (code, event_date, direction, change_pct, ranking, period,
              news_text, ai_summary, reason_category, reason_confidence))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"  [event] 保存エラー {code}/{event_date}: {e}")
        return False


def research_and_save(code: str, event_date: date, direction: str,
                      change_pct: float, ranking: int = None,
                      period: str = "daily") -> bool:
    """
    1銘柄のニュースを収集して price_events に保存する（個別実行用）。
    まとめて処理する場合は research_top_movers() を使うこと。
    """
    global _ai_column_checked
    if not _ai_column_checked:
        _ensure_ai_summary_column()
        _ai_column_checked = True

    company_name = _get_company_name(code)
    window_days  = 7 if period == "weekly" else None
    news         = fetch_news(code, target_date=event_date, company_name=company_name,
                              direction=direction, window_days=window_days)
    news_text    = _format_news_text(news) if news else None

    result = summarize_news_batch([{
        "code": code, "name": company_name, "date": event_date,
        "direction": direction, "change_pct": change_pct,
        "news": news,
        "financials":    _get_financials_context([code]).get(code),
        "market_cap":    _get_market_caps([code]).get(code),
        "themes":        _get_themes_context([code]).get(code),
        "price_history": _get_price_history_context([code], event_date).get(code),
        "recent_events": _get_recent_events_context([code], event_date).get(code),
    }])
    res = result.get(code)
    ai_summary = res.get("summary") if isinstance(res, dict) else res
    gem_cat    = res.get("category") if isinstance(res, dict) else None

    # 機械分類（開示由来等）を優先し、無ければGeminiの分類
    import event_classifier as _ec
    conn = get_conn(); cur = conn.cursor()
    cls = _ec.classify_batch(cur, event_date, [(code, direction, change_pct)]).get(code, {})
    cur.close(); conn.close()
    category   = cls.get("category") or (gem_cat if gem_cat in _ec.REASON_CATEGORIES else None)
    confidence = cls.get("confidence") or ("med" if category else None)

    return _save_event(code, event_date, direction, change_pct, ranking, period,
                       news_text, ai_summary, category, confidence)


def _get_daily_movers(ranking_date: date, threshold: float, max_n: int) -> tuple:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT dp.code,
               ROW_NUMBER() OVER (ORDER BY dp.change_pct DESC) AS rk,
               dp.change_pct
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        LEFT JOIN markets m ON s.market_id = m.id
        WHERE dp.date = %s
          AND dp.change_pct >= %s
          AND s.is_active = TRUE
          AND (s.market_id IS NULL OR m.code IN ('0111','0112','0113'))
        ORDER BY dp.change_pct DESC
        LIMIT %s
    """, (ranking_date, threshold, max_n))
    gainers = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    cur.execute("""
        SELECT dp.code,
               ROW_NUMBER() OVER (ORDER BY dp.change_pct ASC) AS rk,
               dp.change_pct
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        LEFT JOIN markets m ON s.market_id = m.id
        WHERE dp.date = %s
          AND dp.change_pct <= %s
          AND s.is_active = TRUE
          AND (s.market_id IS NULL OR m.code IN ('0111','0112','0113'))
        ORDER BY dp.change_pct ASC
        LIMIT %s
    """, (ranking_date, -threshold, max_n))
    losers = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    cur.close()
    conn.close()
    return gainers, losers


def _get_weekly_movers(ranking_date: date, threshold: float, max_n: int) -> tuple:
    week_start = ranking_date - timedelta(days=6)
    conn = get_conn()
    cur = conn.cursor()

    for order, sign, min_chg in [("DESC", ">=", threshold), ("ASC", "<=", -threshold)]:
        cur.execute(f"""
            WITH week_prices AS (
                SELECT code,
                    -- 分割対応: 必ず調整済み株価で計算する（生closeだと権利落ちが暴落に見える）
                    FIRST_VALUE(COALESCE(adj_close, close)) OVER (PARTITION BY code ORDER BY date) AS first_close,
                    LAST_VALUE(COALESCE(adj_close, close))  OVER (
                        PARTITION BY code ORDER BY date
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    ) AS last_close
                FROM daily_prices
                WHERE date BETWEEN %s AND %s AND close > 0
            ),
            weekly AS (
                SELECT code, MAX(first_close) AS first_close, MAX(last_close) AS last_close
                FROM week_prices GROUP BY code HAVING MAX(first_close) > 0
            ),
            ranked AS (
                SELECT w.code,
                    ROUND((w.last_close - w.first_close) / w.first_close * 100, 4) AS chg
                FROM weekly w
                JOIN stocks s ON w.code = s.code
                LEFT JOIN markets m ON s.market_id = m.id
                WHERE s.is_active = TRUE
                  AND (s.market_id IS NULL OR m.code IN ('0111','0112','0113'))
            )
            SELECT code, ROW_NUMBER() OVER (ORDER BY chg {order}) AS rk, chg
            FROM ranked WHERE chg {sign} %s ORDER BY chg {order} LIMIT %s
        """, (week_start, ranking_date, min_chg, max_n))
        if order == "DESC":
            gainers = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]
        else:
            losers  = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    cur.close()
    conn.close()
    return gainers, losers


def research_top_movers(target_date: date = None, period: str = "daily") -> int:
    """
    ±RESEARCH_THRESHOLD_PCT% 超えの銘柄を3フェーズで調査・保存する。
    Phase 1: 並行ニュース取得 → Phase 2: バッチ AI 要約 → Phase 3: DB 保存
    """
    global _ai_column_checked
    if not _ai_column_checked:
        _ensure_ai_summary_column()
        _ai_column_checked = True

    if target_date is None:
        target_date = date.today()

    # 最新データ日付を取得
    conn = get_conn()
    cur = conn.cursor()
    if period == "daily":
        cur.execute("SELECT MAX(date) FROM daily_prices WHERE close IS NOT NULL")
    else:
        cur.execute("""
            SELECT MAX(period_end) FROM rankings
            WHERE period_type = 'weekly' AND rank_type = 'change_pct'
        """)
    row = cur.fetchone()
    ranking_date = row[0] if row and row[0] else target_date
    cur.close()
    conn.close()

    threshold = RESEARCH_THRESHOLD_PCT
    max_n     = RESEARCH_MAX_PER_DIRECTION

    if period == "daily":
        gainers, losers = _get_daily_movers(ranking_date, threshold, max_n)
    else:
        gainers, losers = _get_weekly_movers(ranking_date, threshold, max_n)

    all_targets = [(c, rk, pct, "up")   for c, rk, pct in gainers] + \
                  [(c, rk, pct, "down") for c, rk, pct in losers]

    print(f"\n  [{period}] {ranking_date} 対象: 上昇{len(gainers)}件 / 下落{len(losers)}件")
    print(f"  調査戦略: {get_strategy_description()}")

    if not all_targets:
        print(f"  調査対象なし（閾値 ±{threshold}%）")
        return 0

    # ── Phase 1: 全銘柄のニュース並行取得 + 文脈データ一括取得 ─────────
    codes = [c for c, _, _, _ in all_targets]
    names    = _get_all_company_names(codes)   # 1クエリで一括取得
    fin_ctx  = _get_financials_context(codes)  # 最新2期の業績
    mcaps    = _get_market_caps(codes)         # 時価総額
    themes   = _get_themes_context(codes)      # 所属テーマ
    hist_ctx = _get_price_history_context(codes, ranking_date)  # 直近の値動き
    ev_ctx   = _get_recent_events_context(codes, ranking_date)  # 過去の変動イベント

    # 週次調査はニュース取得窓を1週間に広げる
    window_days = 7 if period == "weekly" else None

    print(f"\n  [Phase 1] ニュース並行取得中 ({len(all_targets)}銘柄)...")
    stock_specs = [
        {"code": c, "name": names.get(c, ""), "date": ranking_date, "direction": d,
         "window_days": window_days}
        for c, _, _, d in all_targets
    ]
    news_by_code = fetch_news_batch(stock_specs)
    news_found   = sum(1 for n in news_by_code.values() if n)
    print(f"  [Phase 1] 完了: {news_found}/{len(all_targets)} 銘柄でニュースあり"
          f"（財務データあり: {len(fin_ctx)}銘柄）")

    # ── Phase 2: 全銘柄をまとめて AI 要約 ───────────────────────────────
    # ニュースが無い銘柄も対象にする（値動き履歴・過去イベント・テーマから
    # 文脈を推測できるため。方向と整合しない空要約を防ぐ）
    batch_data = [
        {
            "code": c, "name": names.get(c, ""), "date": ranking_date,
            "direction": d, "change_pct": pct,
            "news": news_by_code.get(c, []),
            "financials": fin_ctx.get(c),
            "market_cap": mcaps.get(c),
            "themes": themes.get(c),
            "price_history": hist_ctx.get(c),
            "recent_events": ev_ctx.get(c),
        }
        for c, _, pct, d in all_targets
    ]

    # ── Phase 1.5: 理由の機械分類（開示由来・継続・テーマ・地合い） ──────
    # 一次データ(disclosures/forecast_revisions/theme_daily_stats)で確定できる理由を
    # AIより先に機械判定する（誤りゼロ・コストゼロ）。Geminiは残り＋要約文を担当。
    import event_classifier as _ec
    conn2 = get_conn(); cur2 = conn2.cursor()
    cls = _ec.classify_batch(cur2, ranking_date, [(c, d, pct) for c, _, pct, d in all_targets])
    cur2.close(); conn2.close()
    n_machine = sum(1 for v in cls.values() if v["category"])
    print(f"  [Phase 1.5] 機械分類: {n_machine}/{len(all_targets)}件を一次データから確定")

    # ── Phase 2: 全銘柄をまとめて AI 要約（要約文＋未分類のカテゴリ補完） ──
    if batch_data:
        print(f"\n  [Phase 2] AI 要約バッチ処理 ({len(batch_data)}銘柄)...")
        summaries = summarize_news_batch(batch_data)
    else:
        summaries = {}
        print("  [Phase 2] 対象なし → AI 要約スキップ")

    # ── Phase 3: 全件 DB 保存（機械分類を優先、無ければGemini補完カテゴリ） ──
    print(f"\n  [Phase 3] DB 保存...")
    saved = 0
    for code, rank, pct, direction in all_targets:
        news      = news_by_code.get(code, [])
        news_text = _format_news_text(news) if news else None
        res       = summaries.get(code)
        ai_sum    = res.get("summary") if isinstance(res, dict) else res
        gem_cat   = res.get("category") if isinstance(res, dict) else None

        m = cls.get(code, {})
        category   = m.get("category") or (gem_cat if gem_cat in _ec.REASON_CATEGORIES else None)
        confidence = m.get("confidence") or ("med" if category else None)

        if _save_event(code, ranking_date, direction, pct, rank, period,
                       news_text, ai_sum, category, confidence):
            saved += 1

    n_cat = sum(1 for c, _, _, _ in all_targets
                if (cls.get(c, {}).get("category")
                    or (isinstance(summaries.get(c), dict) and summaries[c].get("category"))))
    print(f"  [Phase 3] 完了: {saved}/{len(all_targets)} 件保存（理由カテゴリ確定: {n_cat}件）")
    return saved


def get_events_for_date(event_date: date = None, period: str = "daily") -> dict:
    """指定日の全イベントを取得（events ページ用）。"""
    conn = get_conn()
    cur = conn.cursor()

    if event_date is None:
        cur.execute("SELECT MAX(event_date) FROM price_events WHERE period = %s", (period,))
        row = cur.fetchone()
        event_date = row[0] if row and row[0] else date.today()

    cur.execute("""
        SELECT pe.code, s.name, pe.direction, pe.change_pct,
               pe.ranking, pe.news_items, pe.ai_summary, f.market_cap,
               pe.reason_category
        FROM price_events pe
        JOIN stocks s ON pe.code = s.code
        LEFT JOIN stock_fundamentals f ON pe.code = f.code
        WHERE pe.event_date = %s AND pe.period = %s
        ORDER BY pe.direction, ABS(pe.change_pct) DESC
    """, (event_date, period))
    cols = ["code","name","direction","change_pct","ranking","news_items","ai_summary","market_cap","reason_category"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()

    return {
        "date":    event_date,
        "gainers": [r for r in rows if r["direction"] == "up"],
        "losers":  [r for r in rows if r["direction"] == "down"],
    }


def get_available_event_dates(period: str = "daily", limit: int = 30) -> list:
    """イベントが存在する日付一覧を返す（日付選択UI用）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT event_date FROM price_events
        WHERE period = %s ORDER BY event_date DESC LIMIT %s
    """, (period, limit))
    dates = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return dates


def get_events_for_stock(code: str, limit: int = 20) -> list:
    """銘柄の直近イベント一覧を取得（stock detail ページ用）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT event_date, direction, change_pct, ranking, period,
               news_items, ai_summary, created_at
        FROM price_events
        WHERE code = %s ORDER BY event_date DESC, period LIMIT %s
    """, (code, limit))
    cols = ["event_date","direction","change_pct","ranking","period",
            "news_items","ai_summary","created_at"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


if __name__ == "__main__":
    if len(sys.argv) > 1:
        codes = sys.argv[1:]
        today = date.today()
        print(f"調査戦略: {get_strategy_description()}")
        for code in codes:
            print(f"\n=== {code} のニュースを調査 ===")
            news = fetch_news(code, target_date=today)
            for n in news:
                print(f"  [{n['dt'].strftime('%m/%d %H:%M')}][{n['category']}] {n['title']}")
            if news:
                research_and_save(code, today, "up", 0.0, period="daily")
                print("  保存完了")
    else:
        print(f"=== 日次 ±{RESEARCH_THRESHOLD_PCT}%超えを調査（3フェーズ処理）===")
        n = research_top_movers(period="daily")
        print(f"\n=== 週次 ±{RESEARCH_THRESHOLD_PCT}%超えを調査（3フェーズ処理）===")
        n2 = research_top_movers(period="weekly")
        print(f"\n合計: {n + n2} 件")
