"""
配当履歴の取得・保存（Yahoo Finance chart API 使用）
権利落ち日と1株配当金額を保存する。
"""
import time
import requests
from datetime import date, datetime
from typing import List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import get_conn, bulk_upsert

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-tracker/1.0)"}


def _fetch_dividends(code4: str) -> List[Tuple]:
    """1銘柄の配当履歴を取得。"""
    ticker = f"{code4}.T"
    url = YAHOO_CHART.format(ticker=ticker)
    for attempt in range(3):
        try:
            r = requests.get(url,
                params={"interval": "1mo", "range": "10y", "events": "dividends"},
                headers=HEADERS, timeout=15)
            if r.status_code == 429:
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code != 200:
                return []
            divs = (r.json().get("chart", {}).get("result", [{}])[0]
                    .get("events", {}).get("dividends", {}))
            rows = []
            for v in divs.values():
                ex_date = datetime.fromtimestamp(v["date"]).date()
                amount = v.get("amount")
                if amount and amount > 0:
                    rows.append((code4, ex_date, round(float(amount), 4)))
            return rows
        except Exception:
            time.sleep(1)
    return []


def fetch_all_dividends(max_workers: int = 10) -> int:
    """全銘柄の配当履歴を取得してUPSERT。週次実行推奨。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT code FROM stocks WHERE is_active=TRUE ORDER BY code")
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    print(f"  対象銘柄: {len(codes)}件 (並列{max_workers}本)")

    all_rows = []

    def fetch(code4):
        time.sleep(0.02)
        return _fetch_dividends(code4)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch, c): c for c in codes}
        done = 0
        for future in as_completed(futures):
            rows = future.result()
            if rows:
                all_rows.extend(rows)
            done += 1
            if done % 1000 == 0:
                print(f"    {done}/{len(codes)} 完了")

    if not all_rows:
        print("  配当データなし")
        return 0

    conn = get_conn()
    cur = conn.cursor()
    bulk_upsert(cur, "dividends",
        ["code", "ex_date", "amount"],
        all_rows,
        update_cols=["amount"])
    conn.commit()
    cur.close()
    conn.close()
    print(f"  配当データ: {len(all_rows):,}件 保存")
    return len(all_rows)


if __name__ == "__main__":
    print("=== 配当データ取得 ===")
    n = fetch_all_dividends()
    print(f"  合計 {n:,} 件")
