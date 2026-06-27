"""
日次・週次ランキングの計算とDBへの保存
"""
from datetime import date, timedelta
from typing import Optional
from config import get_conn, bulk_upsert


def _get_last_trading_day(conn, before: date = None) -> Optional[date]:
    cur = conn.cursor()
    if before is None:
        before = date.today()
    cur.execute("""
        SELECT MAX(date) FROM trading_calendar
        WHERE is_holiday = FALSE AND date <= %s
    """, (before,))
    result = cur.fetchone()[0]
    cur.close()
    return result


def compute_daily_rankings(target_date: date = None, top_n: int = 15) -> int:
    """指定日（デフォルト: 直近取引日）の上昇率・出来高ランキングを計算。"""
    conn = get_conn()

    if target_date is None:
        target_date = _get_last_trading_day(conn)
    if target_date is None:
        conn.close()
        return 0

    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM daily_prices WHERE date = %s", (target_date,))
    if cur.fetchone()[0] == 0:
        print(f"  {target_date} の価格データなし（休場日の可能性）")
        cur.close()
        conn.close()
        return 0

    rows = []
    for rank_type, order_col in [("change_pct", "change_pct"), ("volume", "volume"), ("turnover", "turnover")]:
        cur.execute(f"""
            SELECT
                dp.code,
                dp.`{order_col}` AS value,
                ROW_NUMBER() OVER (ORDER BY dp.`{order_col}` DESC) AS `rank`
            FROM daily_prices dp
            JOIN stocks s ON dp.code = s.code
            LEFT JOIN markets m ON s.market_id = m.id
            WHERE dp.date = %s
              AND dp.`{order_col}` IS NOT NULL
              AND s.is_active = TRUE
              AND (s.market_id IS NULL OR m.code IN ('0111','0112','0113'))
            LIMIT %s
        """, (target_date, top_n))

        for code, value, rank in cur.fetchall():
            rows.append(("daily", target_date, rank_type, int(rank), code, float(value)))

    if rows:
        bulk_upsert(cur, "rankings",
            ["period_type", "period_end", "rank_type", "rank", "code", "value"],
            rows,
            update_cols=["code", "value"])
        conn.commit()

    cur.close()
    conn.close()
    return len(rows)


def compute_weekly_rankings(week_ending: date = None, top_n: int = 15) -> int:
    """直近1週間（月〜金）の上昇率ランキングを計算。"""
    conn = get_conn()

    if week_ending is None:
        today = date.today()
        days_since_friday = (today.weekday() - 4) % 7
        week_ending = today - timedelta(days=days_since_friday)
        if days_since_friday == 0 and today.weekday() != 4:
            week_ending = today - timedelta(days=7)
        week_ending = _get_last_trading_day(conn, week_ending) or week_ending

    week_start = week_ending - timedelta(days=6)

    cur = conn.cursor()

    cur.execute("""
        WITH week_prices AS (
            SELECT
                code,
                FIRST_VALUE(close) OVER (PARTITION BY code ORDER BY date) AS first_close,
                LAST_VALUE(close)  OVER (
                    PARTITION BY code ORDER BY date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                ) AS last_close
            FROM daily_prices
            WHERE date BETWEEN %s AND %s
        ),
        weekly AS (
            SELECT
                code,
                MAX(first_close) AS first_close,
                MAX(last_close)  AS last_close
            FROM week_prices
            GROUP BY code
            HAVING MAX(first_close) > 0
        )
        SELECT
            w.code,
            ROUND((w.last_close - w.first_close) / w.first_close * 100, 4) AS change_pct_1w,
            ROW_NUMBER() OVER (ORDER BY (w.last_close - w.first_close) / w.first_close DESC) AS `rank`
        FROM weekly w
        JOIN stocks s ON w.code = s.code
        LEFT JOIN markets m ON s.market_id = m.id
        WHERE s.is_active = TRUE
          AND (s.market_id IS NULL OR m.code IN ('0111','0112','0113'))
        LIMIT %s
    """, (week_start, week_ending, top_n))

    rows = []
    for code, value, rank in cur.fetchall():
        rows.append(("weekly", week_ending, "change_pct", int(rank), code, float(value)))

    if rows:
        bulk_upsert(cur, "rankings",
            ["period_type", "period_end", "rank_type", "rank", "code", "value"],
            rows,
            update_cols=["code", "value"])
        conn.commit()

    cur.close()
    conn.close()
    return len(rows)


def print_rankings(period_type: str = "daily", rank_type: str = "change_pct",
                   period_end: date = None):
    """ランキングをコンソールに表示（確認用）。"""
    conn = get_conn()
    cur = conn.cursor()

    if period_end is None:
        cur.execute("""
            SELECT MAX(period_end) FROM rankings
            WHERE period_type=%s AND rank_type=%s
        """, (period_type, rank_type))
        period_end = cur.fetchone()[0]

    cur.execute("""
        SELECT r.`rank`, r.code, s.name, r.value
        FROM rankings r JOIN stocks s ON r.code = s.code
        WHERE r.period_type=%s AND r.period_end=%s AND r.rank_type=%s
        ORDER BY r.`rank`
    """, (period_type, period_end, rank_type))

    unit = {"change_pct": "%", "volume": "株", "turnover": "円"}
    print(f"\n【{period_type.upper()} {rank_type} ランキング】{period_end}")
    print("-" * 55)
    for rank, code, name, value in cur.fetchall():
        u = unit.get(rank_type, "")
        v = f"{value:+.2f}{u}" if rank_type == "change_pct" else f"{value:,.0f}{u}"
        print(f"  {rank:2d}位  {code}  {name[:20]:<20}  {v}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    print("=== 日次ランキング計算 ===")
    n = compute_daily_rankings()
    print(f"  {n} 件登録")

    print("=== 週次ランキング計算 ===")
    n = compute_weekly_rankings()
    print(f"  {n} 件登録")

    print_rankings("daily", "change_pct")
