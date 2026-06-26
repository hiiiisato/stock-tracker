"""
Yahoo Finance APIで日本株の当日〜直近価格を取得してTiDBに保存。
J-Quantsフリープランの範囲外（2026-03-31以降）を補完する。

取得タイミング: 東京市場終了後 (15:30 JST) 以降に実行すること
"""
import time
import requests
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import get_conn, bulk_upsert

YAHOO_API = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-tracker/1.0)"}


def _get_missing_date_range(conn) -> Tuple[Optional[date], Optional[date]]:
    """DBに不足している日付範囲を返す（Yahoo Finance補完用）。"""
    cur = conn.cursor()
    cur.execute("SELECT MAX(date) FROM daily_prices")
    last_date = cur.fetchone()[0]
    cur.close()

    today = date.today()

    if last_date is None:
        return date(2024, 3, 30), today
    if last_date >= today:
        return None, None

    return last_date + timedelta(days=1), today


def _fetch_yahoo(code4: str, date_from: date, date_to: date) -> List[dict]:
    """Yahoo Finance APIから1銘柄の日次データを取得。"""
    ticker = f"{code4}.T"
    url = YAHOO_API.format(ticker=ticker)

    import_from = int(datetime.combine(date_from, datetime.min.time()).timestamp())
    import_to   = int(datetime.combine(date_to,   datetime.min.time()).timestamp()) + 86400

    for attempt in range(3):
        try:
            r = requests.get(url,
                params={"interval": "1d", "period1": import_from, "period2": import_to},
                headers=HEADERS,
                timeout=15,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code != 200:
                return []

            result = r.json().get("chart", {}).get("result")
            if not result:
                return []

            res = result[0]
            timestamps = res.get("timestamp", [])
            quotes     = res.get("indicators", {}).get("quote", [{}])[0]
            opens      = quotes.get("open",   [])
            highs      = quotes.get("high",   [])
            lows       = quotes.get("low",    [])
            closes     = quotes.get("close",  [])
            volumes    = quotes.get("volume", [])

            rows = []
            for i, ts in enumerate(timestamps):
                close = closes[i] if i < len(closes) else None
                if close is None:
                    continue
                dt = datetime.fromtimestamp(ts).date()
                rows.append({
                    "code":   code4,
                    "date":   str(dt),
                    "open":   round(opens[i],  2) if i < len(opens)   and opens[i]   else None,
                    "high":   round(highs[i],  2) if i < len(highs)   and highs[i]   else None,
                    "low":    round(lows[i],   2) if i < len(lows)    and lows[i]    else None,
                    "close":  round(close,     2),
                    "volume": int(volumes[i])     if i < len(volumes)  and volumes[i] else None,
                })
            return rows
        except Exception:
            time.sleep(1)
    return []


def fetch_and_store_yahoo(max_workers: int = 10) -> int:
    """差分更新: Yahoo Finance APIで不足している直近データを全銘柄取得してUPSERT。"""
    conn = get_conn()
    date_from, date_to = _get_missing_date_range(conn)

    if date_from is None:
        print("  Yahoo: 価格データは最新です。更新不要。")
        conn.close()
        return 0

    print(f"  Yahoo取得範囲: {date_from} 〜 {date_to}")

    cur = conn.cursor()
    cur.execute("SELECT code FROM stocks WHERE is_active = TRUE ORDER BY code")
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    print(f"  対象銘柄: {len(codes)} 件 (並列{max_workers}本)")

    all_rows = []
    failed = []

    def fetch(code4):
        time.sleep(0.02)
        return code4, _fetch_yahoo(code4, date_from, date_to)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch, c): c for c in codes}
        done = 0
        for future in as_completed(futures):
            code4, rows = future.result()
            done += 1
            if not rows:
                failed.append(code4)
            else:
                all_rows.extend(rows)
            if done % 1000 == 0:
                print(f"    進捗: {done}/{len(codes)} 銘柄完了")

    print(f"  取得完了: {len(all_rows)} 件, データなし: {len(failed)} 銘柄")

    if not all_rows:
        return 0

    db_rows = [(
        r["code"], r["date"], r["open"], r["high"],
        r["low"],  r["close"], r["volume"],
    ) for r in all_rows]

    conn = get_conn()
    cur = conn.cursor()
    bulk_upsert(cur, "daily_prices",
        ["code", "date", "open", "high", "low", "close", "volume"],
        db_rows,
        update_cols=["open", "high", "low", "close", "volume"])
    conn.commit()

    # 前日比を更新
    cur.execute("""
        UPDATE daily_prices dp
        JOIN (
            SELECT code, date,
                   LAG(close) OVER (PARTITION BY code ORDER BY date) AS prev_close
            FROM daily_prices
            WHERE date >= %s - INTERVAL 7 DAY
              AND date <= %s
        ) sub ON dp.code = sub.code AND dp.date = sub.date
        SET dp.change_pct = CASE
            WHEN ABS((dp.close - sub.prev_close) / sub.prev_close * 100) > 9999 THEN NULL
            ELSE ROUND((dp.close - sub.prev_close) / sub.prev_close * 100, 4)
        END
        WHERE dp.date >= %s
          AND sub.prev_close IS NOT NULL
          AND sub.prev_close > 0
    """, (date_from, date_to, date_from))
    conn.commit()

    cur.close()
    conn.close()
    return len(db_rows)


if __name__ == "__main__":
    print("=== Yahoo Finance 価格データ更新 ===")
    n = fetch_and_store_yahoo()
    print(f"  合計 {n} 件を保存")
