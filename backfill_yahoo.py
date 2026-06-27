"""
Yahoo Finance で過去2年分の価格データを一括取得するバックフィルスクリプト。

J-Quantsフリープランは直近12週しか提供できないため、
Yahoo Finance（無料・認証不要）で補完する。

実行方法:
  python3 backfill_yahoo.py

完了目安:
  4,000銘柄 × 2年分 = 約20〜30分
  途中から再実行しても INSERT IGNORE で安全（重複スキップ）

注意:
  - ローカルから実行すること（Renderではタイムアウトする）
  - 東京市場の終値が対象。立会外取引は含まない
"""

import time
import requests
from datetime import date, timedelta, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import get_conn

# ─── 設定 ──────────────────────────────────────────────────────────────────
TARGET_FROM = date.today() - timedelta(days=365 * 2 + 60)  # 2年2ヶ月前（余裕を持つ）
MAX_WORKERS = 6       # 並列数（多すぎると Yahoo がレート制限をかける）
DELAY       = 0.18    # 銘柄ごとの待機秒数（並列考慮後のレート: 6/0.18 ≈ 33 req/s）
BATCH_SIZE  = 500     # DB書き込みバッチサイズ
# ──────────────────────────────────────────────────────────────────────────

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _fetch_yahoo(code4: str, date_from: date, date_to: date) -> list:
    """Yahoo Finance v8 API から1銘柄の日次データを取得。"""
    ticker   = f"{code4}.T"
    url      = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    ts_from  = int(datetime.combine(date_from, datetime.min.time()).timestamp())
    ts_to    = int(datetime.combine(date_to,   datetime.min.time()).timestamp()) + 86400

    for attempt in range(3):
        try:
            r = requests.get(
                url,
                params={"interval": "1d", "period1": ts_from, "period2": ts_to},
                headers=HEADERS,
                timeout=20,
            )
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                print(f"    [レート制限] {code4}: {wait}秒待機...")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return []

            result = r.json().get("chart", {}).get("result")
            if not result:
                return []

            res     = result[0]
            tss     = res.get("timestamp", [])
            quotes  = res.get("indicators", {}).get("quote", [{}])[0]
            opens   = quotes.get("open",   [])
            highs   = quotes.get("high",   [])
            lows    = quotes.get("low",    [])
            closes  = quotes.get("close",  [])
            volumes = quotes.get("volume", [])

            rows = []
            for i, ts in enumerate(tss):
                close = closes[i] if i < len(closes) else None
                if close is None:
                    continue
                dt = datetime.fromtimestamp(ts).date()
                if not (date_from <= dt <= date_to):
                    continue
                rows.append((
                    code4,
                    str(dt),
                    round(opens[i],  2) if i < len(opens)   and opens[i]   else None,
                    round(highs[i],  2) if i < len(highs)   and highs[i]   else None,
                    round(lows[i],   2) if i < len(lows)    and lows[i]    else None,
                    round(close,     2),
                    int(volumes[i])     if i < len(volumes)  and volumes[i] else None,
                ))
            return rows

        except Exception as e:
            time.sleep(2 ** attempt)
    return []


def _update_change_pct(conn, date_from: date, date_to: date):
    """バックフィル期間の前日比変化率を一括更新。"""
    cur = conn.cursor()
    window_from = date_from - timedelta(days=10)  # 直前の取引日を拾うため余裕を持つ

    cur.execute("""
        UPDATE daily_prices dp
        JOIN (
            SELECT code, date,
                   LAG(close) OVER (PARTITION BY code ORDER BY date) AS prev_close
            FROM daily_prices
            WHERE date >= %s AND date <= %s
        ) sub ON dp.code = sub.code AND dp.date = sub.date
        SET dp.change_pct = CASE
            WHEN sub.prev_close IS NULL OR sub.prev_close <= 0 THEN NULL
            WHEN ABS((dp.close - sub.prev_close) / sub.prev_close * 100) > 9999 THEN NULL
            ELSE ROUND((dp.close - sub.prev_close) / sub.prev_close * 100, 4)
        END
        WHERE dp.date >= %s
          AND sub.prev_close IS NOT NULL
          AND sub.prev_close > 0
    """, (window_from, date_to, date_from))

    conn.commit()
    cur.close()
    print(f"  change_pct 更新完了")


def backfill_history():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT code FROM stocks WHERE is_active = TRUE ORDER BY code")
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    date_from = TARGET_FROM
    date_to   = date.today()

    est_minutes = len(codes) * DELAY / MAX_WORKERS / 60
    print("=" * 60)
    print("  Yahoo Finance 過去2年バックフィル")
    print("=" * 60)
    print(f"  取得期間 : {date_from} 〜 {date_to}")
    print(f"  対象銘柄 : {len(codes):,} 件")
    print(f"  並列数   : {MAX_WORKERS}")
    print(f"  目安時間 : {est_minutes:.0f}〜{est_minutes*1.5:.0f} 分")
    print()

    all_rows = []
    failed   = []
    start_ts = time.time()

    def fetch(code4):
        time.sleep(DELAY)
        return code4, _fetch_yahoo(code4, date_from, date_to)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, c): c for c in codes}
        done = 0
        for future in as_completed(futures):
            code4, rows = future.result()
            done += 1
            if not rows:
                failed.append(code4)
            else:
                all_rows.extend(rows)

            if done % 200 == 0 or done == len(codes):
                elapsed = time.time() - start_ts
                remain  = elapsed / done * (len(codes) - done) / 60
                print(f"  [{done:>4}/{len(codes)}]  取得行 {len(all_rows):>8,}  "
                      f"失敗 {len(failed):>3}  残り約 {remain:.0f}分")

    elapsed_total = (time.time() - start_ts) / 60
    print(f"\n  取得完了（{elapsed_total:.1f}分）: {len(all_rows):,} 行 / 失敗: {len(failed)} 銘柄")
    if failed and len(failed) <= 30:
        print(f"  失敗銘柄: {failed}")
    elif failed:
        print(f"  失敗銘柄（先頭30）: {failed[:30]}")

    if not all_rows:
        print("保存するデータがありません。")
        return

    # DB 保存（INSERT IGNORE: 既存データは上書きしない）
    print(f"\n  DB保存中（{len(all_rows):,} 行）...")
    conn = get_conn()
    cur  = conn.cursor()

    for i in range(0, len(all_rows), BATCH_SIZE):
        batch = all_rows[i : i + BATCH_SIZE]
        ph    = ",".join(["(%s,%s,%s,%s,%s,%s,%s)"] * len(batch))
        sql   = (
            "INSERT IGNORE INTO daily_prices "
            "(code,date,open,high,low,close,volume) "
            f"VALUES {ph}"
        )
        cur.execute(sql, [v for row in batch for v in row])

        if (i // BATCH_SIZE + 1) % 100 == 0:
            conn.commit()
            print(f"    {i + BATCH_SIZE:,} 行を保存...")

    conn.commit()
    print("  INSERT 完了")

    # change_pct 更新
    _update_change_pct(conn, date_from, date_to)

    # 完了後の統計
    cur = conn.cursor()
    cur.execute("""
        SELECT MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT code)
        FROM daily_prices WHERE date > '2000-01-01'
    """)
    r = cur.fetchone()
    cur.close()
    conn.close()

    print()
    print("=" * 60)
    print("  完了！")
    print("=" * 60)
    print(f"  期間    : {r[0]} 〜 {r[1]}")
    print(f"  総行数  : {r[2]:,}")
    print(f"  銘柄数  : {r[3]:,}")


if __name__ == "__main__":
    backfill_history()
