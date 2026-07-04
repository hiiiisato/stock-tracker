"""
理論株価（はっしゃん式）を全銘柄について計算し、theoretical_values テーブルへ保存する。

参考: 素材/投資判断ツール（株plus版）.xlsx / kabuka.biz 系
モデル:
  資産価値   = BPS × 割引率(自己資本比率)
  事業価値   = EPS × min(ROA, 20%) × 150 × レバレッジ補正
  理論株価   = (資産価値 + 事業価値) × リスク評価率(PBR)
  上限株価   = 資産価値 + 事業価値 × 2
  5年推移    = EPS_n = EPS × (1+経常増益率)^n,  BPS_n = BPS_{n-1} + EPS_{n-1} × 70%

入力データ（すべて既存テーブルから取得。新規取得は不要）:
  stock_fundamentals : eps_forward, bps, shares_outstanding, roa, market_cap
  price_stats        : close(最新終値), equity_ratio(%), ord_growth(経常増益率 %)

daily_run.py から run() が呼ばれ、Render.com cron で日次自動更新される。
"""

from __future__ import annotations
from datetime import date

from config import (
    get_conn, bulk_upsert,
    THEO_DISCOUNT_TABLE, THEO_RISK_TABLE,
    THEO_BUSINESS_MULT, THEO_ROA_CAP, THEO_RETAIN_RATIO, THEO_SIM_YEARS,
    THEO_GROWTH_CAP, THEO_MIN_PER,
    THEO_MKTCAP_MIN, THEO_MKTCAP_MAX, THEO_BIZ_RATIO_MAX, THEO_JUDGE_MULT,
    theo_lookup, theo_leverage,
)


def _round(v, dec=2):
    return round(v, dec) if v is not None else None


def _select_eps(price, eps_forward, eps_ttm):
    """事業価値に使う EPS を選定して返す（正の値。無ければ 0）。

    会社予想を優先するが、予想PERが THEO_MIN_PER 未満（=Yahooのeps_forwardが
    桁違いに大きい等のデータ異常）なら実績EPSにフォールバック。最終的に
    EPS <= 株価/THEO_MIN_PER にキャップし、異常データによる爆発を防ぐ。
    赤字（正のEPSが無い）銘柄は 0 を返し、資産価値のみで評価される。
    """
    f_ok = eps_forward is not None and eps_forward > 0
    t_ok = eps_ttm is not None and eps_ttm > 0

    def plausible(e):
        return e is not None and e > 0 and (price / e) >= THEO_MIN_PER

    if f_ok and t_ok and eps_forward > 2 * eps_ttm:
        # 予想EPSが実績の2倍超 = Yahooの予想が異常に膨張している典型パターン
        # （例: ZOZO 予想171/実績54, Strike 予想355/実績83）。保守的に実績を採用。
        eps = eps_ttm
    elif plausible(eps_forward):
        eps = eps_forward
    elif plausible(eps_ttm):
        eps = eps_ttm
    else:
        # どちらも下限PER未満 or 非正。正の候補があれば大きい方(=PERが低い)を
        # 使いつつ下限でキャップ。無ければ 0（赤字扱い）。
        cands = [e for e in (eps_forward, eps_ttm) if e is not None and e > 0]
        eps = max(cands) if cands else 0.0

    if eps <= 0:
        return 0.0
    return min(eps, price / THEO_MIN_PER)


