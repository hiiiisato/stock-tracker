"""
スクリーニング指標の「週次スナップショット」を過去に遡って計算し、
price_stats_history テーブルに保存する（バックテスト用）。

- 価格由来指標: compute_price_stats.compute_stock_stats() を共有（日次バッチと完全一致）。
- ファンダ指標: Point-In-Time（PIT）補正。決算は「期末 + LAG_DAYS 日」を公開日とみなし、
  その日以降にのみ使う（先読みバイアスを排除）。
- スナップショット日: 各週の最終取引日。MA200等が揃う SNAPSHOT_START 以降を生成。

実行:
  python compute_stats_history.py --backfill        # 全期間バックフィル（初回一度）
  python compute_stats_history.py --weeks 4         # 直近4週だけ（検証用）
  python compute_stats_history.py                   # 最新週のみ追記（daily_run から呼ぶ）
"""

from __future__ import annotations
import sys
import bisect
from datetime import date, timedelta
from collections import defaultdict

from config import get_conn, bulk_upsert
from compute_price_stats import compute_stock_stats, STAT_COLS, _round

LAG_DAYS = 45                       # 決算期末→公開とみなすまでのラグ（先読み対策）
SNAPSHOT_START = date(2024, 11, 1)  # MA200/スロープが揃う最古週（daily_prices は 2024-01〜）
NIKKEI_SYMBOL = "^N225"

# STAT_COLS に無い、追加のファンダ列（stock_fundamentals 相当を PIT 再構成）
EXTRA_COLS = ["per", "pbr", "roe", "roa", "div_yield", "market_cap",
              "op_margin", "payout_ratio", "debt_to_equity", "beta"]
HISTORY_COLS = ["code", "snapshot_date"] + STAT_COLS + EXTRA_COLS + ["market"]

_FLAG_COLS = {"macd_gc", "break_20d", "break_65d", "cf_positive",
              "gc_5_25", "gc_75_200", "bb_upper", "bb_lower", "break_ytd_high"}


def _ensure_table():
    conn = get_conn(); cur = conn.cursor()
    col_defs = ["code VARCHAR(10) NOT NULL", "snapshot_date DATE NOT NULL"]
    for c in STAT_COLS + EXTRA_COLS:
        if c in _FLAG_COLS:
            col_defs.append(f"{c} TINYINT")
        elif c == "market_cap":
            col_defs.append(f"{c} BIGINT")
        else:
            col_defs.append(f"{c} DECIMAL(18,4)")
    col_defs.append("market VARCHAR(50)")
    col_defs.append("UNIQUE KEY uq_code_date (code, snapshot_date)")
    col_defs.append("KEY idx_date (snapshot_date)")
    cur.execute("CREATE TABLE IF NOT EXISTS price_stats_history (\n  " +
                ",\n  ".join(col_defs) + "\n)")
    conn.commit(); cur.close(); conn.close()


# ─── データ一括ロード ─────────────────────────────────────────────────────────

