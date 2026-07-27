"""業績時系列の統一アクセス層。

業績は1つの事実だが、データ源は2つある:
  - EDINET(有価証券報告書): 公式・最大15年。長期業績の「正」。edinet_code保有率99%。
  - Yahoo timeseries(financials): 日本株は4年が上限(実測)。EDINET未取得銘柄の暫定表示に使う。

この層が「EDINET優先 → 無ければYahoo補完」を一手に引き受け、全画面(業績カード=perf_grid、
CAGR等の指標算出=fundamentals_ext)がこれを通す。画面ごとにテーブルを直読みしていた従来の
分断(同じ業績が画面により見えたり見えなかったり)を構造的に解消する。

EDINETバックフィル(financials_edinet.py)が全銘柄に行き渡れば、補完は自動的に公式・長期データへ
置き換わる(source が 'yahoo'→'edinet')。それまでの橋渡しが本層のYahoo補完。
"""
from __future__ import annotations

from config import get_conn

# 統一レコードのキー(fundamentals_ext._compute が期待する形に合わせる)
#   fiscal_year, revenue, operating_income, net_income, total_assets, net_assets,
#   roe_official(小数 0.11=11%), dividend_per_share, eps, source
_KEYS = ("fiscal_year", "revenue", "operating_income", "net_income",
         "total_assets", "net_assets", "roe_official", "dividend_per_share",
         "eps", "source")


def _f(v):
    return float(v) if v is not None else None


def annual_performance(codes: list[str], cur=None) -> dict[str, list[dict]]:
    """各銘柄の年次業績を fiscal_year 昇順で返す(EDINET優先・Yahoo補完)。
    返り値 dict[code] = [ {..._KEYS...}, ... ]（該当データが無い銘柄はキー自体を持たない）。
    cur を渡せばそのカーソルを使う(バッチ内での再利用)。省略時は自前で接続する。"""
    if not codes:
        return {}
    own = cur is None
    if own:
        conn = get_conn(); cur = conn.cursor()
    try:
        ph = ",".join(["%s"] * len(codes))
        # ① EDINET年次(公式・最大15年)
        cur.execute(f"""
            SELECT code, fiscal_year, revenue, operating_income, net_income,
                   total_assets, net_assets, roe_official, dividend_per_share, eps
            FROM financials_edinet_annual
            WHERE code IN ({ph})
            ORDER BY code, fiscal_year
        """, (*codes,))
        result: dict[str, list[dict]] = {}
        for r in cur.fetchall():
            result.setdefault(r[0], []).append({
                "fiscal_year": r[1], "revenue": _f(r[2]), "operating_income": _f(r[3]),
                "net_income": _f(r[4]), "total_assets": _f(r[5]), "net_assets": _f(r[6]),
                "roe_official": _f(r[7]), "dividend_per_share": _f(r[8]),
                "eps": _f(r[9]), "source": "edinet",
            })

        # ② Yahoo財務でフォールバック(EDINET未取得の銘柄のみ・実績のみ=未来期末の会社予想は除外)
        missing = [c for c in codes if c not in result]
        if missing:
            mph = ",".join(["%s"] * len(missing))
            cur.execute(f"""
                SELECT code, period_end, revenue, operating_income, net_income,
                       total_assets, total_equity
                FROM financials
                WHERE code IN ({mph}) AND period_type = 'A' AND revenue IS NOT NULL
                  AND period_end <= CURDATE()
                ORDER BY code, period_end
            """, (*missing,))
            yahoo = cur.fetchall()
            cur.execute(f"SELECT code, shares_outstanding FROM stock_fundamentals "
                        f"WHERE code IN ({mph})", (*missing,))
            shares = {r[0]: _f(r[1]) for r in cur.fetchall()}
            for code, pe, rev, op, net, ta, te in yahoo:
                _net, _te, _sh = _f(net), _f(te), shares.get(code)
                result.setdefault(code, []).append({
                    "fiscal_year": int(str(pe)[:4]), "revenue": _f(rev),
                    "operating_income": _f(op), "net_income": _net,
                    "total_assets": _f(ta), "net_assets": _te,
                    # ROE=純利益÷自己資本(EDINETの小数表現に合わせる)、配当はYahooに無くNone
                    "roe_official": (_net / _te) if _net is not None and _te else None,
                    "dividend_per_share": None,
                    "eps": round(_net / _sh, 2) if _net is not None and _sh else None,
                    "source": "yahoo",
                })
        return result
    finally:
        if own:
            cur.close(); conn.close()
