"""
財務諸表の取得・保存（Yahoo Finance quoteSummary 使用）
四半期・通期の損益・BS・CF推移を保存する。
crumb認証が必要なため、セッションを使い回す。
"""
import time
import requests
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import get_conn, bulk_upsert

SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
# Yahooの新しい fundamentals-timeseries エンドポイント。旧 quoteSummary/incomeStatementHistory は
# Yahooに劣化させられ、日本株で営業利益・売上総利益がNull・売上も一部誤値になる（例6504）。
# timeseries は営業利益・粗利・総資産・自己資本・営業CFまで正確に返る（Yahoo日本版/EDINETと一致）。
TS_URL = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{ticker}"
# timeseries の型 → financials 列（period_type, column）
_TS_MAP = {
    "annualTotalRevenue":    ("A", "revenue"),
    "annualGrossProfit":     ("A", "gross_profit"),
    "annualOperatingIncome": ("A", "operating_income"),
    "annualNetIncome":       ("A", "net_income"),
    "annualTotalAssets":     ("A", "total_assets"),
    "annualStockholdersEquity": ("A", "total_equity"),
    "annualOperatingCashFlow":  ("A", "cf_operating"),
    "quarterlyTotalRevenue":    ("Q", "revenue"),
    "quarterlyGrossProfit":     ("Q", "gross_profit"),
    "quarterlyOperatingIncome": ("Q", "operating_income"),
    "quarterlyNetIncome":       ("Q", "net_income"),
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _get_session_and_crumb():
    """Yahoo Finance のcrumbを取得する。1日1回取得すれば十分。"""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get("https://fc.yahoo.com", timeout=10)
    r = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
    return session, r.text.strip()


def _to_int(val) -> Optional[int]:
    try:
        v = val.get("raw") if isinstance(val, dict) else val
        return int(float(v)) if v is not None and v != "" else None
    except Exception:
        return None


def _zero_to_none(v):
    """Yahooは日本株の売上/粗利/営業益等を欠損時に 0 で返すことがある（例: 9221は
    営業益・粗利が常時0）。通期の主要損益が"ちょうど0"は実データではなく欠損とみなし
    None にする（fill_only と併せて、公式データ=TDnet を Yahoo の0で壊さないため）。"""
    return None if v == 0 else v


def _parse_income(stmt: dict, code: str, period_type: str) -> Optional[Tuple]:
    end_date = stmt.get("endDate", {}).get("fmt")
    if not end_date:
        return None
    return (
        code,
        end_date,
        period_type,
        _zero_to_none(_to_int(stmt.get("totalRevenue"))),
        _zero_to_none(_to_int(stmt.get("grossProfit"))),
        _zero_to_none(_to_int(stmt.get("operatingIncome") or stmt.get("ebit"))),
        _zero_to_none(_to_int(stmt.get("netIncome"))),
        None, None, None, None,  # BS/CF はここでは None
    )


def _fetch_financials(code4: str, session: requests.Session, crumb: str) -> List[Tuple]:
    """1銘柄の財務時系列を Yahoo fundamentals-timeseries から取得（通期＋四半期）。
    返り値の各行: (code, period_end, period_type, revenue, gross_profit, operating_income,
                   net_income, total_assets, total_equity, total_debt, cf_operating)。"""
    ticker = f"{code4}.T"
    types = ",".join(_TS_MAP.keys())
    now = int(time.time())
    for attempt in range(3):
        try:
            r = session.get(TS_URL.format(ticker=ticker),
                params={"type": types, "period1": 1262304000, "period2": now, "crumb": crumb},
                timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code != 200:
                return []
            res = (r.json().get("timeseries") or {}).get("result") or []
            acc: dict = {}          # (period_type, asOfDate) -> {col: value}
            for item in res:
                meta_t = (item.get("meta") or {}).get("type") or []
                t = meta_t[0] if meta_t else None
                if t not in _TS_MAP or t not in item:
                    continue
                ptype, col = _TS_MAP[t]
                for pt in (item.get(t) or []):
                    if not pt:
                        continue
                    d = pt.get("asOfDate")
                    rv = (pt.get("reportedValue") or {}).get("raw")
                    if not d or rv is None:
                        continue
                    acc.setdefault((ptype, d), {})[col] = int(rv)
            rows = []
            for (ptype, d), v in acc.items():
                rows.append((code4, d, ptype,
                    _zero_to_none(v.get("revenue")), _zero_to_none(v.get("gross_profit")),
                    _zero_to_none(v.get("operating_income")), _zero_to_none(v.get("net_income")),
                    v.get("total_assets"), v.get("total_equity"), None, v.get("cf_operating")))
            return rows
        except Exception:
            time.sleep(1)
    return []


def fetch_all_financials(max_workers: int = 5) -> int:
    """全銘柄の財務諸表を取得してUPSERT。週次実行推奨。"""
    session, crumb = _get_session_and_crumb()
    print(f"  crumb取得完了")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT code FROM stocks WHERE is_active=TRUE ORDER BY code")
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    print(f"  対象銘柄: {len(codes)}件 (並列{max_workers}本)")

    all_rows = []

    def fetch(code4):
        time.sleep(0.1)
        return _fetch_financials(code4, session, crumb)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch, c): c for c in codes}
        done = 0
        for future in as_completed(futures):
            rows = future.result()
            if rows:
                all_rows.extend(rows)
            done += 1
            if done % 500 == 0:
                print(f"    {done}/{len(codes)} 完了")

    if not all_rows:
        print("  財務データなし")
        return 0

    conn = get_conn()
    cur = conn.cursor()
    bulk_upsert(cur, "financials",
        ["code", "period_end", "period_type",
         "revenue", "gross_profit", "operating_income", "net_income",
         "total_assets", "total_equity", "total_debt", "cf_operating"],
        all_rows,
        update_cols=["revenue", "gross_profit", "operating_income", "net_income",
                     "total_assets", "total_equity", "total_debt", "cf_operating"],
        # 損益(売上/粗利/営業益/純益)は timeseries が公式値と一致する信頼データなので
        # 通常更新=上書きにする（旧quoteSummory由来の欠損Null・誤値を修正するため）。
        # timeseries は値がある期のみ行を作るので、TDnet 等の既存値をNullで壊すことはない。
        # BS/CF(総資産・自己資本・営業CF)は EDINET(financials_edinet)がより深いので穴埋め専用。
        fill_only_cols=["total_assets", "total_equity", "cf_operating"])
    conn.commit()
    cur.close()
    conn.close()
    print(f"  財務データ: {len(all_rows):,}件 保存")
    return len(all_rows)


if __name__ == "__main__":
    print("=== 財務諸表取得 ===")
    n = fetch_all_financials()
    print(f"  合計 {n:,} 件")