def compute_one(price, eps_forward, eps_ttm, bps, roa, equity_ratio_frac,
                ord_growth_frac, market_cap=None):
    """1銘柄の理論株価一式を計算して dict で返す。計算不能なら None。

    引数:
      price               : 現在株価（円）
      eps_forward         : EPS 会社予想（円）。優先採用
      eps_ttm             : EPS 実績（円）。予想が異常/欠損時のフォールバック
      bps                 : BPS 実績（円）
      roa                 : ROA（小数, 例 0.0339）
      equity_ratio_frac   : 自己資本比率（小数, 例 0.3612）
      ord_growth_frac     : 経常増益率（小数, 例 -0.0761）。None なら 0 として横ばい
      market_cap          : 時価総額（円, 投資判断用。無くても計算は可）
    """
    if price is None or bps is None or roa is None:
        return None
    if equity_ratio_frac is None or equity_ratio_frac <= 0:
        return None
    if price <= 0 or bps <= 0:
        return None
    # PBR が極端な銘柄は BPS データが信頼できない（例: bps が桁違いで PBR≈0.01）。
    # 資産価値が丸ごと汚染されるため計算対象から除外する。
    pbr = price / bps
    if pbr < 0.1 or pbr > 100:
        return None

    growth = ord_growth_frac if ord_growth_frac is not None else 0.0
    # 異常な増益率は5年複利で爆発するため現実的なレンジにクランプ
    growth = max(-THEO_GROWTH_CAP, min(THEO_GROWTH_CAP, growth))

    # 事業価値に使う EPS を選定（データ異常フォールバック・PER下限キャップ込み）
    eps = _select_eps(price, eps_forward, eps_ttm)

    # ── 現在の理論株価 ──────────────────────────────────────────────
    discount   = theo_lookup(THEO_DISCOUNT_TABLE, equity_ratio_frac)
    lev        = theo_leverage(equity_ratio_frac)
    # 赤字（ROA<=0 または EPS<=0）は事業価値ゼロ＝資産価値のみで評価
    roa_eff    = min(roa, THEO_ROA_CAP) if roa > 0 else 0.0

    asset_value    = bps * discount
    business_value = eps * roa_eff * THEO_BUSINESS_MULT * lev  # eps>=0, roa_eff>=0

    risk_rate = theo_lookup(THEO_RISK_TABLE, pbr)

    theoretical = (asset_value + business_value) * risk_rate
    upper       = asset_value + business_value * 2

    denom = asset_value + business_value
    biz_ratio = (business_value / denom) if denom > 0 else None

    # ── 5年推移シミュレーション ──────────────────────────────────────
    # EPS は経常増益率で複利成長、BPS は毎年 EPS×70% を内部留保で積み増す
    projection = []
    cur_eps = eps
    cur_bps = bps
    for year in range(0, THEO_SIM_YEARS + 1):
        if year > 0:
            cur_bps = cur_bps + cur_eps * THEO_RETAIN_RATIO
            cur_eps = cur_eps * (1.0 + growth)
        # その年の ROA/自己資本比率は概算で据え置き（保守的）
        y_asset = cur_bps * theo_lookup(THEO_DISCOUNT_TABLE, equity_ratio_frac)
        y_biz   = max(cur_eps, 0.0) * roa_eff * THEO_BUSINESS_MULT * lev
        y_pbr   = price / cur_bps if cur_bps > 0 else pbr
        y_theo  = (y_asset + y_biz) * theo_lookup(THEO_RISK_TABLE, y_pbr)
        y_upper = y_asset + y_biz * 2
        projection.append({
            "year": year,
            "eps": _round(cur_eps),
            "bps": _round(cur_bps),
            "theoretical": _round(y_theo),
            "upper": _round(y_upper),
        })

    theo_3y = projection[3]["theoretical"] if len(projection) > 3 else None

    # ── 上昇余地 ────────────────────────────────────────────────────
    upside_pct       = (theoretical / price - 1) * 100
    upper_upside_pct = (upper / price - 1) * 100
    upside_3y_pct    = (theo_3y / price - 1) * 100 if theo_3y else None

    # ── 投資判断○×（Excel由来）─────────────────────────────────────
    judge_mktcap = None
    if market_cap is not None:
        judge_mktcap = (THEO_MKTCAP_MIN <= market_cap <= THEO_MKTCAP_MAX)
    judge_biz   = (biz_ratio is not None and biz_ratio <= THEO_BIZ_RATIO_MAX)
    judge_upper = (upper_upside_pct > 0)
    judge_3y    = (upside_3y_pct is not None and upside_3y_pct > 0)
    # 投資判断倍率 = (3年後理論株価 + 現在理論株価)/2 / 現在株価
    judge_mult  = ((theo_3y + theoretical) / 2) / price if theo_3y else None
    judge_mult_ok = (judge_mult is not None and judge_mult >= THEO_JUDGE_MULT)

    checks = [judge_biz, judge_upper, judge_3y, judge_mult_ok]
    if judge_mktcap is not None:
        checks.append(judge_mktcap)
    pass_all = all(checks)

    return {
        "close": _round(price),
        "eps": _round(eps), "bps": _round(bps),
        "roa": _round(roa, 4), "equity_ratio": _round(equity_ratio_frac * 100),
        "pbr": _round(pbr, 3), "ord_growth": _round(growth * 100),
        "asset_value": _round(asset_value),
        "business_value": _round(business_value),
        "theoretical_price": _round(theoretical),
        "upper_price": _round(upper),
        "biz_ratio": _round(biz_ratio, 4),
        "theo_ratio": _round(theoretical / price, 4),
        "upside_pct": _round(upside_pct),
        "upper_upside_pct": _round(upper_upside_pct),
        "theo_3y": theo_3y,
        "upside_3y_pct": _round(upside_3y_pct),
        "judge_mult": _round(judge_mult, 3),
        "pass_all": 1 if pass_all else 0,
        "market_cap": int(market_cap) if market_cap is not None else None,
        # 各判定の内訳（UI表示用）
        "judge_mktcap": judge_mktcap,
        "judge_biz": judge_biz,
        "judge_upper": judge_upper,
        "judge_3y": judge_3y,
        "judge_mult_ok": judge_mult_ok,
        # 5年推移（グラフ・What-if用）
        "projection": projection,
    }


