"""複数年（時系列）ファンダメンタル指標を算出して fundamental_metrics に保存する。

スクリーニングで「継続性／トラックレコード」を見る条件を使えるようにするための整備。
現行スクリーニングは単年スナップショット中心で、以下が空白だったため追加する:

  売上/純利益CAGR5年 ・ 連続増収/増益/増配年数 ・ ROE/ROA/営業利益率の5年平均 ・
  最高益フラグ ・ ROE 3年差(pt) ・ ROEデュポン分解の主導因子(利益率/回転率/レバレッジ)

【データ源】統一アクセス層 financials_view.annual_performance（EDINET公式=最大15年を正とし、
  未取得の銘柄は Yahoo財務=最大4年で暫定補完）を通す。全画面(業績カード等)と同じ源を見るため
  分断が起きない。Yahoo補完銘柄は年数が短いぶん長期指標(CAGR5年等)は取れる範囲で算出され、
  EDINETバックフィル(financials_edinet.py)が進めば自動的に長期・公式データへ切り替わる。

【継続更新】daily_run.py から日次実行。決算が出れば翌日以降に自動で反映される。
【保守】計算は本ファイルに集約。列を足すときは COLS と _compute() だけ触ればよい。

使い方: python fundamentals_ext.py            # 全銘柄を再計算してupsert
        python fundamentals_ext.py 7203 6501  # 指定銘柄だけ（デバッグ）
"""
from __future__ import annotations

import sys
from datetime import datetime

from config import get_conn, bulk_upsert

RECENT_YEARS = 6          # 直近何年分を読むか（5年CAGR＝5区間のため6行）
AVG_YEARS = 5             # 平均・CAGRの対象年数

# fundamental_metrics の列（updated_at 除く）。ここに足すだけで拡張できる。
COLS = [
    "rev_cagr_5y", "profit_cagr_5y",
    "consec_rev_growth", "consec_profit_growth", "consec_div_growth",
    "roe_5y_avg", "roa_5y_avg", "opm_5y_avg",
    "is_record_profit", "roe_delta_3y", "roe_driver_3y", "n_years",
]


def ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fundamental_metrics (
            code                 VARCHAR(12) PRIMARY KEY,
            rev_cagr_5y          DECIMAL(8,2),   -- 売上高CAGR(直近最大5年, %)
            profit_cagr_5y       DECIMAL(8,2),   -- 純利益CAGR(%)
            consec_rev_growth    INT,            -- 連続増収年数
            consec_profit_growth INT,            -- 連続増益年数(純利益)
            consec_div_growth    INT,            -- 連続増配年数(1株配当)
            roe_5y_avg           DECIMAL(8,2),   -- ROE5年平均(%)
            roa_5y_avg           DECIMAL(8,2),   -- ROA5年平均(%)
            opm_5y_avg           DECIMAL(8,2),   -- 営業利益率5年平均(%)
            is_record_profit     TINYINT,        -- 最新純利益が取得年内で過去最高なら1
            roe_delta_3y         DECIMAL(8,2),   -- 直近ROE - 3期前ROE(pt)
            roe_driver_3y        TINYINT,        -- ROE変化の主導因子 1=利益率 2=回転率 3=レバレッジ
            n_years              INT,            -- 算出に使った年数(透明性)
            updated_at           DATETIME
        )
    """)


# ─── 算出ヘルパ ──────────────────────────────────────────────────────────
def _f(x):
    return float(x) if x is not None else None


def _cagr(new, old, years):
    if new is None or old is None or years <= 0 or old <= 0 or new <= 0:
        return None
    return ((new / old) ** (1.0 / years) - 1.0) * 100.0


def _consec_increase(vals):
    """OLD→NEW 順の系列で、末尾から連続して増加している年数。Noneが出たら打ち切り。"""
    c = 0
    for i in range(len(vals) - 1, 0, -1):
        a, b = vals[i - 1], vals[i]
        if a is None or b is None:
            break
        if b > a:
            c += 1
        else:
            break
    return c


def _avg(vals):
    xs = [v for v in vals if v is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _roe_driver(now, old):
    """DuPont分解: ROE = 純利益率 × 総資産回転率 × 財務レバレッジ。
    3年で各因子の相対変化が最大のものを主導因子(1/2/3)とする。now/old=(ni,rev,ta,na)。"""
    def factors(x):
        ni, rev, ta, na = x
        if None in (ni, rev, ta, na) or rev == 0 or ta == 0 or na == 0:
            return None
        return (ni / rev, rev / ta, ta / na)   # 利益率, 回転率, レバレッジ
    fn, fo = factors(now), factors(old)
    if not fn or not fo:
        return None
    rels = []
    for a, b in zip(fn, fo):
        if b == 0:
            return None
        rels.append(abs((a - b) / abs(b)))
    return max(range(3), key=lambda i: rels[i]) + 1


def _compute(rows: list[dict]) -> dict | None:
    """rows: fiscal_year 昇順の年次dictリスト。指標dictを返す（1行未満はNone）。"""
    if not rows:
        return None
    rev = [r["revenue"] for r in rows]
    net = [r["net_income"] for r in rows]
    dps = [r["dividend_per_share"] for r in rows]
    # roe_official は小数(0.11=11%)で格納されているため % に換算する
    roe = [(r["roe_official"] * 100) if r["roe_official"] is not None else None for r in rows]

    latest = rows[-1]
    span = rows[-(AVG_YEARS + 1):]                       # 最大6行（5区間）
    old = span[0]
    years = latest["fiscal_year"] - old["fiscal_year"]

    m = {
        "rev_cagr_5y": round(_cagr(latest["revenue"], old["revenue"], years), 2)
        if years >= 3 and _cagr(latest["revenue"], old["revenue"], years) is not None else None,
        "profit_cagr_5y": round(_cagr(latest["net_income"], old["net_income"], years), 2)
        if years >= 3 and _cagr(latest["net_income"], old["net_income"], years) is not None else None,
        "consec_rev_growth": _consec_increase(rev),
        "consec_profit_growth": _consec_increase(net),
        "consec_div_growth": _consec_increase(dps),
        "roe_5y_avg": _avg(roe[-AVG_YEARS:]),
        "roa_5y_avg": _avg([(net[i] / rows[i]["total_assets"] * 100)
                            if net[i] is not None and rows[i]["total_assets"] else None
                            for i in range(len(rows))][-AVG_YEARS:]),
        "opm_5y_avg": _avg([(rows[i]["operating_income"] / rev[i] * 100)
                            if rows[i]["operating_income"] is not None and rev[i] else None
                            for i in range(len(rows))][-AVG_YEARS:]),
        "is_record_profit": None, "roe_delta_3y": None, "roe_driver_3y": None,
        "n_years": len(rows),
    }
    # 最高益フラグ（取得年内・最新が過去最高）
    nets = [v for v in net if v is not None]
    if latest["net_income"] is not None and nets:
        m["is_record_profit"] = 1 if latest["net_income"] >= max(nets) else 0
    # ROE 3年差 と 主導因子（3期前が取れれば）
    if len(rows) >= 4:
        r3 = rows[-4]                                    # 3期前
        if latest["roe_official"] is not None and r3["roe_official"] is not None:
            m["roe_delta_3y"] = round((_f(latest["roe_official"]) - _f(r3["roe_official"])) * 100, 2)
        m["roe_driver_3y"] = _roe_driver(
            (latest["net_income"], latest["revenue"], latest["total_assets"], latest["net_assets"]),
            (r3["net_income"], r3["revenue"], r3["total_assets"], r3["net_assets"]))
    return m


# ─── 実行 ────────────────────────────────────────────────────────────────
def run(codes: list[str] | None = None, verbose: bool = True) -> int:
    from financials_view import annual_performance
    conn = get_conn(); cur = conn.cursor()
    ensure_table(cur); conn.commit()

    # 統一アクセス層で業績時系列を取得（EDINET優先＋Yahoo補完）。EDINET未取得の銘柄も
    # Yahoo(最大4年)で暫定的に指標を算出でき、EDINETバックフィルが進めば自動的に長期・公式へ。
    if codes:
        target = list(codes)
    else:
        cur.execute("SELECT code FROM stocks WHERE is_active=1 AND market_id IN (2,3,4)")
        target = [r[0] for r in cur.fetchall()]
    by_code = annual_performance(target, cur=cur)

    now = datetime.now()
    data = []
    for code, rows in by_code.items():
        rows = rows[-RECENT_YEARS:]
        m = _compute(rows)
        if m is None:
            continue
        data.append([code] + [m[c] for c in COLS] + [now])

    bulk_upsert(cur, "fundamental_metrics", ["code"] + COLS + ["updated_at"], data)
    conn.commit(); cur.close(); conn.close()
    if verbose:
        print(f"fundamental_metrics: {len(data)}銘柄を算出・保存")
    return len(data)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    run(codes=args or None)
