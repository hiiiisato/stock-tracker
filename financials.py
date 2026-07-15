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
    """1銘柄の財務諸表を取得。四半期×4期 + 通期×4期。"""
    ticker = f"{code4}.T"
    modules = "incomeStatementHistory,incomeStatementHistoryQuarterly,balanceSheetHistory"

    for attempt in range(3):
        try:
            r = session.get(SUMMARY_URL.format(ticker=ticker),
                params={"modules": modules, "crumb": crumb},
                timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code in (401, 403):
                return []
            if r.status_code != 200:
                return []

            result = r.json().get("quoteSummary", {}).get("result")
            if not result:
                return []
            data = result[0]
            rows = []

            # 通期損益
            for stmt in data.get("incomeStatementHistory", {}).get("incomeStatementHistory", []):
                row = _parse_income(stmt, code4, "A")
                if row:
                    rows.append(row)

            # 四半期損益
            for stmt in data.get("incomeStatementHistoryQuarterly", {}).get("incomeStatementHistory", []):
                row = _parse_income(stmt, code4, "Q")
                if row:
                    rows.append(row)

            # BS（取れる場合のみ）
            bs_list = data.get("balanceSheetHistory", {}).get("balanceSheetStatements", [])
            for stmt in bs_list:
                end_date = stmt.get("endDate", {}).get("fmt")
                if not end_date:
                    continue
                total_assets = _to_int(stmt.get("totalAssets"))
                total_equity = _to_int(stmt.get("totalStockholderEquity"))
                total_debt = _to_int(stmt.get("longTermDebt") or stmt.get("totalDebt"))
                if any(v is not None for v in [total_assets, total_equity, total_debt]):
                    # 既存の通期行に BS データをマージ（UPSERT で更新）
                    rows.append((code4, end_date, "A",
                        None, None, None, None,
                        total_assets, total_equity, total_debt, None))

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
        # 損益は権威データ=TDnet(financials_tdnet)が正。Yahooは穴埋め専用にし、
        # 既に値がある行は上書きしない（Yahooの欠損=0/None で公式値を壊さない）。
        # total_debt だけは TDnet が持たないため Yahoo が通常更新する。
        fill_only_cols=["revenue", "gross_profit", "operating_income", "net_income",
                        "total_assets", "total_equity", "cf_operating"])
    conn.commit()
    cur.close()
    conn.close()
    print(f"  財務データ: {len(all_rows):,}件 保存")
    return len(all_rows)


if __name__ == "__main__":
    print("=== 財務諸表取得 ===")
    n = fetch_all_financials()
    print(f"  合計 {n:,} 件")
