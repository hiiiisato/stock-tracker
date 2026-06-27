"""
過去の財務指標（PER・PBR・ROE・ROA）を四半期/年次ごとに計算して保存する。

テーブル: stock_metrics_history
- code, date, per, roe, roa, market_cap, price

計算方法:
  PER  = 株価(期末日最寄り) / TTM_EPS
       TTM_EPS = (直近4Q net_income 合計) / shares_outstanding
  ROE  = annual net_income / total_equity  (年次のみ)
  ROA  = annual net_income / total_assets  (年次のみ)

実行方法:
  python3 compute_metrics_history.py               # 全銘柄
  python3 compute_metrics_history.py 7203 6758     # 指定銘柄
"""

import sys
from datetime import date, timedelta
from config import get_conn, bulk_upsert


# ─── テーブル作成 ───────────────────────────────────────────────────────────────

def _ensure_table():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_metrics_history (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            code       VARCHAR(10)    NOT NULL,
            date       DATE           NOT NULL,
            price      DECIMAL(14,2),
            market_cap BIGINT,
            per        DECIMAL(10,2),
            pbr        DECIMAL(10,2),
            roe        DECIMAL(8,4),
            roa        DECIMAL(8,4),
            ttm_eps    DECIMAL(14,2),
            created_at DATETIME DEFAULT NOW(),
            updated_at DATETIME DEFAULT NOW() ON UPDATE NOW(),
            UNIQUE KEY uq_code_date (code, date)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("  [DB] stock_metrics_history テーブル確認済み")


# ─── 計算ロジック ─────────────────────────────────────────────────────────────────

def _compute_for_codes(codes: list):
    conn = get_conn()
    cur  = conn.cursor()

    ph = ",".join(["%s"] * len(codes))

    # 株数（現在スナップショット）
    cur.execute(f"""
        SELECT code, shares_outstanding, bps
        FROM stock_fundamentals WHERE code IN ({ph})
    """, codes)
    shares_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    # 四半期 net_income（直近8Q）
    cur.execute(f"""
        SELECT code, period_end, net_income
        FROM financials
        WHERE code IN ({ph}) AND period_type = 'Q'
          AND net_income IS NOT NULL AND net_income != 0
        ORDER BY code, period_end
    """, codes)
    q_map: dict[str, list] = {}
    for code, pend, ni in cur.fetchall():
        q_map.setdefault(code, []).append((pend, ni))

    # 年次（net_income, total_equity, total_assets）
    cur.execute(f"""
        SELECT code, period_end, net_income, total_equity, total_assets
        FROM financials
        WHERE code IN ({ph}) AND period_type = 'A'
          AND net_income IS NOT NULL AND net_income != 0
        ORDER BY code, period_end
    """, codes)
    a_map: dict[str, list] = {}
    for code, pend, ni, eq, assets in cur.fetchall():
        a_map.setdefault(code, []).append((pend, ni, eq, assets))

    # 株価履歴（period_end 前後の最寄り日）
    cur.execute(f"""
        SELECT code, date, close
        FROM daily_prices
        WHERE code IN ({ph}) AND close IS NOT NULL
        ORDER BY code, date
    """, codes)
    price_rows: dict[str, list] = {}
    for code, dt, close in cur.fetchall():
        price_rows.setdefault(code, []).append((dt, float(close)))

    cur.close()
    conn.close()

    def nearest_price(code: str, target_date):
        rows = price_rows.get(code, [])
        if not rows:
            return None
        best = min(rows, key=lambda r: abs((r[0] - target_date).days))
        if abs((best[0] - target_date).days) > 10:
            return None
        return best[1]  # close

    rows_to_save = []

    for code in codes:
        shares, bps = shares_map.get(code, (None, None))

        # ── 四半期 PER ──────────────────────────────────────────
        quarters = q_map.get(code, [])
        for i, (pend, _) in enumerate(quarters):
            ttm_quarters = quarters[max(0, i-3): i+1]
            if len(ttm_quarters) < 4:
                continue
            ttm_ni = sum(q[1] for q in ttm_quarters)
            if not shares or shares <= 0:
                continue
            ttm_eps = ttm_ni / shares
            if ttm_eps <= 0:
                continue
            price = nearest_price(code, pend)
            if not price:
                continue
            per = round(price / ttm_eps, 2)
            if per <= 0 or per > 9999:
                continue
            mc = int(price * shares) if shares else None
            rows_to_save.append((
                code, str(pend), price, mc,
                per, None, None, None, round(ttm_eps, 2)
            ))

        # ── 年次 ROE / ROA / PBR ─────────────────────────────────
        for pend, ni, eq, assets in a_map.get(code, []):
            roe = round(ni / eq,     4) if eq     and eq     > 0 else None
            roa = round(ni / assets, 4) if assets and assets > 0 else None
            if roe is None and roa is None:
                continue
            price = nearest_price(code, pend)
            mc    = int(price * shares) if (price and shares) else None
            # PBR = price / BPS, BPS = total_equity / shares
            bps_hist = round(eq / shares, 2) if (eq and shares and shares > 0) else None
            pbr = round(price / bps_hist, 2) if (price and bps_hist and bps_hist > 0) else None
            rows_to_save.append((
                code, str(pend), price, mc,
                None, pbr, roe, roa, None
            ))

    return rows_to_save


# ─── メイン ──────────────────────────────────────────────────────────────────────

def run(target_codes=None):
    _ensure_table()

    conn = get_conn()
    cur  = conn.cursor()
    if target_codes:
        ph = ",".join(["%s"] * len(target_codes))
        cur.execute(f"SELECT code FROM stocks WHERE code IN ({ph})", target_codes)
    else:
        cur.execute("SELECT code FROM stocks WHERE is_active=TRUE ORDER BY code")
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    print(f"=== 過去指標計算 ({len(codes)} 銘柄) ===")

    BATCH = 200
    saved = 0
    for i in range(0, len(codes), BATCH):
        batch_codes = codes[i: i+BATCH]
        rows = _compute_for_codes(batch_codes)
        if rows:
            conn = get_conn()
            cur  = conn.cursor()
            bulk_upsert(cur, "stock_metrics_history",
                ["code", "date", "price", "market_cap",
                 "per", "pbr", "roe", "roa", "ttm_eps"],
                rows,
                update_cols=["price", "market_cap",
                             "per", "pbr", "roe", "roa", "ttm_eps"])
            conn.commit()
            cur.close()
            conn.close()
            saved += len(rows)
        pct = min(i + BATCH, len(codes))
        print(f"  [{pct}/{len(codes)}]  保存累計: {saved} 件")

    print(f"\n完了: {saved} 件保存")


if __name__ == "__main__":
    args         = sys.argv[1:]
    target_codes = args or None
    run(target_codes=target_codes)
