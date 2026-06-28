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
    summarize_news,
    summarize_news_batch,
    get_strategy_description,
    RESEARCH_THRESHOLD_PCT,
    RESEARCH_MAX_PER_DIRECTION,
)


def _format_news_text(news_items: list) -> str:
    lines = []
    for it in news_items:
        dt_str = it["dt"].strftime("%m/%d %H:%M")
        lines.append(f"[{dt_str}][{it['category']}] {it['title']}")
    return "\n".join(lines)


def _ensure_ai_summary_column():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE price_events ADD COLUMN ai_summary TEXT")
        conn.commit()
        print("  [migration] price_events.ai_summary カラムを追加しました")
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


def _save_event(code: str, event_date: date, direction: str, change_pct: float,
                ranking: int, period: str, news_text: str | None,
                ai_summary: str | None) -> bool:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO price_events
              (code, event_date, direction, change_pct, ranking, period,
               news_items, ai_summary, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
              direction  = VALUES(direction),
              change_pct = VALUES(change_pct),
              ranking    = VALUES(ranking),
              news_items = VALUES(news_items),
              ai_summary = VALUES(ai_summary),
              created_at = NOW()
        """, (code, event_date, direction, change_pct, ranking, period,
              news_text, ai_summary))
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
    news         = fetch_news(code, target_date=event_date, company_name=company_name,
                              direction=direction)
    news_text    = _format_news_text(news) if news else None
    ai_summary   = summarize_news(news, code, company_name, event_date,
                                  direction=direction, change_pct=change_pct) if news else None

    if ai_summary:
        print(f"    [AI要約完了] {code}")

    return _save_event(code, event_date, direction, change_pct, ranking, period,
                       news_text, ai_summary)


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
                    FIRST_VALUE(close) OVER (PARTITION BY code ORDER BY date) AS first_close,
                    LAST_VALUE(close)  OVER (
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

    # ── Phase 1: 全銘柄のニュース並行取得 + 財務データ一括取得 ─────────
    codes = [c for c, _, _, _ in all_targets]
    names   = _get_all_company_names(codes)   # 1クエリで一括取得
    fin_ctx = _get_financials_context(codes)   # 最新2期の業績を一括取得

    print(f"\n  [Phase 1] ニュース並行取得中 ({len(all_targets)}銘柄)...")
    stock_specs = [
        {"code": c, "name": names.get(c, ""), "date": ranking_date, "direction": d}
        for c, _, _, d in all_targets
    ]
    news_by_code = fetch_news_batch(stock_specs)
    news_found   = sum(1 for n in news_by_code.values() if n)
    print(f"  [Phase 1] 完了: {news_found}/{len(all_targets)} 銘柄でニュースあり"
          f"（財務データあり: {len(fin_ctx)}銘柄）")

    # ── Phase 2: ニュースがある銘柄をまとめて AI 要約 ───────────────────
    batch_data = [
        {
            "code": c, "name": names.get(c, ""), "date": ranking_date,
            "direction": d, "change_pct": pct,
            "news": news_by_code.get(c, []),
            "financials": fin_ctx.get(c),   # DB から取得した直近2期の業績
        }
        for c, _, pct, d in all_targets
        if news_by_code.get(c)
    ]

    if batch_data:
        print(f"\n  [Phase 2] AI 要約バッチ処理 ({len(batch_data)}銘柄)...")
        summaries = summarize_news_batch(batch_data)
    else:
        summaries = {}
        print("  [Phase 2] ニュースなし → AI 要約スキップ")

    # ── Phase 3: 全件 DB 保存 ────────────────────────────────────────────
    print(f"\n  [Phase 3] DB 保存...")
    saved = 0
    for code, rank, pct, direction in all_targets:
        news      = news_by_code.get(code, [])
        news_text = _format_news_text(news) if news else None
        ai_sum    = summaries.get(code)

        if _save_event(code, ranking_date, direction, pct, rank, period,
                       news_text, ai_sum):
            saved += 1

    print(f"  [Phase 3] 完了: {saved}/{len(all_targets)} 件保存")
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
               pe.ranking, pe.news_items, pe.ai_summary
        FROM price_events pe
        JOIN stocks s ON pe.code = s.code
        WHERE pe.event_date = %s AND pe.period = %s
        ORDER BY pe.direction, ABS(pe.change_pct) DESC
    """, (event_date, period))
    cols = ["code","name","direction","change_pct","ranking","news_items","ai_summary"]
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
