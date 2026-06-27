"""
銘柄ファンダメンタルズ取得・保存
Yahoo Finance quoteSummary API から EPS/BPS/株数/ROE 等を取得する。

使い方:
  python fundamentals.py              # テーマ登録銘柄全件を更新
  python fundamentals.py 7203 9984    # 指定銘柄のみ更新
"""

import time
import sys
import requests
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import get_conn

SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
MODULES    = "defaultKeyStatistics,summaryDetail,financialData"


def _get_session_and_crumb():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get("https://fc.yahoo.com", timeout=10)
    r = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
    return session, r.text.strip()


def _raw(d: dict, key: str) -> Optional[float]:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("raw")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fetch_one(code: str, session: requests.Session, crumb: str) -> Optional[dict]:
    ticker = f"{code}.T"
    for attempt in range(3):
        try:
            r = session.get(
                SUMMARY_URL.format(ticker=ticker),
                params={"modules": MODULES, "crumb": crumb},
                timeout=20,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt + 2)
                continue
            if r.status_code not in (200,):
                return None
            result = r.json().get("quoteSummary", {}).get("result")
            if not result:
                return None
            data = result[0]
            ks = data.get("defaultKeyStatistics", {})
            sd = data.get("summaryDetail",        {})
            fd = data.get("financialData",         {})
            return {
                "code":               code,
                "shares_outstanding": _raw(ks, "sharesOutstanding"),
                "eps_ttm":            _raw(ks, "trailingEps"),
                "eps_forward":        _raw(ks, "forwardEps"),
                "bps":                _raw(ks, "bookValue"),
                "dividend_rate":      _raw(sd, "dividendRate"),
                "annual_dps":         _raw(sd, "trailingAnnualDividendRate"),
                "payout_ratio":       _raw(sd, "payoutRatio"),
                "roe":                _raw(fd, "returnOnEquity"),
                "roa":                _raw(fd, "returnOnAssets"),
                "debt_to_equity":     _raw(fd, "debtToEquity"),
                "operating_margin":   _raw(fd, "operatingMargins"),
                "profit_margin":      _raw(fd, "profitMargins"),
                "beta":               _raw(ks, "beta"),
                "market_cap":         _raw(sd, "marketCap"),
            }
        except Exception:
            time.sleep(1)
    return None


def _upsert(rows: list):
    if not rows:
        return
    conn = get_conn()
    cur  = conn.cursor()
    sql = """
        INSERT INTO stock_fundamentals
          (code, shares_outstanding, eps_ttm, eps_forward, bps,
           dividend_rate, annual_dps, payout_ratio,
           roe, roa, debt_to_equity, operating_margin, profit_margin,
           beta, market_cap, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON DUPLICATE KEY UPDATE
          shares_outstanding = VALUES(shares_outstanding),
          eps_ttm            = VALUES(eps_ttm),
          eps_forward        = VALUES(eps_forward),
          bps                = VALUES(bps),
          dividend_rate      = VALUES(dividend_rate),
          annual_dps         = VALUES(annual_dps),
          payout_ratio       = VALUES(payout_ratio),
          roe                = VALUES(roe),
          roa                = VALUES(roa),
          debt_to_equity     = VALUES(debt_to_equity),
          operating_margin   = VALUES(operating_margin),
          profit_margin      = VALUES(profit_margin),
          beta               = VALUES(beta),
          market_cap         = VALUES(market_cap),
          updated_at         = NOW()
    """
    params = [(
        r["code"],
        r.get("shares_outstanding"),
        r.get("eps_ttm"),
        r.get("eps_forward"),
        r.get("bps"),
        r.get("dividend_rate"),
        r.get("annual_dps"),
        r.get("payout_ratio"),
        r.get("roe"),
        r.get("roa"),
        r.get("debt_to_equity"),
        r.get("operating_margin"),
        r.get("profit_margin"),
        r.get("beta"),
        r.get("market_cap"),
    ) for r in rows]
    cur.executemany(sql, params)
    conn.commit()
    cur.close()
    conn.close()


