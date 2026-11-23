"""業績時系列の統一アクセス層。

年次実績は「銘柄ごとに一つの取得元を選ぶ」のではなく、年度・項目単位で
canonical financials（TDnet > Yahoo）を基準に、同一年度の値が整合するEDINETで欠損を補完する。
これによりEDINET取得済みの過去履歴と、まだ有報が無い最新実績を同時に返す。
"""
from __future__ import annotations

from config import get_conn
from financial_metric_engine import enrich_annual_series
from financials_store import financial_rows_consistent


_VALUE_KEYS = (
    "revenue", "gross_profit", "operating_income", "ordinary_income",
    "profit_before_tax", "net_income", "total_assets", "net_assets",
    "total_debt", "cf_operating", "roe_official", "dividend_per_share", "eps",
)


def _f(v):
    return float(v) if v is not None else None


def _merge_annual_records(edinet_rows: list[dict], fallback_rows: list[dict]) -> dict[str, list[dict]]:
    """既存canonicalを正とし、整合するEDINETで欠損だけを補完する。"""
    merged: dict[tuple[str, int], dict] = {}
    for row in fallback_rows:
        key = (row["code"], int(row["fiscal_year"]))
        item = dict(row)
        item["source"] = "financials"
        item["sources"] = {k: "financials" for k in _VALUE_KEYS if item.get(k) is not None}
        merged[key] = item

    compare_keys = (
        "revenue", "gross_profit", "operating_income", "ordinary_income",
        "net_income", "total_assets", "net_assets", "cf_operating",
    )
    # EDINETだけに存在する古い年度は、同じ銘柄の重複年度がすべて整合した場合だけ採用する。
    # 1年度でも不一致なら、取得APIが連結/単体を取り違えている可能性があるため長期行も出さない。
    validation: dict[str, dict[str, int | bool]] = {}
    for row in edinet_rows:
        key = (row["code"], int(row["fiscal_year"]))
        item = merged.get(key)
        if item is None:
            continue
        existing = [item.get(k) for k in compare_keys]
        candidate = [row.get(k) for k in compare_keys]
        if not any(old is not None and new is not None
                   for old, new in zip(existing, candidate)):
            continue
        state = validation.setdefault(row["code"], {"anchors": 0, "invalid": False})
        state["anchors"] = int(state["anchors"]) + 1
        if not financial_rows_consistent(existing, candidate):
            state["invalid"] = True
    trusted_edinet_codes = {
        code for code, state in validation.items()
        if int(state["anchors"]) > 0 and not state["invalid"]
    }

    for row in edinet_rows:
        key = (row["code"], int(row["fiscal_year"]))
        item = merged.get(key)
        if item is None:
            if row["code"] not in trusted_edinet_codes:
                continue
            item = dict(row)
            item["source"] = "edinet"
            item["sources"] = {k: "edinet" for k in _VALUE_KEYS if item.get(k) is not None}
            merged[key] = item
            continue
        if not financial_rows_consistent(
            [item.get(k) for k in compare_keys],
            [row.get(k) for k in compare_keys],
        ):
            continue
        filled = False
        for field in _VALUE_KEYS:
            if item.get(field) is None and row.get(field) is not None:
                item[field] = row[field]
                item["sources"][field] = "edinet"
                filled = True
        if filled:
            item["source"] = "merged"
        item["accounting_standard"] = row.get("accounting_standard")
        if item.get("period_end") is None:
            item["period_end"] = row.get("period_end")

    result: dict[str, list[dict]] = {}
    for (code, _year), item in sorted(merged.items()):
        standard = (item.get("accounting_standard") or "").upper()
        if standard in {"IFRS", "USGAAP"} and item.get("profit_before_tax") is not None:
            item["secondary_profit"] = item["profit_before_tax"]
            item["secondary_profit_label"] = "税引前利益"
        else:
            item["secondary_profit"] = item.get("ordinary_income")
            item["secondary_profit_label"] = "経常利益"
        result.setdefault(code, []).append(item)
    for code, rows in result.items():
        result[code] = enrich_annual_series(rows)
    return result