def _load_all():
    """daily_prices / financials / dividends / shares / 指数 / market を一括ロード。"""
    conn = get_conn(); cur = conn.cursor()

    print("  daily_prices ロード中...")
    cur.execute("""
        SELECT code, date, COALESCE(adj_close, close), high, low, volume, close
        FROM daily_prices
        WHERE close IS NOT NULL AND close > 0 AND date >= '2023-06-01'
        ORDER BY code, date
    """)
    prices = defaultdict(list)
    for code, dt, adj, hi, lo, vol, raw in cur.fetchall():
        prices[code].append((dt, float(adj), float(hi or adj), float(lo or adj),
                             int(vol or 0), float(raw or adj)))

    print("  financials(年次) ロード中...")
    annuals = defaultdict(list)
    cur.execute("""
        SELECT code, period_end, revenue, operating_income, ordinary_income,
               net_income, total_assets, total_equity, total_debt, cf_operating
        FROM financials WHERE period_type='A' ORDER BY code, period_end
    """)
    for r in cur.fetchall():
        annuals[r[0]].append(dict(
            period_end=r[1], avail=r[1] + timedelta(days=LAG_DAYS),
            rev=_f(r[2]), opi=_f(r[3]), ord=_f(r[4]), ni=_f(r[5]),
            ta=_f(r[6]), teq=_f(r[7]), tdebt=_f(r[8]), cfo=_f(r[9])))

    print("  financials(四半期) ロード中...")
    quarters = defaultdict(list)
    cur.execute("""
        SELECT code, period_end, net_income FROM financials
        WHERE period_type='Q' AND net_income IS NOT NULL
        ORDER BY code, period_end
    """)
    for code, pend, ni in cur.fetchall():
        quarters[code].append((pend, pend + timedelta(days=LAG_DAYS), float(ni)))

    print("  dividends ロード中...")
    divs = defaultdict(list)
    cur.execute("SELECT code, ex_date, amount FROM dividends WHERE amount IS NOT NULL ORDER BY code, ex_date")
    for code, exd, amt in cur.fetchall():
        divs[code].append((exd, float(amt)))

    print("  shares/beta（現在値・履歴近似）ロード中...")
    cur.execute("SELECT code, shares_outstanding, beta FROM stock_fundamentals")
    shares = {}
    for code, sh, beta in cur.fetchall():
        shares[code] = (float(sh) if sh else None, float(beta) if beta is not None else None)

    print("  指数(^N225) ロード中...")
    cur.execute("SELECT date, close FROM market_index_prices WHERE symbol=%s AND close IS NOT NULL ORDER BY date", (NIKKEI_SYMBOL,))
    nk = [(r[0], float(r[1])) for r in cur.fetchall()]

    print("  銘柄マスタ(market) ロード中...")
    cur.execute("""
        SELECT s.code, m.name FROM stocks s LEFT JOIN markets m ON s.market_id=m.id
    """)
    market = {r[0]: (r[1] or "") for r in cur.fetchall()}

    cur.close(); conn.close()
    return prices, annuals, quarters, divs, shares, nk, market


def _f(v):
    return float(v) if v is not None else None


# ─── 週次スナップショット日の決定 ────────────────────────────────────────────

def _weekly_snapshot_dates(prices, start, end):
    """全銘柄の取引日を集約し、各 ISO 週の最終取引日を start〜end で返す。"""
    all_dates = set()
    for series in prices.values():
        for row in series:
            if start <= row[0] <= end:
                all_dates.add(row[0])
    if not all_dates:
        return []
    by_week = {}
    for d in all_dates:
        key = (d.isocalendar().year, d.isocalendar().week)
        if key not in by_week or d > by_week[key]:
            by_week[key] = d
    return sorted(by_week.values())


# ─── PIT ファンダ計算 ─────────────────────────────────────────────────────────

def _pit_annuals(alist, D):
    """period_end+45日 <= D を満たす年次を新しい順に返す（[0]=最新, [1]=前期）。"""
    known = [a for a in alist if a["avail"] <= D]
    return known[::-1]  # alist は昇順なので反転して降順


