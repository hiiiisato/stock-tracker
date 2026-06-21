"""
日次価格データの取得・保存
- 差分更新: DBの最終取得日の翌日から最新日まで取得
- 並列取得 + レート制限対応
- change_pct（前日比）は直前の取引日と比較して算出
"""
import time
import requests
from datetime import date
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from psycopg2.extras import execute_values
from config import get_conn, JQUANTS_BASE_URL, JQUANTS_HEADERS


def _get_missing_date_range(conn) -> Tuple[Optional[date], Optional[date]]:
    """DBに存在しない価格データの日付範囲を返す。"""
    cur = conn.cursor()

    # DBの最終取得日
    cur.execute("SELECT MAX(date) FROM daily_prices")
    last_date = cur.fetchone()[0]

    # APIで取得可能な最終営業日
    cur.execute("""
        SELECT MAX(date) FROM trading_calendar
        WHERE is_holiday = FALSE AND date <= CURRENT_DATE
    """)
    last_trading = cur.fetchone()[0]

    cur.close()

    if last_trading is None:
        return None, None

    if last_date is None:
        # 初回: フリープランの開始日から
        return date(2024, 3, 30), last_trading

    if last_date >= last_trading:
        return None, None  # 更新不要

    from datetime import timedelta
    return last_date + timedelta(days=1), last_trading


def _fetch_one_stock(code5: str, date_from: str, date_to: str) -> List[dict]:
    """1銘柄の価格データをAPIから取得する。失敗時は空リストを返す。"""
    for attempt in range(3):
        try:
            r = requests.get(
                f"{JQUANTS_BASE_URL}/equities/bars/daily",
                headers=JQUANTS_HEADERS,
                params={"code": code5, "date_from": date_from, "date_to": date_to},
                timeout=30,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception:
            time.sleep(1)
    return []


def fetch_and_store_prices(max_workers: int = 8) -> int:
    """
    差分更新: DBにない日付範囲の価格データを全銘柄分取得してUPSERT。
    """
    conn = get_conn()
    date_from, date_to = _get_missing_date_range(conn)

    if date_from is None:
        print("  価格データは最新です。更新不要。")
        conn.close()
        return 0

    print(f"  取得範囲: {date_from} 〜 {date_to}")

    # アクティブ銘柄のコード一覧（5桁で渡す必要がある）
    cur = conn.cursor()
    cur.execute("SELECT code FROM stocks WHERE is_active = TRUE ORDER BY code")
    codes4 = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    codes5 = [c + "0" for c in codes4]  # 4桁→5桁
    date_from_str = date_from.strftime("%Y%m%d")
    date_to_str   = date_to.strftime("%Y%m%d")

    print(f"  対象銘柄: {len(codes5)} 件 (並列{max_workers}本)")

    all_rows = []
    failed = []

    def fetch(code5):
        time.sleep(0.05)  # レート制限対策
        return code5, _fetch_one_stock(code5, date_from_str, date_to_str)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch, c): c for c in codes5}
        done = 0
        for future in as_completed(futures):
            code5, rows = future.result()
            done += 1
            if not rows:
                failed.append(code5)
            else:
                all_rows.extend(rows)
            if done % 500 == 0:
                print(f"    進捗: {done}/{len(codes5)} 銘柄完了")

    print(f"  取得完了: {len(all_rows)} 件, 失敗: {len(failed)} 銘柄")
    if failed:
        print(f"  失敗銘柄(先頭10): {failed[:10]}")

    if not all_rows:
        return 0

    # DBに保存
    db_rows = [(
        d["Code"][:4],         # code (4桁)
        d["Date"],
        d.get("O"),
        d.get("H"),
        d.get("L"),
        d["C"],
        int(d["Vo"]) if d.get("Vo") else None,
        int(d["Va"]) if d.get("Va") else None,
        d.get("AdjC"),
        d.get("AdjFactor", 1.0),
        d.get("UL") == "1",    # is_upper_limit
        d.get("LL") == "1",    # is_lower_limit
    ) for d in all_rows]

    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO daily_prices
          (code, date, open, high, low, close, volume, turnover,
           adj_close, adj_factor, is_upper_limit, is_lower_limit)
        VALUES %s
        ON CONFLICT (code, date) DO UPDATE SET
            open           = EXCLUDED.open,
            high           = EXCLUDED.high,
            low            = EXCLUDED.low,
            close          = EXCLUDED.close,
            volume         = EXCLUDED.volume,
            turnover       = EXCLUDED.turnover,
            adj_close      = EXCLUDED.adj_close,
            adj_factor     = EXCLUDED.adj_factor,
            is_upper_limit = EXCLUDED.is_upper_limit,
            is_lower_limit = EXCLUDED.is_lower_limit
    """, db_rows, page_size=1000)
    conn.commit()

    _update_change_pct(conn, cur, date_from, date_to)

    cur.close()
    conn.close()
    return len(db_rows)


def _update_change_pct(conn, cur, date_from: date, date_to: date):
    """
    前取引日比の変化率を更新。
    LAG(close) OVER (PARTITION BY code ORDER BY date) で直前取引日と比較。
    """
    cur.execute("""
        UPDATE daily_prices dp
        SET change_pct = ROUND(
            (dp.close - prev.close) / prev.close * 100,
            4
        )
        FROM (
            SELECT
                code, date,
                LAG(close) OVER (PARTITION BY code ORDER BY date) AS close
            FROM daily_prices
            WHERE date >= %s - INTERVAL '7 days'  -- 前の取引日を含むため余裕を持つ
              AND date <= %s
        ) prev
        WHERE dp.code = prev.code
          AND dp.date = prev.date
          AND dp.date >= %s
          AND prev.close IS NOT NULL
          AND prev.close > 0
    """, (date_from, date_to, date_from))
    conn.commit()
    print(f"  change_pct 更新完了")


if __name__ == "__main__":
    print("=== 価格データ更新 ===")
    n = fetch_and_store_prices()
    print(f"  合計 {n} 件を保存")
