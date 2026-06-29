"""
株式分割対応バックフィル
========================
1. stock_splits テーブルを作成（分割イベント記録用）
2. 全銘柄の adj_close / adj_factor を Yahoo Finance から再取得してDB更新
3. change_pct を adj_close ベースで全件再計算

実行方法:
  python3 split_backfill.py            # 全銘柄（時間がかかる）
  python3 split_backfill.py --splits   # 分割疑い銘柄のみ（change_pct < -40%）
  python3 split_backfill.py --all      # 明示的に全銘柄
"""
import sys
import time
import requests
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import get_conn, bulk_upsert

YAHOO_API = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; stock-tracker/1.0)"}
EPOCH_FROM = date(2024, 1, 1)   # 取得開始日（J-Quants補完開始日に合わせる）


# ─────────────────────────────────────────────────────────────────────────────
# テーブル初期化
# ─────────────────────────────────────────────────────────────────────────────

def ensure_splits_table():
    """stock_splits テーブルを作成する（初回のみ）。"""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_splits (
            code         VARCHAR(10)   NOT NULL,
            ex_date      DATE          NOT NULL,
            split_ratio  DECIMAL(10,4) NOT NULL COMMENT 'new/old 例: 10:1分割なら10.0',
            detected_at  DATETIME      DEFAULT CURRENT_TIMESTAMP,
            source       VARCHAR(20)   DEFAULT 'yahoo',
            PRIMARY KEY (code, ex_date)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("stock_splits テーブル: OK")


# ─────────────────────────────────────────────────────────────────────────────
# Yahoo Finance からの adj_close 取得
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_adj(code4: str) -> tuple[list[dict], list[dict]]:
    """
    Yahoo Finance から全期間の adj_close / adj_factor / splits を取得。
    戻り値: (price_rows, split_rows)
    """
    ticker = f"{code4}.T"
    url    = YAHOO_API.format(ticker=ticker)
    p1 = int(datetime.combine(EPOCH_FROM, datetime.min.time()).timestamp())
    p2 = int(datetime.now().timestamp()) + 86400

    for attempt in range(3):
        try:
            r = requests.get(url,
                params={"interval": "1d", "period1": p1, "period2": p2},
                headers=HEADERS,
                timeout=20,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt + 2)
                continue
            if r.status_code != 200:
                return [], []

            result = r.json().get("chart", {}).get("result")
            if not result:
                return [], []

            res        = result[0]
            timestamps = res.get("timestamp", [])
            quotes     = res.get("indicators", {}).get("quote", [{}])[0]
            closes     = quotes.get("close",  [])
            adjcloses  = res.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose", [])

            # 株式分割イベント
            splits_raw = res.get("events", {}).get("splits", {})
            split_rows = []
            for _, s in splits_raw.items():
                ex_dt    = datetime.fromtimestamp(s["date"]).date()
                numerator   = s.get("numerator",   1)
                denominator = s.get("denominator", 1)
                if denominator and denominator > 0:
                    ratio = round(numerator / denominator, 4)
                    split_rows.append({
                        "code":        code4,
                        "ex_date":     str(ex_dt),
                        "split_ratio": ratio,
                        "source":      "yahoo",
                    })

            # 価格行（adj_close / adj_factor のみ更新）
            price_rows = []
            for i, ts in enumerate(timestamps):
                close = closes[i] if i < len(closes) else None
                if close is None or close == 0:
                    continue
                adj_c = adjcloses[i] if (i < len(adjcloses) and adjcloses[i] is not None) else close
                adj_c = round(float(adj_c), 4)
                adj_factor = round(adj_c / float(close), 6)
                dt = datetime.fromtimestamp(ts).date()
                price_rows.append({
                    "code":      code4,
                    "date":      str(dt),
                    "adj_close": adj_c,
                    # adj_factor は後でDBのcloseを基準に計算する（Yahoo返値のcloseは遡及調整済みのため）
                })

            return price_rows, split_rows

        except Exception:
            time.sleep(1)

    return [], []


# ─────────────────────────────────────────────────────────────────────────────
# change_pct を adj_close ベースで全件再計算
# ─────────────────────────────────────────────────────────────────────────────

def recompute_change_pct(codes: list[str] | None = None):
    """adj_close の LAG を使って change_pct を全銘柄（or 指定銘柄）再計算。"""
    conn = get_conn()
    cur  = conn.cursor()

    if codes:
        # 指定銘柄のみ（IN句）
        fmt = ",".join(["%s"] * len(codes))
        cur.execute(f"""
            UPDATE daily_prices dp
            JOIN (
                SELECT code, date,
                       LAG(adj_close) OVER (PARTITION BY code ORDER BY date) AS prev_adj
                FROM daily_prices
                WHERE code IN ({fmt})
                  AND adj_close IS NOT NULL
            ) sub ON dp.code = sub.code AND dp.date = sub.date
            SET dp.change_pct = CASE
                WHEN sub.prev_adj IS NULL OR sub.prev_adj = 0 THEN NULL
                WHEN ABS((dp.adj_close - sub.prev_adj) / sub.prev_adj * 100) > 9999 THEN NULL
                ELSE ROUND((dp.adj_close - sub.prev_adj) / sub.prev_adj * 100, 4)
            END
            WHERE sub.prev_adj IS NOT NULL AND sub.prev_adj > 0
        """, codes)
    else:
        # 全銘柄（重いので分割実行 — バッチごとに commit）
        cur.execute("SELECT DISTINCT code FROM daily_prices WHERE adj_close IS NOT NULL")
        all_codes = [r[0] for r in cur.fetchall()]
        BATCH = 200
        for start in range(0, len(all_codes), BATCH):
            batch = all_codes[start:start + BATCH]
            fmt   = ",".join(["%s"] * len(batch))
            cur.execute(f"""
                UPDATE daily_prices dp
                JOIN (
                    SELECT code, date,
                           LAG(adj_close) OVER (PARTITION BY code ORDER BY date) AS prev_adj
                    FROM daily_prices
                    WHERE code IN ({fmt})
                      AND adj_close IS NOT NULL
                ) sub ON dp.code = sub.code AND dp.date = sub.date
                SET dp.change_pct = CASE
                    WHEN sub.prev_adj IS NULL OR sub.prev_adj = 0 THEN NULL
                    WHEN ABS((dp.adj_close - sub.prev_adj) / sub.prev_adj * 100) > 9999 THEN NULL
                    ELSE ROUND((dp.adj_close - sub.prev_adj) / sub.prev_adj * 100, 4)
                END
                WHERE sub.prev_adj IS NOT NULL AND sub.prev_adj > 0
            """, batch)
            conn.commit()
            print(f"    change_pct 更新: {min(start + BATCH, len(all_codes))}/{len(all_codes)} 銘柄")

    conn.commit()
    cur.close()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# メイン: バックフィル実行
# ─────────────────────────────────────────────────────────────────────────────

def run(splits_only: bool = False, max_workers: int = 8):
    ensure_splits_table()

    conn = get_conn()
    cur  = conn.cursor()

    if splits_only:
        # change_pct が -40% 未満の日がある銘柄（分割疑い）
        cur.execute("""
            SELECT DISTINCT code FROM daily_prices
            WHERE change_pct < -40
            ORDER BY code
        """)
        print("モード: 分割疑い銘柄のみ")
    else:
        cur.execute("SELECT code FROM stocks WHERE is_active = TRUE ORDER BY code")
        print("モード: 全銘柄")

    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    print(f"対象: {len(codes)} 銘柄")

    all_price_rows: list[dict] = []
    all_split_rows: list[dict] = []
    failed: list[str]  = []

    def fetch(code4):
        time.sleep(0.05)
        return code4, *_fetch_adj(code4)

    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch, c): c for c in codes}
        for future in as_completed(futures):
            code4, price_rows, split_rows = future.result()
            done += 1
            if not price_rows:
                failed.append(code4)
            else:
                all_price_rows.extend(price_rows)
                all_split_rows.extend(split_rows)
            if done % 500 == 0 or done == len(codes):
                print(f"  取得進捗: {done}/{len(codes)} 銘柄 "
                      f"(価格{len(all_price_rows)}件, 分割{len(all_split_rows)}件)")

    print(f"\n取得完了: 価格{len(all_price_rows)}件, 分割{len(all_split_rows)}件, "
          f"失敗{len(failed)}銘柄")

    if not all_price_rows:
        print("更新データなし。終了。")
        return

    # ── adj_close / adj_factor をDB更新 ─────────────────────────────────────
    print("\nadj_close / adj_factor をUPSERT中...")
    conn = get_conn()
    cur  = conn.cursor()

    # 既存行の adj_close / adj_factor を UPDATE（INSERT はしない）
    BATCH = 500
    updated = 0
    for start in range(0, len(all_price_rows), BATCH):
        batch = all_price_rows[start:start + BATCH]
        for row in batch:
            # adj_factor = adj_close / DBのclose（Yahooのcloseは遡及調整済みのためDB値を使用）
            cur.execute("""
                UPDATE daily_prices
                SET adj_close = %s,
                    adj_factor = CASE WHEN close > 0 THEN ROUND(%s / close, 6) ELSE 1.0 END
                WHERE code = %s AND date = %s
            """, (row["adj_close"], row["adj_close"], row["code"], row["date"]))
        conn.commit()
        updated += len(batch)
        if updated % 50000 == 0 or updated >= len(all_price_rows):
            print(f"  adj更新: {updated}/{len(all_price_rows)}件")

    # ── stock_splits に記録 ──────────────────────────────────────────────────
    if all_split_rows:
        print(f"\nstock_splits に {len(all_split_rows)} 件を記録中...")
        split_db_rows = [(
            r["code"], r["ex_date"], r["split_ratio"], r["source"]
        ) for r in all_split_rows]
        bulk_upsert(cur, "stock_splits",
            ["code", "ex_date", "split_ratio", "source"],
            split_db_rows,
            update_cols=["split_ratio", "source"])
        conn.commit()
        print(f"  登録済み分割イベント:")
        for r in sorted(all_split_rows, key=lambda x: x["ex_date"]):
            print(f"    {r['code']}: {r['ex_date']} ratio={r['split_ratio']}")

    cur.close()
    conn.close()

    # ── change_pct を adj_close ベースで再計算 ───────────────────────────────
    updated_codes = list({r["code"] for r in all_price_rows})
    print(f"\nchange_pct を adj_close ベースで再計算中 ({len(updated_codes)} 銘柄)...")
    recompute_change_pct(updated_codes)
    print("change_pct 再計算完了")

    if failed:
        print(f"\n[警告] データ取得失敗: {len(failed)} 銘柄")
        print("  " + ", ".join(failed[:20]) + ("..." if len(failed) > 20 else ""))

    print("\nバックフィル完了。")
    print("次のステップ: python3 compute_price_stats.py を実行してMA/RSI等を再計算してください。")


if __name__ == "__main__":
    splits_only = "--splits" in sys.argv
    run(splits_only=splits_only)