def _pit_fm(known_annuals, shares):
    """_load_financials と同じ計算を PIT の最新/前期年次で再現する。"""
    if not known_annuals:
        return {}
    latest = known_annuals[0]
    prior = known_annuals[1] if len(known_annuals) >= 2 else None
    shr = shares

    def yoy(new, old):
        if new is None or old is None or old == 0:
            return None
        return (new / old - 1) * 100

    rev_l, opi_l, ord_l, ni_l = latest["rev"], latest["opi"], latest["ord"], latest["ni"]
    ta_l, eq_l, dbt_l, cfo_l = latest["ta"], latest["teq"], latest["tdebt"], latest["cfo"]
    rev_p = prior["rev"] if prior else None
    opi_p = prior["opi"] if prior else None
    ord_p = prior["ord"] if prior else None
    ni_p = prior["ni"] if prior else None

    eps_l = (ni_l / shr) if (ni_l is not None and shr and shr > 0) else None
    eps_p = (ni_p / shr) if (ni_p is not None and shr and shr > 0) else None

    roic = None
    cap = (eq_l or 0) + (dbt_l or 0)
    if opi_l is not None and cap > 0:
        roic = opi_l / cap * 100

    equity_ratio = eq_l / ta_l * 100 if (eq_l is not None and ta_l and ta_l > 0) else None
    ord_margin = ord_l / rev_l * 100 if (ord_l is not None and rev_l and rev_l > 0) else None
    cfo_per_share = cfo_l / shr if (cfo_l is not None and shr and shr > 0) else None
    rev_per_share = rev_l / shr if (rev_l is not None and shr and shr > 0) else None

    return {
        "rev_growth": _round(yoy(rev_l, rev_p)),
        "op_growth": _round(yoy(opi_l, opi_p)),
        "ord_growth": _round(yoy(ord_l, ord_p)),
        "eps_growth": _round(yoy(eps_l, eps_p)),
        "roic": _round(roic),
        "cf_positive": 1 if (cfo_l is not None and cfo_l > 0) else 0,
        "equity_ratio": _round(equity_ratio),
        "ord_margin": _round(ord_margin),
        "cfo_per_share": cfo_per_share,
        "rev_per_share": rev_per_share,
    }


def _ttm_eps(qlist, D, shares):
    """D 時点で公開済みの直近4四半期 net_income 合計 / 株数。"""
    known = [q for q in qlist if q[1] <= D]
    if len(known) < 4 or not shares or shares <= 0:
        return None
    ttm_ni = sum(q[2] for q in known[-4:])
    eps = ttm_ni / shares
    return eps


def _trailing_dps(dlist, D):
    """D から遡って12ヶ月の配当合計（年間DPS近似）。"""
    lo = D - timedelta(days=365)
    total = sum(amt for exd, amt in dlist if lo < exd <= D)
    return total if total > 0 else None


def _pit_extra(known_annuals, price_D, ttm_eps, ann_dps, shares):
    """per/pbr/roe/roa/market_cap/op_margin/payout_ratio/debt_to_equity を PIT 計算。"""
    per = pbr = roe = roa = mcap = op_margin = payout = d2e = None
    if price_D and shares and shares > 0:
        mc = price_D * shares
        # 株数データ破損による異常値を除外（実在の時価総額は 1京円未満）
        mcap = int(mc) if 0 < mc < 1e16 else None
    if ttm_eps and ttm_eps > 0 and price_D:
        per = _round(price_D / ttm_eps, 2)
        if per is not None and (per <= 0 or per > 9999):
            per = None
    if known_annuals:
        a = known_annuals[0]
        eq, ta, ni, opi, rev, dbt = a["teq"], a["ta"], a["ni"], a["opi"], a["rev"], a["tdebt"]
        if eq and eq > 0 and shares and shares > 0 and price_D:
            bps = eq / shares
            pbr = _round(price_D / bps, 2) if bps > 0 else None
        if ni is not None and eq and eq > 0:
            roe = _round(ni / eq * 100, 2)
        if ni is not None and ta and ta > 0:
            roa = _round(ni / ta * 100, 4)
        if opi is not None and rev and rev > 0:
            op_margin = _round(opi / rev * 100, 2)
        if dbt is not None and eq and eq > 0:
            d2e = _round(dbt / eq * 100, 2)
    div_yield = None
    if ann_dps and price_D and price_D > 0:
        div_yield = _round(ann_dps / price_D * 100, 2)
    if ann_dps and ttm_eps and ttm_eps > 0:
        payout = _round(ann_dps / ttm_eps * 100, 2)
    return dict(per=per, pbr=pbr, roe=roe, roa=roa, div_yield=div_yield,
                market_cap=mcap, op_margin=op_margin, payout_ratio=payout,
                debt_to_equity=d2e)


# ─── メイン計算 ──────────────────────────────────────────────────────────────

def _nikkei_1m_chg(nk_dates, nk_closes, D):
    hi = bisect.bisect_right(nk_dates, D)
    if hi < 26:
        return None
    return (nk_closes[hi - 1] / nk_closes[hi - 26] - 1) * 100


