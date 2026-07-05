"""
理論株価（はっしゃん式）を全銘柄について計算し、theoretical_values テーブルへ保存する。

参考: 素材/投資判断ツール（株plus版）.xlsx / kabuka.biz 系（はっしゃん式）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 モデルの考え方（透明化のため明記）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 理論株価 = (資産価値 + 事業価値) × リスク評価率
   ● 資産価値 = BPS × 割引率(自己資本比率)
       会社を今解散したときの1株価値。財務が厚い(自己資本比率が高い)ほど
       割引率を高くして資産を高く評価する（0%→0.50 … 80%→0.80）。
   ● 事業価値 = EPS × min(ROA,20%) × 150 × レバレッジ補正
       稼ぐ力の価値。ROA(資本効率)が高いほど1株利益を高PERで評価する。
       「×150」は はっしゃん式が定める妥当PER相当の係数。ROAは20%で頭打ち。
       レバレッジ補正 = median(1, 1.5, (1/自己資本比率)/3 + 1/2)。
   ● リスク評価率 = 極端に低PBR(解散価値を大きく下回る)銘柄を減点する掛け目。
       実在銘柄はほぼ 1.0（PBR≧0.5 で無減点）。
   ● 上限株価 = 資産価値 + 事業価値 × 2  … 強気シナリオの天井。
   ● ROA は EPS/BPS×自己資本比率 で自己整合的に算出（＝ROE×自己資本比率）。

 5年推移（はっしゃん式に忠実 + 成長率を高度化）:
   毎年、内部留保でBPSが増え、それに伴い ROE・自己資本比率・ROA・レバレッジを
   再計算する（BPSが膨らむほどROEは自然低下＝保守的）。
     BPS_n  = BPS_{n-1} + EPS_{n-1} × 70%        （利益の70%を内部留保）
     EPS_n  = EPS_{n-1} × (1 + 成長率_n)
     ROE_n  = EPS_n / BPS_n
     自己資本比率_n = ROE等から再計算（急変を平均で緩和）
     ROA_n  = ROE_n × 自己資本比率_n
   成長率_n は「1年目=会社予想 → 以降は過去3年CAGRへ逓減」ブレンド:
     成長率_n = 長期(過去3年CAGR) + (1年目(会社予想) - 長期) × decay^(n-1)
   直近は会社ガイダンスを重視し、遠い将来は長期平均に回帰させる（永久高成長を仮定しない）。

 入力データ（すべて既存テーブルから取得。新規取得は不要）:
   stock_fundamentals : eps_forward, eps_ttm, bps, shares_outstanding, market_cap
   price_stats        : close(最新終値), equity_ratio(%)
   financials         : ordinary_income 年次系列（過去3年CAGR用）
   financials_forecast: ordinary_income 会社予想（1年目成長率用）

daily_run.py から run() が呼ばれ、Render.com cron で日次自動更新される。
"""

from __future__ import annotations

from collections import defaultdict

from config import (
    get_conn, bulk_upsert,
    THEO_DISCOUNT_TABLE, THEO_RISK_TABLE,
    THEO_BUSINESS_MULT, THEO_ROA_CAP, THEO_RETAIN_RATIO, THEO_SIM_YEARS,
    THEO_GROWTH_CAP, THEO_GROWTH_DECAY, THEO_LONGRUN_CAP, THEO_MIN_PER,
    THEO_MKTCAP_MIN, THEO_MKTCAP_MAX, THEO_BIZ_RATIO_MAX, THEO_JUDGE_MULT,
    theo_lookup, theo_leverage,
)


def _round(v, dec=2):
    return round(v, dec) if v is not None else None


def _clamp_growth(g):
    return max(-THEO_GROWTH_CAP, min(THEO_GROWTH_CAP, g))


