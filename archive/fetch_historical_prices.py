"""
過去2年分の株価データを一括取得するスクリプト。
J-Quantsから2024-06-27から現在までのデータを取得します。
既存データがあれば上書きしません（IGNORE使用）。

実行例: python3 fetch_historical_prices.py
"""
import time
import requests
from datetime import date, timedelta
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import get_conn, JQUANTS_BASE_URL, JQUANTS_HEADERS, bulk_upsert

# 2年前から現在まで取得
DATE_FROM = date(2024, 6, 27)
BATCH_SIZE = 500
MAX_WORKERS = 8


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
        except Exception as e:
            print(f"    エラー {code5}: {e}")
            time.sleep(1)
    return []


def fetch_historical_prices():
    """過去2年分の価格データを取得してDBに保存する。"""
    conn = get_conn()
    cur = conn.cursor()

    # 対象銘柄を取得
    cur.execute("SELECT code FROM stocks WHERE is_active = TRUE ORDER BY code")
    codes4 = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    if not codes4:
        print("対象銘柄がありません。先にマスターデータを作成してください。")
        return

    codes5 = [c + "0" for c in codes4]
    date_from_str = DATE_FROM.strftime("%Y%m%d")
    date_to_str = date.today().strftime("%Y%m%d")

    print(f"過去データ取得開始")
    print(f"  期間: {DATE_FROM} 〜 {date.today()}")
    print(f"  対象銘柄: {len(codes5)} 件")
    print(f"  並列度: {MAX_WORKERS}")

    all_rows = []
    failed = []
    success_count = 0

    def fetch(code5):
        time.sleep(0.05)
        return code5, _fetch_one_stock(code5, date_from_str, date_to_str)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, c): c for c in codes5}
        done = 0
        for future in as_completed(futures):
            code5, rows = future.result()
            done += 1
            if done % 500 == 0 or done == len(codes5):
                print(f"  進捗: {done}/{len(codes5)}")

            if not rows:
                failed.append(code5)
            else:
                all_rows.extend(rows)
                success_count += 1

    print(f"\n  取得完了: {success_count}/{len(codes5)} 銘柄")
    print(f"  総データ行数: {len(all_rows)}")

    if failed:
        print(f"  失敗銘柄: {len(failed)} 件")

    # DBに保存（IGNORE: 既存データは上書きしない）
    if all_rows:
        print("\nDB保存中...")
        conn = get_conn()
        cur = conn.cursor()

        # 既存データを避けるため、INSERT IGNOREを使用
        for i in range(0, len(all_rows), BATCH_SIZE):
            batch = all_rows[i : i + BATCH_SIZE]
            placeholders = ",".join(["%s"] * len(batch))

            sql = """
                INSERT IGNORE INTO daily_prices
                (code, date, open, high, low, close, volume, turnover)
                VALUES
            """
            values = []
            for row in batch:
                values.append(
                    (
                        row.get("code"),
                        row.get("date"),
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row.get("close"),
                        row.get("volume"),
                        row.get("turnover"),
                    )
                )

            sql += ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(batch))
            cur.execute(sql, [item for row in values for item in row])

            if (i // BATCH_SIZE + 1) % 10 == 0:
                print(f"  {i + BATCH_SIZE}行を保存...")

        conn.commit()
        cur.close()
        conn.close()

        print("保存完了！")
        print(f"  追加: {len(all_rows)} 行")

        # 保存後の統計を表示
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM daily_prices")
        result = cur.fetchone()
        min_date, max_date, count = result
        print(f"\n現在のデータ：")
        print(f"  期間: {min_date} 〜 {max_date}")
        print(f"  総行数: {count}")
        cur.close()
        conn.close()


if __name__ == "__main__":
    fetch_historical_prices()
