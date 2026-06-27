"""
銘柄別ニュース収集・イベント記録モジュール
調査手法の詳細は research_strategy.py を参照・編集すること。

使い方:
  python event_researcher.py              # ±10%超えの銘柄を調査
  python event_researcher.py 7203 6857   # 指定銘柄を調査
"""
import sys
from datetime import date, timedelta
from config import get_conn
from research_strategy import (
    fetch_news,
    summarize_news,
    get_strategy_description,
    RESEARCH_THRESHOLD_PCT,
    RESEARCH_MAX_PER_DIRECTION,
)


def _format_news_text(news_items: list) -> str:
    """ニュースリストを保存用テキストに変換。"""
    lines = []
    for it in news_items:
        dt_str = it["dt"].strftime("%m/%d %H:%M")
        lines.append(f"[{dt_str}][{it['category']}] {it['title']}")
    return "\n".join(lines)


def _ensure_ai_summary_column():
    """price_events に ai_summary カラムが存在しなければ追加する。"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE price_events ADD COLUMN ai_summary TEXT")
        conn.commit()
        print("  [migration] price_events.ai_summary カラムを追加しました")
    except Exception:
        pass  # 既に存在する場合は無視
    cur.close()
    conn.close()


_ai_column_checked = False


def _get_company_name(code: str) -> str:
    """DBから銘柄名を取得する（Google News RSS の検索クエリ用）。"""
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT name FROM stocks WHERE code = %s", (code,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            # 長い社名は短縮（例: "トヨタ自動車" → 最初の4文字）
            name = row[0]
            return name[:10]
    except Exception:
        pass
    return ""


def research_and_save(code: str, event_date: date, direction: str,
                      change_pct: float, ranking: int = None,
                      period: str = "daily") -> bool:
    """
    1銘柄のニュースを収集して price_events に保存する。
    既存レコードがある場合は上書き更新。
    """
    global _ai_column_checked
    if not _ai_column_checked:
        _ensure_ai_summary_column()
        _ai_column_checked = True

    company_name = _get_company_name(code)
    news = fetch_news(code, target_date=event_date, company_name=company_name)
    news_text  = _format_news_text(news) if news else None
    ai_summary = summarize_news(news, code, company_name, event_date) if news else None

    if ai_summary:
        print(f"    [AI要約完了] {code}")

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


def _get_daily_movers(ranking_date: date, threshold: float, max_n: int) -> tuple:
    """日次の上昇/下落銘柄を取得。threshold% 超えのみ対象。"""
    conn = get_conn()
    cur = conn.cursor()

    # 上昇: threshold% 超え
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

    # 下落: -threshold% 以下
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
    """週次の上昇/下落銘柄を取得。threshold% 超えのみ対象。"""
    week_start = ranking_date - timedelta(days=6)
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
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
            SELECT code,
                MAX(first_close) AS first_close,
                MAX(last_close)  AS last_close
            FROM week_prices GROUP BY code
            HAVING MAX(first_close) > 0
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
        SELECT code, ROW_NUMBER() OVER (ORDER BY chg DESC) AS rk, chg
        FROM ranked WHERE chg >= %s ORDER BY chg DESC LIMIT %s
    """, (week_start, ranking_date, threshold, max_n))
    gainers = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    cur.execute("""
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
            SELECT code,
                MAX(first_close) AS first_close,
                MAX(last_close)  AS last_close
            FROM week_prices GROUP BY code
            HAVING MAX(first_close) > 0
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
        SELECT code, ROW_NUMBER() OVER (ORDER BY chg ASC) AS rk, chg
        FROM ranked WHERE chg <= %s ORDER BY chg ASC LIMIT %s
    """, (week_start, ranking_date, -threshold, max_n))
    losers = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    cur.close()
    conn.close()
    return gainers, losers


def research_top_movers(target_date: date = None, period: str = "daily") -> int:
    """
    ±RESEARCH_THRESHOLD_PCT% 超えの銘柄のニュースを収集・保存。
    閾値・最大件数は research_strategy.py で設定する。
    period: 'daily' | 'weekly'
    """
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
    max_n = RESEARCH_MAX_PER_DIRECTION

    if period == "daily":
        gainers, losers = _get_daily_movers(ranking_date, threshold, max_n)
    else:
        gainers, losers = _get_weekly_movers(ranking_date, threshold, max_n)

    print(f"  [{period}] {ranking_date} ±{threshold}%超え: "
          f"上昇{len(gainers)}件 / 下落{len(losers)}件")
    print(f"  調査戦略: {get_strategy_description()}")

    all_targets = [(c, rk, pct, "up")   for c, rk, pct in gainers] + \
                  [(c, rk, pct, "down") for c, rk, pct in losers]

    if not all_targets:
        print(f"  調査対象なし（閾値 ±{threshold}%）")
        return 0

    saved = 0
    for i, (code, rank, pct, direction) in enumerate(all_targets):
        if research_and_save(code, ranking_date, direction, pct, rank, period):
            saved += 1
        if (i + 1) % 10 == 0:
            print(f"    {i+1}/{len(all_targets)} 完了")

    print(f"  完了: {saved}/{len(all_targets)} 件保存")
    return saved


def get_events_for_date(event_date: date = None, period: str = "daily") -> dict:
    """指定日の全イベントを取得（events ページ用）。"""
    conn = get_conn()
    cur = conn.cursor()

    if event_date is None:
        cur.execute("""
            SELECT MAX(event_date) FROM price_events WHERE period = %s
        """, (period,))
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

    gainers = [r for r in rows if r["direction"] == "up"]
    losers  = [r for r in rows if r["direction"] == "down"]
    return {"date": event_date, "gainers": gainers, "losers": losers}


def get_available_event_dates(period: str = "daily", limit: int = 30) -> list:
    """イベントが存在する日付一覧を返す（日付選択UI用）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT event_date
        FROM price_events
        WHERE period = %s
        ORDER BY event_date DESC
        LIMIT %s
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
        WHERE code = %s
        ORDER BY event_date DESC, period
        LIMIT %s
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
                print(f"  保存完了")
    else:
        print(f"=== 日次 ±{RESEARCH_THRESHOLD_PCT}%超えを調査 ===")
        n = research_top_movers(period="daily")
        print(f"\n=== 週次 ±{RESEARCH_THRESHOLD_PCT}%超えを調査 ===")
        n2 = research_top_movers(period="weekly")
        print(f"\n合計: {n + n2} 件")