def growth_schedule(fwd_growth, cagr_growth, ord_growth, n_years):
    """将来 n_years 年分の成長率リストと、その根拠ラベルを返す。

    ブレンド: 1年目=会社予想(fwd) → 以降は過去3年CAGR(cagr)へ decay^(n-1) で逓減。
      g_n = 長期 + (1年目 - 長期) × decay^(n-1)
    データ欠損時のフォールバック:
      予想も過去も有 → ブレンド('blend')
      予想のみ有     → 予想を一定('forecast')
      過去のみ有     → 過去3年CAGRを一定('cagr3y')
      いずれも無     → 単年YoY(ord_growth)を一定('yoy') / それも無ければ 0('none')
    """
    def ok(x):
        return x is not None
    if ok(fwd_growth) and ok(cagr_growth):
        y1, lr, basis = fwd_growth, cagr_growth, "blend"
    elif ok(fwd_growth):
        y1 = lr = fwd_growth; basis = "forecast"
    elif ok(cagr_growth):
        y1 = lr = cagr_growth; basis = "cagr3y"
    elif ok(ord_growth):
        y1 = lr = ord_growth; basis = "yoy"
    else:
        y1 = lr = 0.0; basis = "none"
    # 1年目(会社予想)は ±GROWTH_CAP、長期は持続可能性を考え ±LONGRUN_CAP に抑える
    y1 = _clamp_growth(y1)
    lr = max(-THEO_LONGRUN_CAP, min(THEO_LONGRUN_CAP, lr))
    sched = []
    for k in range(1, n_years + 1):
        g = lr + (y1 - lr) * (THEO_GROWTH_DECAY ** (k - 1))
        sched.append(_clamp_growth(g))
    return sched, basis, y1, lr


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


def _theo_value(bps_n, eps_n, roa_n, eq_frac_n, price):
    """ある年の (資産価値, 事業価値, 理論株価, 上限株価) を返す（はっしゃん式）。"""
    disc = theo_lookup(THEO_DISCOUNT_TABLE, eq_frac_n)
    lev  = theo_leverage(eq_frac_n)
    roa_eff = min(roa_n, THEO_ROA_CAP) if roa_n > 0 else 0.0
    asset = bps_n * disc
    biz   = max(eps_n, 0.0) * roa_eff * THEO_BUSINESS_MULT * lev
    pbr_n = price / bps_n if bps_n > 0 else 0.0
    risk  = theo_lookup(THEO_RISK_TABLE, pbr_n)
    theo  = (asset + biz) * risk
    upper = asset + biz * 2
    return asset, biz, theo, upper