def _compute_snapshot(D, prices, annuals, quarters, divs, shares, nk, market):
    nk_dates = [x[0] for x in nk]
    nk_closes = [x[1] for x in nk]
    nk_chg = _nikkei_1m_chg(nk_dates, nk_closes, D)

    d_500 = D - timedelta(days=500)
    d_380 = D - timedelta(days=380)
    d_ytd = date(D.year, 1, 1)

    rows = []
    for code, series in prices.items():
        dates = [r[0] for r in series]
        hi = bisect.bisect_right(dates, D)
        if hi == 0:
            continue
        last_date = dates[hi - 1]
        # D 近辺で取引が無い（=D以前に上場廃止/長期停止）銘柄はこの週の対象外
        if (D - last_date).days > 10:
            continue
        lo = bisect.bisect_left(dates, d_500)
        sl = series[lo:hi]
        if len(sl) < 5:
            continue

        # 52週・年初来（スライスから）
        lo52 = bisect.bisect_left(dates, d_380)
        w52_slice = series[lo52:hi]
        adjs = [r[1] for r in w52_slice]
        w52 = (max(adjs), min(adjs)) if adjs else (None, None)
        loytd = bisect.bisect_left(dates, d_ytd)
        ytd_slice = series[loytd:hi]
        adjy = [r[1] for r in ytd_slice]
        ytd = (max(adjy), min(adjy)) if adjy else (None, None)

        sh, beta = shares.get(code, (None, None))
        known_ann = _pit_annuals(annuals.get(code, []), D)
        fm = _pit_fm(known_ann, sh)

        stat = compute_stock_stats(sl, w52, ytd, nk_chg, fm)
        if stat is None:
            continue

        price_D = series[hi - 1][5]  # raw close（price_stats と同じ生終値）
        ttm = _ttm_eps(quarters.get(code, []), D, sh)
        dps = _trailing_dps(divs.get(code, []), D)
        extra = _pit_extra(known_ann, price_D, ttm, dps, sh)

        row = [code, D] + list(stat) + [
            extra["per"], extra["pbr"], extra["roe"], extra["roa"],
            extra["div_yield"], extra["market_cap"], extra["op_margin"],
            extra["payout_ratio"], extra["debt_to_equity"], beta,
            market.get(code, ""),
        ]
        rows.append(tuple(row))
    return rows


def run(backfill=False, weeks=None, latest_only=False):
    _ensure_table()
    print("=== 週次スナップショット計算 ===")
    data = _load_all()
    prices, annuals, quarters, divs, shares, nk, market = data

    end = max((s[-1][0] for s in prices.values() if s), default=date.today())
    if backfill:
        start = SNAPSHOT_START
    elif weeks:
        start = end - timedelta(weeks=weeks)
    else:
        start = end - timedelta(days=10)  # 最新週のみ
    snap_dates = _weekly_snapshot_dates(prices, start, end)
    print(f"  対象スナップショット: {len(snap_dates)}週 ({snap_dates[0] if snap_dates else '-'} 〜 {snap_dates[-1] if snap_dates else '-'})")

    total = 0
    for i, D in enumerate(snap_dates):
        rows = _compute_snapshot(D, prices, annuals, quarters, divs, shares, nk, market)
        if rows:
            conn = get_conn(); cur = conn.cursor()
            bulk_upsert(cur, "price_stats_history", HISTORY_COLS, rows,
                        update_cols=[c for c in HISTORY_COLS if c not in ("code", "snapshot_date")])
            conn.commit(); cur.close(); conn.close()
            total += len(rows)
        print(f"  [{i+1}/{len(snap_dates)}] {D}: {len(rows)}銘柄  (累計 {total})")
    print(f"\n完了: {total} 行保存")
    return total


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--backfill" in args:
        run(backfill=True)
    elif "--weeks" in args:
        n = int(args[args.index("--weeks") + 1])
        run(weeks=n)
    else:
        run(latest_only=True)