def fetch_fundamentals(codes: list, max_workers: int = 4) -> int:
    """指定銘柄のファンダメンタルズを取得して DB に保存。保存件数を返す。"""
    session, crumb = _get_session_and_crumb()
    print(f"  crumb取得完了。対象: {len(codes)} 銘柄")

    results = []

    def fetch(code):
        time.sleep(0.15)
        return _fetch_one(code, session, crumb)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch, c): c for c in codes}
        done = 0
        for future in as_completed(futures):
            row = future.result()
            if row:
                results.append(row)
            done += 1
            if done % 100 == 0:
                print(f"    {done}/{len(codes)} 完了")

    _upsert(results)
    print(f"  ファンダメンタルズ保存: {len(results)}/{len(codes)} 件")
    return len(results)


def recompute_price_metrics(latest_date=None) -> int:
    """
    最新株価から PER・PBR・時価総額・配当利回りを再計算して stock_fundamentals に保存。
    EPS/BPS は週次更新、PER/PBR は日次更新（株価が変わるから）。
    daily_run.py の価格更新後に毎日呼ぶ。
    """
    conn = get_conn()
    cur  = conn.cursor()

    if not latest_date:
        cur.execute("SELECT MAX(date) FROM daily_prices WHERE close IS NOT NULL")
        latest_date = cur.fetchone()[0]

    if not latest_date:
        cur.close(); conn.close()
        return 0

    cur.execute("""
        UPDATE stock_fundamentals sf
        JOIN daily_prices dp ON dp.code = sf.code AND dp.date = %s
        SET
          sf.market_cap = CASE
            WHEN sf.shares_outstanding IS NOT NULL AND sf.shares_outstanding > 0
            THEN ROUND(dp.close * sf.shares_outstanding)
            ELSE NULL
          END,
          sf.per = CASE
            WHEN sf.eps_ttm IS NOT NULL AND sf.eps_ttm > 0
            THEN ROUND(dp.close / sf.eps_ttm, 2)
            ELSE NULL
          END,
          sf.pbr = CASE
            WHEN sf.bps IS NOT NULL AND sf.bps > 0
            THEN ROUND(dp.close / sf.bps, 2)
            ELSE NULL
          END,
          sf.div_yield = CASE
            WHEN sf.annual_dps IS NOT NULL AND sf.annual_dps > 0 AND dp.close > 0
            THEN ROUND(sf.annual_dps / dp.close * 100, 4)
            ELSE NULL
          END
        WHERE dp.close IS NOT NULL AND dp.close > 0
    """, (latest_date,))
    updated = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    print(f"  PER/PBR/時価総額/配当利回り 再計算: {updated:,} 件（{latest_date}）")
    return updated


def fetch_one_on_demand(code: str) -> bool:
    """1銘柄をその場で取得して保存。銘柄ページ表示時の即時取得用。"""
    try:
        session, crumb = _get_session_and_crumb()
        row = _fetch_one(code, session, crumb)
        if row:
            _upsert([row])
            return True
    except Exception as e:
        print(f"  [fundamentals on-demand] {code}: {e}")
    return False


def fetch_all_known(max_workers: int = 4) -> int:
    """過去に取得済みの全銘柄を更新。週次バッチ用。"""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT code FROM stock_fundamentals ORDER BY code")
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    if not codes:
        print("  取得済み銘柄なし")
        return 0
    print(f"  既取得銘柄（全体）: {len(codes)} 件")
    return fetch_fundamentals(codes, max_workers=max_workers)


def fetch_theme_stocks(max_workers: int = 4) -> int:
    """テーマ登録銘柄のファンダメンタルズを更新（後方互換用）。"""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT DISTINCT st.code
        FROM stock_themes st
        JOIN stocks s ON st.code = s.code
        WHERE s.is_active = TRUE
        ORDER BY st.code
    """)
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    print(f"  テーマ登録銘柄: {len(codes)} 件")
    return fetch_fundamentals(codes, max_workers=max_workers)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        codes = sys.argv[1:]
        print(f"=== 個別銘柄ファンダメンタルズ取得: {codes} ===")
        n = fetch_fundamentals(codes)
    elif "--all" in sys.argv:
        print("=== 取得済み全銘柄を更新 ===")
        n = fetch_all_known()
    else:
        print("=== テーマ登録銘柄ファンダメンタルズ取得 ===")
        n = fetch_theme_stocks()
    print(f"完了: {n} 件")