def annual_performance(codes: list[str], cur=None) -> dict[str, list[dict]]:
    """年次実績を fiscal_year 昇順で返す。予想は含めない。"""
    if not codes:
        return {}
    own = cur is None
    if own:
        conn = get_conn()
        cur = conn.cursor()
    try:
        ph = ",".join(["%s"] * len(codes))
        cur.execute(f"""
            SELECT code, fiscal_year, period_end, revenue, gross_profit,
                   operating_income, ordinary_income, profit_before_tax, net_income,
                   total_assets, shareholders_equity, net_assets, cf_operating,
                   roe_official, dividend_per_share, eps, accounting_standard
            FROM financials_edinet_annual
            WHERE code IN ({ph})
            ORDER BY code, fiscal_year
        """, (*codes,))
        edinet_rows = []
        for r in cur.fetchall():
            edinet_rows.append({
                "code": r[0], "fiscal_year": r[1], "period_end": r[2],
                "revenue": _f(r[3]), "gross_profit": _f(r[4]),
                "operating_income": _f(r[5]), "ordinary_income": _f(r[6]),
                "profit_before_tax": _f(r[7]), "net_income": _f(r[8]),
                "total_assets": _f(r[9]), "net_assets": _f(r[10]) or _f(r[11]),
                # EDINETの「負債合計」は有利子負債と同義ではないため代用しない。
                "total_debt": None,
                "cf_operating": _f(r[12]), "roe_official": _f(r[13]),
                "dividend_per_share": _f(r[14]), "eps": _f(r[15]),
                "accounting_standard": r[16],
            })

        cur.execute(f"""
            SELECT code, period_end, revenue, gross_profit, operating_income,
                   ordinary_income, net_income, total_assets, total_equity,
                   total_debt, cf_operating
            FROM financials
            WHERE code IN ({ph}) AND period_type='A' AND period_end <= CURDATE()
            ORDER BY code, period_end
        """, (*codes,))
        fallback_rows = []
        for code, pe, rev, gp, op, ordi, net, ta, te, debt, cfo in cur.fetchall():
            net_value, equity_value = _f(net), _f(te)
            fallback_rows.append({
                "code": code, "fiscal_year": int(str(pe)[:4]), "period_end": pe,
                "revenue": _f(rev), "gross_profit": _f(gp),
                "operating_income": _f(op), "ordinary_income": _f(ordi),
                "profit_before_tax": None, "net_income": net_value,
                "total_assets": _f(ta), "net_assets": equity_value,
                "total_debt": _f(debt),
                "cf_operating": _f(cfo),
                # 公式ROEが無い行は「公式値」を捏造しない。
                # enrich_annual_seriesが共通式（平均自己資本）で補完する。
                "roe_official": None,
                "dividend_per_share": None, "eps": None,
                "accounting_standard": None,
            })
        return _merge_annual_records(edinet_rows, fallback_rows)
    finally:
        if own:
            cur.close()
            conn.close()


def annual_forecast(codes: list[str], cur=None) -> dict[str, dict]:
    """各銘柄の直近の今期通期会社予想を返す。"""
    if not codes:
        return {}
    own = cur is None
    if own:
        conn = get_conn()
        cur = conn.cursor()
    try:
        ph = ",".join(["%s"] * len(codes))
        cur.execute(f"""
            SELECT f.code, f.period_end, f.revenue, f.operating_income,
                   f.ordinary_income, f.net_income
            FROM financials f
            JOIN (SELECT code, MIN(period_end) AS pe FROM financials
                  WHERE code IN ({ph}) AND period_type='A' AND period_end > CURDATE()
                  GROUP BY code) x ON x.code=f.code AND x.pe=f.period_end
            WHERE f.period_type='A'
        """, (*codes,))
        out: dict[str, dict] = {}
        for code, pe, rev, op, ordi, net in cur.fetchall():
            out[code] = {
                "fiscal_year": int(str(pe)[:4]), "revenue": _f(rev),
                "operating_income": _f(op), "ordinary_income": _f(ordi),
                "net_income": _f(net),
            }
        return out
    finally:
        if own:
            cur.close()
            conn.close()