def _fetch_inputs(code: str):
    """1銘柄の理論株価入力（バッチと同じフォールバック補完済み）を取得して返す。
    戻り値 dict（price/eps_forward/eps_ttm/bps/roa/equity_ratio/ord_growth/
    market_cap/name）。データが無ければ None。"""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        WITH fin_eq AS (
            SELECT code, total_equity, total_assets, net_income,
                   ROW_NUMBER() OVER (PARTITION BY code ORDER BY period_end DESC) AS rn
            FROM financials
            WHERE period_type = 'A'
              AND total_equity IS NOT NULL
              AND total_assets IS NOT NULL AND total_assets > 0
        )
        SELECT ps.close,
               sf.eps_forward,
               sf.eps_ttm,
               COALESCE(sf.bps,
                        CASE WHEN fe.total_equity IS NOT NULL AND sf.shares_outstanding > 0
                             THEN fe.total_equity / sf.shares_outstanding END) AS bps,
               COALESCE(sf.roa,
                        CASE WHEN fe.net_income IS NOT NULL
                             THEN fe.net_income / fe.total_assets END) AS roa,
               COALESCE(ps.equity_ratio, fe.total_equity / fe.total_assets * 100) AS equity_ratio,
               ps.ord_growth,
               sf.market_cap,
               s.name
        FROM stock_fundamentals sf
        JOIN price_stats ps ON sf.code = ps.code
        LEFT JOIN fin_eq fe ON sf.code = fe.code AND fe.rn = 1
        LEFT JOIN stocks s ON sf.code = s.code
        WHERE sf.code = %s
        LIMIT 1
    """, (code,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    close, eps_fwd, eps_ttm, bps, roa, eq_ratio, ord_growth, mktcap, name = row
    return {
        "price": float(close) if close is not None else None,
        "eps_forward": float(eps_fwd) if eps_fwd is not None else None,
        "eps_ttm": float(eps_ttm) if eps_ttm is not None else None,
        "bps": float(bps) if bps is not None else None,
        "roa": float(roa) if roa is not None else None,
        "equity_ratio": float(eq_ratio) if eq_ratio is not None else None,
        "ord_growth": float(ord_growth) if ord_growth is not None else None,
        "market_cap": float(mktcap) if mktcap is not None else None,
        "name": name or "",
    }


def compute_for_code(code: str, overrides: dict | None = None):
    """1銘柄の理論株価一式を計算して返す（銘柄名付き）。
    overrides で eps/roa/equity_ratio/ord_growth/price を差し替え可能（What-if用）。
    計算不能なら None。"""
    inp = _fetch_inputs(code)
    if inp is None:
        return None
    o = overrides or {}

    def pick(key, default):
        v = o.get(key)
        return v if v is not None else default

    # What-if: EPS は単一値で上書きできるよう eps_forward/eps_ttm 両方に反映
    eps_override = o.get("eps")
    eps_fwd = eps_override if eps_override is not None else inp["eps_forward"]
    eps_ttm = eps_override if eps_override is not None else inp["eps_ttm"]

    eq_ratio = pick("equity_ratio", inp["equity_ratio"])
    roa      = pick("roa", inp["roa"])
    ord_g    = pick("ord_growth", inp["ord_growth"])
    price    = pick("price", inp["price"])

    res = compute_one(
        price=price,
        eps_forward=eps_fwd,
        eps_ttm=eps_ttm,
        bps=pick("bps", inp["bps"]),
        roa=(roa / 100.0) if (o.get("roa") is not None) else roa,
        equity_ratio_frac=(eq_ratio / 100.0) if eq_ratio is not None else None,
        ord_growth_frac=(ord_g / 100.0) if ord_g is not None else None,
        market_cap=inp["market_cap"],
    )
    if res is None:
        return None
    res["code"] = code
    res["name"] = inp["name"]
    return res


def _ensure_table():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS theoretical_values (
            code               VARCHAR(10) PRIMARY KEY,
            updated_at         DATETIME DEFAULT NOW() ON UPDATE NOW(),
            close              DECIMAL(16,2),
            eps                DECIMAL(16,2),
            bps                DECIMAL(16,2),
            roa                DECIMAL(8,4),
            equity_ratio       DECIMAL(8,2),
            pbr                DECIMAL(12,3),
            ord_growth         DECIMAL(8,2),
            asset_value        DECIMAL(16,2),
            business_value     DECIMAL(16,2),
            theoretical_price  DECIMAL(16,2),
            upper_price        DECIMAL(16,2),
            biz_ratio          DECIMAL(8,4),
            theo_ratio         DECIMAL(14,4),
            upside_pct         DECIMAL(14,2),
            upper_upside_pct   DECIMAL(14,2),
            theo_3y            DECIMAL(16,2),
            upside_3y_pct      DECIMAL(14,2),
            judge_mult         DECIMAL(12,3),
            pass_all           TINYINT DEFAULT 0,
            market_cap         BIGINT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def run() -> int:
    _ensure_table()

    # 入力データを既存テーブルから収集。欠損は以下の順でフォールバック補完する:
    #   EPS予想/実績  = eps_forward / eps_ttm（両方 compute_one に渡し内部で選定）
    #   BPS          = stock_fundamentals.bps → 最新期 total_equity / 発行済株式数
    #   ROA          = stock_fundamentals.roa → 最新期 net_income / total_assets
    #   自己資本比率  = price_stats.equity_ratio → 最新期 total_equity / total_assets
    # fin_eq は「total_equity/total_assets が非NULLな最新の通期」を採用（最新期が
    # 未確定でNULLでも取りこぼさない）。これでカバレッジが約22%→87%に向上。
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        WITH fin_eq AS (
            SELECT code, total_equity, total_assets, net_income,
                   ROW_NUMBER() OVER (PARTITION BY code ORDER BY period_end DESC) AS rn
            FROM financials
            WHERE period_type = 'A'
              AND total_equity IS NOT NULL
              AND total_assets IS NOT NULL AND total_assets > 0
        )
        SELECT sf.code,
               ps.close,
               sf.eps_forward,
               sf.eps_ttm,
               COALESCE(sf.bps,
                        CASE WHEN fe.total_equity IS NOT NULL AND sf.shares_outstanding > 0
                             THEN fe.total_equity / sf.shares_outstanding END) AS bps,
               COALESCE(sf.roa,
                        CASE WHEN fe.net_income IS NOT NULL
                             THEN fe.net_income / fe.total_assets END) AS roa,
               COALESCE(ps.equity_ratio, fe.total_equity / fe.total_assets * 100) AS equity_ratio,
               ps.ord_growth,
               sf.market_cap
        FROM stock_fundamentals sf
        JOIN price_stats ps ON sf.code = ps.code
        LEFT JOIN fin_eq fe ON sf.code = fe.code AND fe.rn = 1
    """)
    src = cur.fetchall()
    cur.close()
    conn.close()

    cols = [
        "code", "close", "eps", "bps", "roa", "equity_ratio", "pbr", "ord_growth",
        "asset_value", "business_value", "theoretical_price", "upper_price",
        "biz_ratio", "theo_ratio", "upside_pct", "upper_upside_pct",
        "theo_3y", "upside_3y_pct", "judge_mult", "pass_all", "market_cap",
    ]

    rows = []
    for code, close, eps_fwd, eps_ttm, bps, roa, eq_ratio, ord_growth, mktcap in src:
        r = compute_one(
            price=float(close) if close is not None else None,
            eps_forward=float(eps_fwd) if eps_fwd is not None else None,
            eps_ttm=float(eps_ttm) if eps_ttm is not None else None,
            bps=float(bps) if bps is not None else None,
            roa=float(roa) if roa is not None else None,
            equity_ratio_frac=(float(eq_ratio) / 100.0) if eq_ratio is not None else None,
            ord_growth_frac=(float(ord_growth) / 100.0) if ord_growth is not None else None,
            market_cap=float(mktcap) if mktcap is not None else None,
        )
        if r is None:
            continue
        rows.append((
            code, r["close"], r["eps"], r["bps"], r["roa"], r["equity_ratio"],
            r["pbr"], r["ord_growth"], r["asset_value"], r["business_value"],
            r["theoretical_price"], r["upper_price"], r["biz_ratio"], r["theo_ratio"],
            r["upside_pct"], r["upper_upside_pct"], r["theo_3y"], r["upside_3y_pct"],
            r["judge_mult"], r["pass_all"], r["market_cap"],
        ))

    if rows:
        computed_codes = [r[0] for r in rows]
        conn = get_conn()
        cur  = conn.cursor()
        bulk_upsert(cur, "theoretical_values", cols, rows,
                    update_cols=[c for c in cols if c != "code"])
        # 今回の計算対象から外れた銘柄（データ欠損・PBR異常化など）の古い値を削除
        ph = ",".join(["%s"] * len(computed_codes))
        cur.execute(f"DELETE FROM theoretical_values WHERE code NOT IN ({ph})",
                    computed_codes)
        conn.commit()
        cur.close()
        conn.close()

    print(f"  theoretical_values: {len(rows)} 銘柄更新（対象 {len(src)} 件中）")
    return len(rows)


if __name__ == "__main__":
    n = run()
    print(f"完了: {n} 銘柄")
