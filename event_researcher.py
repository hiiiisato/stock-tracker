"""
銘柄別ニュース収集・イベント記録モジュール
kabutan.jp の銘柄ニュースページをスクレイピングして price_events に保存する。

使い方:
  python event_researcher.py              # 直近の上昇/下落トップ15を調査
  python event_researcher.py 7203 6857   # 指定銘柄を調査
"""
import sys
import time
import requests
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta
from config import get_conn

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
NEWS_URL = "https://kabutan.jp/stock/news?code={code}"

# 重要度の高いカテゴリ（価格変動理由として有用）
PRIORITY_CATEGORIES = {"材料", "開示", "業績", "決算", "注目"}


def scrape_news(code: str, target_date: date = None, max_items: int = 5) -> list:
    """
    kabutan.jp から銘柄のニュースを取得する。
    target_date 指定時はその日前後のニュースを優先して返す。
    """
    url = NEWS_URL.format(code=code)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []
    except Exception as e:
        print(f"  [news] {code}: リクエストエラー {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    tbl = soup.find("table", class_="s_news_list")
    if not tbl:
        return []

    items = []
    for row in tbl.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 3:
            continue
        time_el = tds[0].find("time")
        if not time_el:
            continue
        dt_str = time_el.get("datetime", "")
        # datetime="2026-06-26T12:57:00+09:00" 形式
        try:
            news_dt = datetime.fromisoformat(dt_str[:16])
        except ValueError:
            try:
                news_dt = datetime.strptime(dt_str[:10], "%Y-%m-%d")
            except ValueError:
                continue

        category = tds[1].text.strip()
        link = tds[2].find("a")
        title = link.text.strip() if link else tds[2].text.strip()
        href = link.get("href", "") if link else ""

        items.append({
            "dt": news_dt,
            "date": news_dt.date(),
            "category": category,
            "title": title,
            "href": href,
        })

    if not items:
        return []

    if target_date:
        # target_date ±1日のニュースを優先、次に直近を追加
        priority = [it for it in items
                    if abs((it["date"] - target_date).days) <= 1
                    and it["category"] in PRIORITY_CATEGORIES]
        nearby   = [it for it in items
                    if abs((it["date"] - target_date).days) <= 1
                    and it not in priority]
        rest     = [it for it in items if it not in priority and it not in nearby]
        ordered  = priority + nearby + rest
    else:
        # 優先カテゴリを前に
        priority = [it for it in items if it["category"] in PRIORITY_CATEGORIES]
        rest     = [it for it in items if it not in priority]
        ordered  = priority + rest

    return ordered[:max_items]


def format_news_text(news_items: list) -> str:
    """ニュースリストを保存用テキストに変換。"""
    lines = []
    for it in news_items:
        dt_str = it["dt"].strftime("%m/%d %H:%M")
        lines.append(f"[{dt_str}][{it['category']}] {it['title']}")
    return "\n".join(lines)


def research_and_save(code: str, event_date: date, direction: str,
                      change_pct: float, ranking: int = None,
                      period: str = "daily") -> bool:
    """
    1銘柄のニュースを収集して price_events に保存する。
    既存レコードがある場合は上書き更新。
    """
    news = scrape_news(code, target_date=event_date, max_items=8)
    news_text = format_news_text(news) if news else None

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO price_events
              (code, event_date, direction, change_pct, ranking, period, news_items, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
              direction  = VALUES(direction),
              change_pct = VALUES(change_pct),
              ranking    = VALUES(ranking),
              news_items = VALUES(news_items),
              created_at = NOW()
        """, (code, event_date, direction, change_pct, ranking, period, news_text))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"  [event] 保存エラー {code}/{event_date}: {e}")
        return False


def research_top_movers(target_date: date = None, top_n: int = 15,
                        period: str = "daily") -> int:
    """
    ランキングテーブルから上昇/下落 TOP-N を取得してニュース収集・保存。
    period: 'daily' | 'weekly'
    """
    if target_date is None:
        target_date = date.today()

    conn = get_conn()
    cur = conn.cursor()

    # 最新ランキング日付を取得
    cur.execute("""
        SELECT MAX(period_end) FROM rankings
        WHERE period_type = %s AND rank_type = 'change_pct'
    """, (period,))
    row = cur.fetchone()
    ranking_date = row[0] if row and row[0] else target_date

    if period == "daily":
        # 日次: 上昇は rankings テーブルから、下落は daily_prices から直接取得
        cur.execute("""
            SELECT code, `rank`, value
            FROM rankings
            WHERE period_type = 'daily' AND period_end = %s AND rank_type = 'change_pct'
            ORDER BY value DESC
            LIMIT %s
        """, (ranking_date, top_n))
        gainers = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

        cur.execute("""
            SELECT dp.code, ROW_NUMBER() OVER (ORDER BY dp.change_pct ASC) AS rk, dp.change_pct
            FROM daily_prices dp
            JOIN stocks s ON dp.code = s.code
            LEFT JOIN markets m ON s.market_id = m.id
            WHERE dp.date = %s
              AND dp.change_pct IS NOT NULL AND dp.change_pct < 0
              AND s.is_active = TRUE
              AND (s.market_id IS NULL OR m.code IN ('0111','0112','0113'))
            ORDER BY dp.change_pct ASC
            LIMIT %s
        """, (ranking_date, top_n))
        losers = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    else:
        # 週次: rankings テーブルに上昇のみ → 下落は別途計算
        cur.execute("""
            SELECT code, `rank`, value
            FROM rankings
            WHERE period_type = 'weekly' AND period_end = %s AND rank_type = 'change_pct'
            ORDER BY value DESC
            LIMIT %s
        """, (ranking_date, top_n))
        gainers = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

        week_start = ranking_date - timedelta(days=6)
        cur.execute("""
            WITH week_prices AS (
                SELECT code,
                    FIRST_VALUE(close) OVER (PARTITION BY code ORDER BY date) AS first_close,
                    LAST_VALUE(close)  OVER (
                        PARTITION BY code ORDER BY date
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    ) AS last_close
                FROM daily_prices
                WHERE date BETWEEN %s AND %s
            ),
            weekly AS (
                SELECT code,
                    MAX(first_close) AS first_close,
                    MAX(last_close)  AS last_close
                FROM week_prices GROUP BY code
                HAVING MAX(first_close) > 0
            )
            SELECT w.code,
                ROW_NUMBER() OVER (ORDER BY (w.last_close - w.first_close)/w.first_close ASC) AS rk,
                ROUND((w.last_close - w.first_close) / w.first_close * 100, 4) AS chg
            FROM weekly w
            JOIN stocks s ON w.code = s.code
            LEFT JOIN markets m ON s.market_id = m.id
            WHERE s.is_active = TRUE
              AND (s.market_id IS NULL OR m.code IN ('0111','0112','0113'))
              AND (w.last_close - w.first_close) / w.first_close < 0
            ORDER BY chg ASC
            LIMIT %s
        """, (week_start, ranking_date, top_n))
        losers = [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    cur.close()
    conn.close()

    saved = 0
    all_targets = [(c, rk, pct, "up") for c, rk, pct in gainers] + \
                  [(c, rk, pct, "down") for c, rk, pct in losers]

    print(f"  [{period}] {ranking_date} の上昇/下落 各{top_n}件を調査中...")
    for i, (code, rank, pct, direction) in enumerate(all_targets):
        if research_and_save(code, ranking_date, direction, pct, rank, period):
            saved += 1
        # kabutan.jpへの過負荷を防ぐ
        time.sleep(0.8)
        if (i + 1) % 10 == 0:
            print(f"    {i+1}/{len(all_targets)} 完了")

    print(f"  完了: {saved}/{len(all_targets)} 件保存")
    return saved


def get_events_for_stock(code: str, limit: int = 20) -> list:
    """銘柄の直近イベント一覧を取得（app.py 表示用）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT event_date, direction, change_pct, ranking, period, news_items, created_at
        FROM price_events
        WHERE code = %s
        ORDER BY event_date DESC, period
        LIMIT %s
    """, (code, limit))
    cols = ["event_date", "direction", "change_pct", "ranking", "period", "news_items", "created_at"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


if __name__ == "__main__":
    if len(sys.argv) > 1:
        codes = sys.argv[1:]
        today = date.today()
        for code in codes:
            print(f"=== {code} のニュースを調査 ===")
            news = scrape_news(code, target_date=today, max_items=8)
            for n in news:
                print(f"  [{n['dt'].strftime('%m/%d %H:%M')}][{n['category']}] {n['title']}")
            if news:
                research_and_save(code, today, "up", 0.0, period="daily")
                print(f"  保存完了")
    else:
        print("=== 日次 TOP15 上昇/下落を調査 ===")
        n = research_top_movers(period="daily", top_n=15)
        print(f"\n=== 週次 TOP15 上昇/下落を調査 ===")
        n2 = research_top_movers(period="weekly", top_n=15)
        print(f"\n合計: {n + n2} 件")