def compute_one(price, eps_forward, eps_ttm, bps, equity_ratio_frac,
                fwd_growth_frac=None, cagr_growth_frac=None,
                ord_growth_frac=None, roa_override=None, market_cap=None):
    """1銘柄の理論株価一式を計算して dict で返す。計算不能なら None。

    引数:
      price               : 現在株価（円）
      eps_forward         : EPS 会社予想（円）。優先採用
      eps_ttm             : EPS 実績（円）。予想が異常/欠損時のフォールバック
      bps                 : BPS 実績（円）
      equity_ratio_frac   : 自己資本比率（小数, 例 0.3612）
      fwd_growth_frac     : 会社予想の経常増益率（小数）。5年推移の1年目に使う
      cagr_growth_frac    : 過去3年の経常益CAGR（小数）。長期成長率として使う
      ord_growth_frac     : 単年YoY経常増益率（小数）。上記が無い時のフォールバック、
                            および What-if で成長率を一定値で上書きする場合に使用
      roa_override        : ROA（小数）を明示指定する場合（What-if用）。通常は
                            EPS/BPS×自己資本比率 で自己整合的に算出する
      market_cap          : 時価総額（円, 投資判断用。無くても計算は可）
    """
    if price is None or bps is None:
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

    # 事業価値に使う EPS を選定（データ異常フォールバック・PER下限キャップ込み）
    eps = _select_eps(price, eps_forward, eps_ttm)

    # ROA は EPS/BPS×自己資本比率 で自己整合的に算出（はっしゃん式 E2=F2/G2*D2）。
    # What-if で明示指定された場合のみそれを使う。
    if roa_override is not None:
        roa = roa_override
    else:
        roa = (eps / bps) * equity_ratio_frac if bps > 0 else 0.0

    # ── 現在（year 0）の理論株価 ────────────────────────────────────
    asset_value, business_value, theoretical, upper = _theo_value(
        bps, eps, roa, equity_ratio_frac, price)
    denom = asset_value + business_value
    biz_ratio = (business_value / denom) if denom > 0 else None

    # ── 成長率スケジュール（1年目=会社予想 → 過去3年CAGRへ逓減）────────
    # What-if で ord_growth を明示指定した場合はその値を一定で使う（fwd/cagrを無効化）。
    if ord_growth_frac is not None and fwd_growth_frac is None and cagr_growth_frac is None:
        sched, growth_basis, g_y1, g_lr = growth_schedule(
            None, None, ord_growth_frac, THEO_SIM_YEARS)
    else:
        sched, growth_basis, g_y1, g_lr = growth_schedule(
            fwd_growth_frac, cagr_growth_frac, ord_growth_frac, THEO_SIM_YEARS)

    # ── 5年推移（はっしゃん式に忠実: 毎年 ROE/自己資本比率/ROA を再計算）──
    projection = [{
        "year": 0, "eps": _round(eps), "bps": _round(bps),
        "theoretical": _round(theoretical), "upper": _round(upper),
        "growth": 0.0,
    }]
    cur_eps, cur_bps, cur_eq = eps, bps, equity_ratio_frac
    for year in range(1, THEO_SIM_YEARS + 1):
        g = sched[year - 1]
        prev_eps, prev_bps, prev_eq = cur_eps, cur_bps, cur_eq
        # BPS は前年EPSの70%を内部留保、EPSは成長率で伸ばす
        cur_bps = prev_bps + prev_eps * THEO_RETAIN_RATIO
        cur_eps = prev_eps * (1.0 + g)
        # 自己資本比率を再計算（新BPS / 新総資産/株）。前年比率と平均して急変を緩和。
        prev_assets_ps = (prev_bps / prev_eq) if prev_eq > 0 else prev_bps
        new_assets_ps  = prev_assets_ps + prev_eps * THEO_RETAIN_RATIO
        implied_eq = (cur_bps / new_assets_ps) if new_assets_ps > 0 else prev_eq
        cur_eq = max(0.01, min(1.0, (implied_eq + prev_eq) / 2.0))
        # ROE→ROA を再計算
        roe_n = (cur_eps / cur_bps) if cur_bps > 0 else 0.0
        roa_n = roe_n * cur_eq
        y_asset, y_biz, y_theo, y_upper = _theo_value(
            cur_bps, cur_eps, roa_n, cur_eq, price)
        projection.append({
            "year": year, "eps": _round(cur_eps), "bps": _round(cur_bps),
            "theoretical": _round(y_theo), "upper": _round(y_upper),
            "growth": _round(g * 100, 1),
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

    # 想定フェアPER（透明化のため: 理論株価 ÷ 採用EPS）
    fair_per = _round(theoretical / eps, 1) if eps and eps > 0 else None

    return {
        "close": _round(price),
        "eps": _round(eps), "bps": _round(bps),
        "roa": _round(roa, 4), "equity_ratio": _round(equity_ratio_frac * 100),
        "pbr": _round(pbr, 3),
        # ord_growth = 1年目の採用成長率（%）。後方互換のためこの名前を維持。
        "ord_growth": _round(g_y1 * 100),
        # 成長率の内訳（透明化）
        "growth_basis": growth_basis,       # 'blend'/'forecast'/'cagr3y'/'yoy'/'none'
        "growth_y1": _round(g_y1 * 100),     # 1年目成長率（会社予想など）%
        "growth_lr": _round(g_lr * 100),     # 長期成長率（過去3年CAGRなど）%
        "fair_per": fair_per,                # 想定フェアPER
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


def _cagr(latest, past, years):
    """past → latest への years 年 CAGR（小数）。両方正でないと None。"""
    if latest is None or past is None or past <= 0 or latest <= 0 or years <= 0:
        return None
    return (latest / past) ** (1.0 / years) - 1.0


def _growth_from_series(actual_desc, future_oi, forecast_fallback):
    """実績系列(新しい順)と未来期経常益から (fwd_growth, cagr_growth) を返す。
      latest = 実績の最新, past3 = 3年前実績, forecast = 未来期(会社予想) or 予想テーブル
      fwd_growth  = forecast / latest - 1
      cagr_growth = (latest / past3)^(1/3) - 1
    経常益が正のときのみ算出。"""
    latest = actual_desc[0] if actual_desc else None
    past3  = actual_desc[3] if len(actual_desc) >= 4 else None
    cagr = _cagr(latest, past3, 3)
    fcst = future_oi if future_oi is not None else forecast_fallback
    fwd = None
    if fcst is not None and latest is not None and latest > 0 and fcst > 0:
        fwd = fcst / latest - 1.0
    return fwd, cagr


def _load_growth_all():
    """全銘柄の (fwd_growth, cagr_growth) を {code: (fwd, cagr)} で返す。
    会社予想は「financials に未来日付で入っている翌期の経常益」を第一ソースとし
    （多くの銘柄でここに来期ガイダンスが入る）、無ければ financials_forecast を使う。"""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT code, period_end, ordinary_income
        FROM financials
        WHERE period_type='A' AND ordinary_income IS NOT NULL
        ORDER BY code, period_end
    """)
    from datetime import date as _d
    today = _d.today()
    actuals = defaultdict(list)   # 昇順
    future  = {}                  # code -> 最も近い未来期の経常益（来期予想）
    for code, pend, oi in cur.fetchall():
        if pend <= today:
            actuals[code].append(float(oi))
        elif code not in future:      # 昇順なので最初の未来期＝最も近い来期
            future[code] = float(oi)
    # financials_forecast フォールバック
    cur.execute("""
        SELECT f.code, f.ordinary_income
        FROM financials_forecast f
        JOIN (SELECT code, MAX(announced_at) AS ma FROM financials_forecast
              WHERE period_type='A' AND ordinary_income IS NOT NULL GROUP BY code) t
          ON f.code=t.code AND f.announced_at=t.ma
        WHERE f.period_type='A' AND f.ordinary_income IS NOT NULL
    """)
    forecast_fb = {code: float(oi) for code, oi in cur.fetchall()}
    cur.close(); conn.close()

    out = {}
    codes = set(actuals) | set(future) | set(forecast_fb)
    for code in codes:
        desc = list(reversed(actuals.get(code, [])))   # 新しい順
        out[code] = _growth_from_series(desc, future.get(code), forecast_fb.get(code))
    return out


def _fetch_inputs(code: str):
    """1銘柄の理論株価入力（バッチと同じフォールバック補完済み）を取得して返す。
    戻り値 dict（price/eps_forward/eps_ttm/bps/equity_ratio/ord_growth/
    fwd_growth/cagr_growth/market_cap/name）。データが無ければ None。"""
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
    if not row:
        cur.close(); conn.close()
        return None
    close, eps_fwd, eps_ttm, bps, roa, eq_ratio, ord_growth, mktcap, name = row

    # 成長率（会社予想・過去3年CAGR）をこの銘柄について算出
    # 会社予想 = financials に未来日付で入る翌期経常益（無ければ financials_forecast）
    cur.execute("""
        SELECT period_end, ordinary_income FROM financials
        WHERE code=%s AND period_type='A' AND ordinary_income IS NOT NULL
        ORDER BY period_end
    """, (code,))
    from datetime import date as _d
    today = _d.today()
    actual_asc, future_oi = [], None
    for pend, oi in cur.fetchall():
        if pend <= today:
            actual_asc.append(float(oi))
        elif future_oi is None:
            future_oi = float(oi)
    cur.execute("""
        SELECT ordinary_income FROM financials_forecast
        WHERE code=%s AND period_type='A' AND ordinary_income IS NOT NULL
        ORDER BY announced_at DESC LIMIT 1
    """, (code,))
    frow = cur.fetchone()
    cur.close(); conn.close()

    fb = float(frow[0]) if frow else None
    fwd_g, cagr_g = _growth_from_series(list(reversed(actual_asc)), future_oi, fb)

    return {
        "price": float(close) if close is not None else None,
        "eps_forward": float(eps_fwd) if eps_fwd is not None else None,
        "eps_ttm": float(eps_ttm) if eps_ttm is not None else None,
        "bps": float(bps) if bps is not None else None,
        "equity_ratio": float(eq_ratio) if eq_ratio is not None else None,
        "ord_growth": float(ord_growth) if ord_growth is not None else None,
        "fwd_growth": fwd_g,          # 小数
        "cagr_growth": cagr_g,        # 小数
        "market_cap": float(mktcap) if mktcap is not None else None,
        "name": name or "",
    }


def compute_for_code(code: str, overrides: dict | None = None):
    """1銘柄の理論株価一式を計算して返す（銘柄名付き）。
    overrides で eps/roa/equity_ratio/ord_growth/price を差し替え可能（What-if用）。
    ord_growth を指定すると成長率を一定値で上書きする（ブレンドを無効化）。
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
    price    = pick("price", inp["price"])
    # ord_growth を明示指定した場合は成長率一定（ブレンド無効化）
    ord_override = o.get("ord_growth")
    roa_override = o.get("roa")

    res = compute_one(
        price=price,
        eps_forward=eps_fwd,
        eps_ttm=eps_ttm,
        bps=pick("bps", inp["bps"]),
        equity_ratio_frac=(eq_ratio / 100.0) if eq_ratio is not None else None,
        fwd_growth_frac=(None if ord_override is not None else inp["fwd_growth"]),
        cagr_growth_frac=(None if ord_override is not None else inp["cagr_growth"]),
        ord_growth_frac=(ord_override / 100.0) if ord_override is not None
                        else (inp["ord_growth"] / 100.0 if inp["ord_growth"] is not None else None),
        roa_override=(roa_override / 100.0) if roa_override is not None else None,
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
            market_cap         BIGINT,
            growth_basis       VARCHAR(12),
            growth_y1          DECIMAL(8,2),
            growth_lr          DECIMAL(8,2),
            fair_per           DECIMAL(10,1)
        )
    """)
    # 既存テーブルへの後方互換カラム追加（初回マイグレーション）
    for col, ddl in [
        ("growth_basis", "VARCHAR(12)"), ("growth_y1", "DECIMAL(8,2)"),
        ("growth_lr", "DECIMAL(8,2)"), ("fair_per", "DECIMAL(10,1)"),
    ]:
        try:
            cur.execute(f"ALTER TABLE theoretical_values ADD COLUMN {col} {ddl}")
        except Exception:
            pass  # 既に存在
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

    # 成長率（会社予想・過去3年CAGR）を全銘柄分ロード
    growth_map = _load_growth_all()

    cols = [
        "code", "close", "eps", "bps", "roa", "equity_ratio", "pbr", "ord_growth",
        "asset_value", "business_value", "theoretical_price", "upper_price",
        "biz_ratio", "theo_ratio", "upside_pct", "upper_upside_pct",
        "theo_3y", "upside_3y_pct", "judge_mult", "pass_all", "market_cap",
        "growth_basis", "growth_y1", "growth_lr", "fair_per",
    ]

    rows = []
    for code, close, eps_fwd, eps_ttm, bps, eq_ratio, ord_growth, mktcap in src:
        fwd_g, cagr_g = growth_map.get(code, (None, None))
        r = compute_one(
            price=float(close) if close is not None else None,
            eps_forward=float(eps_fwd) if eps_fwd is not None else None,
            eps_ttm=float(eps_ttm) if eps_ttm is not None else None,
            bps=float(bps) if bps is not None else None,
            equity_ratio_frac=(float(eq_ratio) / 100.0) if eq_ratio is not None else None,
            fwd_growth_frac=fwd_g,
            cagr_growth_frac=cagr_g,
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
            r["growth_basis"], r["growth_y1"], r["growth_lr"], r["fair_per"],
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
