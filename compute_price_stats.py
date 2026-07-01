"""
株価テクニカル指標・財務指標を毎日計算して price_stats テーブルに保存する。

【テクニカル指標】
  chg5d/25d/75d/126d … N営業日騰落率(%)
  ma5/25/50/75/200   … 移動平均
  ma200_slope        … SMA200 の20日前比変化率(%)  ← Stage2 判定用
  dev_ma25/75/200    … 移動平均乖離率(%)
  vol_ratio          … 直近5日出来高 / 25日平均
  vol20_ratio        … 当日出来高 / 20日平均        ← 出来高サージ判定用
  vol_ratio_6_25     … 6日平均出来高 / 25日平均出来高
  high52w/low52w     … 52週高値・安値
  dev_high52w        … 52週高値からの乖離率(%)
  dev_low52w         … 52週安値からの上昇率(%)
  ytd_high/ytd_low   … 年初来高値・安値
  break_ytd_high     … 年初来高値更新フラグ
  dev_ytd_high       … 年初来高値からの乖離率(%)
  dev_ytd_low        … 年初来安値からの上昇率(%)
  rsi14              … 14日RSI（Wilder スムージング）
  macd               … MACD(12,26,9)
  macd_signal/hist/gc… MACDシグナル・ヒストグラム・GCフラグ
  stoch_k/stoch_d    … ストキャスティクス %K/%D (14日, 3日SMA)
  bb_upper/bb_lower  … ボリンジャー±2σ突破フラグ
  volatility_60d     … 60日ヒストリカルボラティリティ（年率%）
  gc_5_25            … MA5>MA25フラグ（ゴールデンクロス①状態）
  gc_75_200          … MA75>MA200フラグ（ゴールデンクロス③状態）
  turnover_day       … 当日売買代金（億円）
  turnover_20d       … 20日平均売買代金（億円）
  high20d/high65d    … 20日・65日高値
  break_20d/break_65d… 高値更新フラグ
  nikkei_rel_1m      … 対日経平均1ヶ月相対パフォーマンス(%)

【財務指標】(financials + stock_fundamentals)
  rev_growth/op_growth/eps_growth … 売上高・営業利益・EPS 成長率YoY(%)
  ord_growth         … 経常利益成長率YoY(%)
  roic               … ROIC(%)
  cf_positive        … 営業CF プラスフラグ（1/0）
  equity_ratio       … 自己資本比率(%)
  ord_margin         … 経常利益率(%)
  psr                … PSR(倍) = 時価総額/売上高（当日終値ベース）
  pcfr               … PCFR(倍) = 株価/1株CFO（当日終値ベース）

daily_run.py から毎日呼び出す。
単体実行: python3 compute_price_stats.py
"""

from datetime import date, timedelta
from collections import defaultdict
from config import get_conn, bulk_upsert


def _round(v, dec=2):
    return round(v, dec) if v is not None else None


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder スムージング法による RSI を計算する。"""
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def _ema_list(values: list[float], period: int) -> list[float]:
    """EMAの系列を返す（長さ = len(values) - period + 1）。"""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    result = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        result.append(e)
    return result


def _stoch_calc(
    closes: list[float], highs: list[float], lows: list[float], period: int = 14, smooth: int = 3
) -> tuple[float | None, float | None]:
    """
    ストキャスティクス %K/%D を計算する。
    %K = (close - lowest_low_N) / (highest_high_N - lowest_low_N) * 100
    %D = %K の smooth 日単純移動平均
    戻り値: (stoch_k, stoch_d) または (None, None)
    """
    n = len(closes)
    if n < period + smooth - 1:
        return None, None
    k_series = []
    for i in range(period - 1, n):
        hi_n  = max(highs[i - period + 1: i + 1])
        lo_n  = min(lows [i - period + 1: i + 1])
        denom = hi_n - lo_n
        k = (closes[i] - lo_n) / denom * 100 if denom > 0 else 50.0
        k_series.append(k)
    if len(k_series) < smooth:
        return None, None
    stoch_k = k_series[-1]
    stoch_d = sum(k_series[-smooth:]) / smooth
    return stoch_k, stoch_d


def _volatility_60d(closes: list[float]) -> float | None:
    """60営業日ヒストリカルボラティリティ（年率%）を計算する。"""
    import math
    if len(closes) < 62:
        return None
    log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(-61, 0)]
    n = len(log_rets)
    mean = sum(log_rets) / n
    variance = sum((r - mean) ** 2 for r in log_rets) / (n - 1)
    return math.sqrt(variance * 252) * 100


def _macd_calc(closes: list[float]) -> tuple[float | None, float | None, float | None, int]:
    """
    MACD(12,26,9) を計算する。
    戻り値: (macd, signal, histogram, is_gc)
    is_gc: 直近5本以内にゴールデンクロスがあった場合 1、なければ 0
    """
    if len(closes) < 35:  # 26 + 9 = 35本最低限必要
        return None, None, None, 0

    fast = _ema_list(closes, 12)  # len = n - 11
    slow = _ema_list(closes, 26)  # len = n - 25

    # fast[14:] と slow を末尾揃えで差分（MACD = EMA12 - EMA26）
    macd = [f - s for f, s in zip(fast[14:], slow)]

    sig = _ema_list(macd, 9)
    if not sig:
        return None, None, None, 0

    cur_macd = macd[-1]
    cur_sig  = sig[-1]
    cur_hist = cur_macd - cur_sig

    # 直近5本のペアでゴールデンクロスを探す（sig と macd は末尾が同時点）
    is_gc = 0
    check = min(5, len(sig) - 1)
    for i in range(-check, 0):
        if macd[i - 1] < sig[i - 1] and macd[i] >= sig[i]:
            is_gc = 1
            break

    return cur_macd, cur_sig, cur_hist, is_gc


def _ensure_table():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_stats (
            code          VARCHAR(10)  NOT NULL PRIMARY KEY,
            updated_at    DATE,
            close         DECIMAL(14,2),
            chg5d         DECIMAL(8,2),
            chg25d        DECIMAL(8,2),
            chg75d        DECIMAL(8,2),
            chg126d       DECIMAL(8,2),
            ma5           DECIMAL(14,4),
            ma25          DECIMAL(14,4),
            ma50          DECIMAL(14,4),
            ma75          DECIMAL(14,4),
            ma200         DECIMAL(14,4),
            ma200_slope   DECIMAL(8,4),
            dev_ma25      DECIMAL(8,2),
            dev_ma75      DECIMAL(8,2),
            vol_ratio     DECIMAL(8,2),
            vol20_ratio   DECIMAL(8,2),
            high52w       DECIMAL(14,2),
            low52w        DECIMAL(14,2),
            dev_high52w   DECIMAL(8,2),
            rsi14         DECIMAL(6,2),
            macd          DECIMAL(14,6),
            macd_signal   DECIMAL(14,6),
            macd_hist     DECIMAL(14,6),
            macd_gc       TINYINT DEFAULT 0,
            turnover_day  DECIMAL(12,2),
            turnover_20d  DECIMAL(12,2),
            high20d       DECIMAL(14,2),
            high65d       DECIMAL(14,2),
            break_20d     TINYINT DEFAULT 0,
            break_65d     TINYINT DEFAULT 0,
            rev_growth    DECIMAL(8,2),
            op_growth     DECIMAL(8,2),
            eps_growth    DECIMAL(8,2),
            roic          DECIMAL(8,2),
            cf_positive   TINYINT DEFAULT 0
        )
    """)
    # 既存テーブルへの新カラム追加（初回マイグレーション）
    # ※ CREATE TABLE にカラムを追加した場合はここにも追記すること
    new_cols = [
        # --- 既存 ---
        ("macd",           "DECIMAL(14,6)"),
        ("macd_signal",    "DECIMAL(14,6)"),
        ("macd_hist",      "DECIMAL(14,6)"),
        ("macd_gc",        "TINYINT DEFAULT 0"),
        ("turnover_day",   "DECIMAL(12,2)"),
        ("turnover_20d",   "DECIMAL(12,2)"),
        ("high20d",        "DECIMAL(14,2)"),
        ("high65d",        "DECIMAL(14,2)"),
        ("break_20d",      "TINYINT DEFAULT 0"),
        ("break_65d",      "TINYINT DEFAULT 0"),
        ("rev_growth",     "DECIMAL(8,2)"),
        ("op_growth",      "DECIMAL(8,2)"),
        ("eps_growth",     "DECIMAL(8,2)"),
        ("roic",           "DECIMAL(8,2)"),
        ("cf_positive",    "TINYINT DEFAULT 0"),
        # --- テクニカル追加 ---
        ("dev_ma200",      "DECIMAL(8,2)"),
        ("dev_low52w",     "DECIMAL(8,2)"),
        ("vol_ratio_6_25", "DECIMAL(6,2)"),
        ("volatility_60d", "DECIMAL(6,2)"),
        ("gc_5_25",        "TINYINT DEFAULT 0"),
        ("gc_75_200",      "TINYINT DEFAULT 0"),
        ("bb_upper",       "TINYINT DEFAULT 0"),
        ("bb_lower",       "TINYINT DEFAULT 0"),
        ("stoch_k",        "DECIMAL(6,2)"),
        ("stoch_d",        "DECIMAL(6,2)"),
        ("ytd_high",       "DECIMAL(14,2)"),
        ("ytd_low",        "DECIMAL(14,2)"),
        ("break_ytd_high", "TINYINT DEFAULT 0"),
        ("dev_ytd_high",   "DECIMAL(8,2)"),
        ("dev_ytd_low",    "DECIMAL(8,2)"),
        ("nikkei_rel_1m",  "DECIMAL(8,2)"),
        # --- 財務追加 ---
        ("equity_ratio",   "DECIMAL(8,2)"),
        ("ord_margin",     "DECIMAL(8,2)"),
        ("ord_growth",     "DECIMAL(8,2)"),
        ("psr",            "DECIMAL(8,2)"),
        ("pcfr",           "DECIMAL(8,2)"),
    ]
    for col, typedef in new_cols:
        try:
            cur.execute(f"ALTER TABLE price_stats ADD COLUMN {col} {typedef}")
        except Exception:
            pass  # 既存カラムは無視
    conn.commit()
    cur.close()
    conn.close()


def _load_financials() -> dict[str, dict]:
    """
    financials + stock_fundamentals から財務指標を計算して返す。
    戻り値 dict のキー:
      rev_growth, op_growth, eps_growth, ord_growth … 成長率YoY(%)
      roic, cf_positive                              … ROIC・CFフラグ
      equity_ratio                                   … 自己資本比率(%)
      ord_margin                                     … 経常利益率(%)
      cfo_per_share                                  … 1株CFO（PCFR計算用）
      rev_per_share                                  … 1株売上（PSR計算用）
    """
    conn = get_conn()
    cur  = conn.cursor()

    # 最新2期分の通期データ + stock_fundamentals
    cur.execute("""
        SELECT f.code, f.period_end,
               f.revenue, f.operating_income, f.ordinary_income, f.net_income,
               f.total_assets, f.total_equity, f.total_debt, f.cf_operating,
               s.shares_outstanding
        FROM financials f
        JOIN (
            SELECT code,
                   MAX(CASE WHEN rn=1 THEN period_end END) AS latest,
                   MAX(CASE WHEN rn=2 THEN period_end END) AS prior
            FROM (
                SELECT code, period_end,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY period_end DESC) AS rn
                FROM financials
                WHERE period_type = 'A'
            ) ranked
            WHERE rn <= 2
            GROUP BY code
        ) top2 ON f.code = top2.code
              AND f.period_end IN (top2.latest, top2.prior)
        LEFT JOIN stock_fundamentals s ON f.code = s.code
        WHERE f.period_type = 'A'
        ORDER BY f.code, f.period_end DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    by_code: dict[str, list] = defaultdict(list)
    for r in rows:
        by_code[r[0]].append(r)

    result = {}
    for code, periods in by_code.items():
        if not periods:
            continue

        latest = periods[0]
        prior  = periods[1] if len(periods) >= 2 else None

        rev_l  = float(latest[2])  if latest[2]  is not None else None
        opi_l  = float(latest[3])  if latest[3]  is not None else None
        ord_l  = float(latest[4])  if latest[4]  is not None else None
        ni_l   = float(latest[5])  if latest[5]  is not None else None
        ta_l   = float(latest[6])  if latest[6]  is not None else None
        eq_l   = float(latest[7])  if latest[7]  is not None else None
        dbt_l  = float(latest[8])  if latest[8]  is not None else None
        cfo_l  = float(latest[9])  if latest[9]  is not None else None
        shr    = float(latest[10]) if latest[10] is not None else None

        rev_p  = float(prior[2])  if (prior and prior[2]  is not None) else None
        opi_p  = float(prior[3])  if (prior and prior[3]  is not None) else None
        ord_p  = float(prior[4])  if (prior and prior[4]  is not None) else None
        ni_p   = float(prior[5])  if (prior and prior[5]  is not None) else None

        def yoy(new, old):
            if new is None or old is None or old == 0:
                return None
            return (new / old - 1) * 100

        rev_growth = yoy(rev_l, rev_p)
        op_growth  = yoy(opi_l, opi_p)
        ord_growth = yoy(ord_l, ord_p)

        eps_l = (ni_l / shr) if (ni_l is not None and shr and shr > 0) else None
        eps_p = (ni_p / shr) if (ni_p is not None and shr and shr > 0) else None
        eps_growth = yoy(eps_l, eps_p)

        # ROIC = 営業利益 / (自己資本 + 有利子負債) × 100
        roic = None
        cap = (eq_l or 0) + (dbt_l or 0)
        if opi_l is not None and cap > 0:
            roic = opi_l / cap * 100

        cf_positive  = 1 if (cfo_l is not None and cfo_l > 0) else 0

        # 自己資本比率 = 自己資本 / 総資産
        equity_ratio = eq_l / ta_l * 100 if (eq_l is not None and ta_l and ta_l > 0) else None

        # 経常利益率 = 経常利益 / 売上高
        ord_margin   = ord_l / rev_l * 100 if (ord_l is not None and rev_l and rev_l > 0) else None

        # 1株あたり CFO・売上高（当日終値と組み合わせて PCFR/PSR を計算するため）
        cfo_per_share = cfo_l / shr if (cfo_l is not None and shr and shr > 0) else None
        rev_per_share = rev_l / shr if (rev_l is not None and shr and shr > 0) else None

        result[code] = {
            "rev_growth":    _round(rev_growth),
            "op_growth":     _round(op_growth),
            "ord_growth":    _round(ord_growth),
            "eps_growth":    _round(eps_growth),
            "roic":          _round(roic),
            "cf_positive":   cf_positive,
            "equity_ratio":  _round(equity_ratio),
            "ord_margin":    _round(ord_margin),
            "cfo_per_share": cfo_per_share,
            "rev_per_share": rev_per_share,
        }

    return result


def run() -> int:
    _ensure_table()

    # ─── 株価データ取得 ───────────────────────────────────────────────────────
    conn = get_conn()
    cur  = conn.cursor()

    # MA200(200日) + スロープ用20日前 + 60日ボラ + ストキャス = 最低265日 → 500カレンダー日で確保
    date_from = (date.today() - timedelta(days=500)).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT code, date,
               COALESCE(adj_close, close) AS close,
               high, low, volume,
               close AS raw_close
        FROM daily_prices
        WHERE date >= %s AND close IS NOT NULL AND close > 0
        ORDER BY code, date
    """, (date_from,))

    data: dict[str, list] = defaultdict(list)
    for code, dt, close, high, low, vol, raw_close in cur.fetchall():
        data[code].append((dt, float(close), float(high or close), float(low or close), int(vol or 0), float(raw_close or close)))

    # 52週高値・安値（調整済価格で取得）
    date_from_52w = (date.today() - timedelta(days=380)).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT code,
               MAX(COALESCE(adj_close, close)),
               MIN(COALESCE(adj_close, close))
        FROM daily_prices
        WHERE date >= %s AND close IS NOT NULL
        GROUP BY code
    """, (date_from_52w,))
    w52 = {r[0]: (float(r[1]) if r[1] else None, float(r[2]) if r[2] else None)
           for r in cur.fetchall()}

    # 年初来高値・安値（当年1月1日以降）
    ytd_from = f"{date.today().year}-01-01"
    cur.execute("""
        SELECT code,
               MAX(COALESCE(adj_close, close)),
               MIN(COALESCE(adj_close, close))
        FROM daily_prices
        WHERE date >= %s AND close IS NOT NULL
        GROUP BY code
    """, (ytd_from,))
    ytd_map = {r[0]: (float(r[1]) if r[1] else None, float(r[2]) if r[2] else None)
               for r in cur.fetchall()}

    # 日経平均 1ヶ月前の終値（25営業日 ≒ 40カレンダー日前に最も近いデータ）
    nk_from = (date.today() - timedelta(days=60)).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT date, close FROM market_index_prices
        WHERE symbol = %s AND date >= %s
        ORDER BY date
    """, ("^N225", nk_from))
    nk_rows = cur.fetchall()
    nk_close_map: dict[str, float] = {str(r[0]): float(r[1]) for r in nk_rows if r[1]}
    nk_sorted = sorted(nk_close_map.items())
    nikkei_1m_chg: float | None = None
    if len(nk_sorted) >= 26:
        nk_now  = nk_sorted[-1][1]
        nk_past = nk_sorted[-26][1]
        nikkei_1m_chg = (nk_now / nk_past - 1) * 100 if nk_past else None

    cur.close()
    conn.close()

    # ─── 財務指標取得 ─────────────────────────────────────────────────────────
    fin_metrics = _load_financials()

    # ─── 指標計算 ─────────────────────────────────────────────────────────────
    rows = []
    today = str(date.today())

    for code, prices in data.items():
        n = len(prices)
        if n < 5:
            continue

        closes    = [p[1] for p in prices]   # adj_close（指標計算用）
        highs     = [p[2] for p in prices]   # high
        lows      = [p[3] for p in prices]   # low
        vols      = [p[4] for p in prices]
        raw_cls   = [p[5] for p in prices]   # 生終値（売買代金計算用）
        last      = closes[-1]
        last_hi   = highs[-1]
        last_raw  = raw_cls[-1]

        # ─ 移動平均 ─
        ma5   = sum(closes[-5:])   / 5   if n >= 5   else None
        ma25  = sum(closes[-25:])  / 25  if n >= 25  else None
        ma50  = sum(closes[-50:])  / 50  if n >= 50  else None
        ma75  = sum(closes[-75:])  / 75  if n >= 75  else None
        ma200 = sum(closes[-200:]) / 200 if n >= 200 else None

        ma200_slope = None
        if n >= 220 and ma200 is not None:
            ma200_20d = sum(closes[-220:-20]) / 200
            if ma200_20d and ma200_20d > 0:
                ma200_slope = (ma200 - ma200_20d) / ma200_20d * 100

        # ─ 乖離率 ─
        dev_ma25  = (last - ma25)  / ma25  * 100 if ma25  else None
        dev_ma75  = (last - ma75)  / ma75  * 100 if ma75  else None
        dev_ma200 = (last - ma200) / ma200 * 100 if ma200 else None

        # ─ N日騰落率 ─
        def chg_nd(n_days):
            if n <= n_days:
                return None
            past = closes[-(n_days + 1)]
            return (last / past - 1) * 100 if past else None

        chg5d   = chg_nd(5)
        chg25d  = chg_nd(25)
        chg75d  = chg_nd(75)
        chg126d = chg_nd(126)

        # ─ 出来高比率 ─
        vol_ratio = None
        vol_ratio_6_25 = None
        if n >= 25:
            avg6  = sum(vols[-6:])  / 6
            avg25 = sum(vols[-25:]) / 25
            vol_ratio = avg6 / avg25 if avg25 else None  # 旧 vol_ratio は avg5/avg25 だったが avg6 に統一
            vol_ratio_6_25 = avg6 / avg25 if avg25 else None

        vol20_ratio = None
        if n >= 20 and vols[-1] > 0:
            avg20 = sum(vols[-20:]) / 20
            vol20_ratio = vols[-1] / avg20 if avg20 else None

        # ─ 52週高値・安値 ─
        high52, low52 = w52.get(code, (None, None))
        dev_high52w = (last / high52 - 1) * 100 if (high52 and high52 > 0) else None
        dev_low52w  = (last / low52  - 1) * 100 if (low52  and low52  > 0) else None

        # ─ 年初来高値・安値 ─
        ytd_hi, ytd_lo = ytd_map.get(code, (None, None))
        break_ytd_high = 0
        dev_ytd_high   = None
        dev_ytd_low    = None
        if ytd_hi:
            dev_ytd_high   = (last / ytd_hi - 1) * 100
            break_ytd_high = 1 if last >= ytd_hi * 0.9999 else 0
        if ytd_lo:
            dev_ytd_low = (last / ytd_lo - 1) * 100

        # ─ RSI14 ─
        rsi14 = _rsi(closes)

        # ─ ストキャスティクス(14,3) ─
        stoch_k, stoch_d = _stoch_calc(closes, highs, lows)

        # ─ ボリンジャーバンド(25,2σ) ─
        bb_upper = bb_lower = 0
        if n >= 25:
            import math
            ma25_ser = closes[-25:]
            bb_mean  = sum(ma25_ser) / 25
            bb_std   = math.sqrt(sum((v - bb_mean) ** 2 for v in ma25_ser) / 25)
            bb_upper = 1 if last >= bb_mean + 2 * bb_std else 0
            bb_lower = 1 if last <= bb_mean - 2 * bb_std else 0

        # ─ 60日ボラティリティ（年率%）─
        volatility_60d = _volatility_60d(closes)

        # ─ ゴールデンクロス状態フラグ ─
        gc_5_25   = (1 if (ma5  is not None and ma25  is not None and ma5  > ma25)  else 0)
        gc_75_200 = (1 if (ma75 is not None and ma200 is not None and ma75 > ma200) else 0)

        # ─ MACD(12,26,9) ─
        macd, macd_signal, macd_hist, macd_gc = _macd_calc(closes)

        # ─ 売買代金（実際の売買金額なので生値×出来高を使う）─
        turnover_day  = last_raw * vols[-1] / 1e8 if vols[-1] else None
        turnover_20d  = None
        if n >= 20:
            t20 = [raw_cls[i] * vols[i] / 1e8 for i in range(-20, 0) if vols[i]]
            if t20:
                turnover_20d = sum(t20) / len(t20)

        # ─ 20日高値・65日高値 ─
        high20d = max(highs[-20:]) if n >= 20 else None
        high65d = max(highs[-65:]) if n >= 65 else None

        # ─ 高値更新フラグ（当日の高値が直前N日の最高値以上） ─
        break_20d = 0
        if n >= 21:
            prev_high20 = max(highs[-21:-1])
            break_20d = 1 if last_hi >= prev_high20 else 0

        break_65d = 0
        if n >= 66:
            prev_high65 = max(highs[-66:-1])
            break_65d = 1 if last_hi >= prev_high65 else 0

        # ─ 財務指標（financials + stock_fundamentals）─
        fm = fin_metrics.get(code, {})
        rev_growth   = fm.get("rev_growth")
        op_growth    = fm.get("op_growth")
        ord_growth   = fm.get("ord_growth")
        eps_growth   = fm.get("eps_growth")
        roic         = fm.get("roic")
        cf_positive  = fm.get("cf_positive", 0)
        equity_ratio = fm.get("equity_ratio")
        ord_margin   = fm.get("ord_margin")

        # PSR・PCFR は当日終値で動的計算
        cfo_ps = fm.get("cfo_per_share")
        rev_ps = fm.get("rev_per_share")
        pcfr = _round(last_raw / cfo_ps, 1) if (cfo_ps and cfo_ps > 0) else None
        psr  = _round(last_raw / rev_ps,  2) if (rev_ps  and rev_ps  > 0) else None

        # ─ 対日経1ヶ月相対パフォーマンス ─
        nikkei_rel_1m = None
        if nikkei_1m_chg is not None and chg25d is not None:
            nikkei_rel_1m = _round(chg25d - nikkei_1m_chg)

        rows.append((
            code, today, _round(last_raw, 2),
            _round(chg5d), _round(chg25d), _round(chg75d), _round(chg126d),
            _round(ma5, 4), _round(ma25, 4), _round(ma50, 4),
            _round(ma75, 4), _round(ma200, 4), _round(ma200_slope, 4),
            _round(dev_ma25), _round(dev_ma75),
            _round(vol_ratio), _round(vol20_ratio),
            _round(high52, 2), _round(low52, 2), _round(dev_high52w),
            _round(rsi14, 2),
            _round(macd, 4), _round(macd_signal, 4), _round(macd_hist, 4), macd_gc,
            _round(turnover_day, 2), _round(turnover_20d, 2),
            _round(high20d, 2), _round(high65d, 2),
            break_20d, break_65d,
            rev_growth, op_growth, eps_growth, roic, cf_positive,
            # ─ 追加テクニカル ─
            _round(dev_ma200), _round(dev_low52w),
            _round(vol_ratio_6_25), _round(volatility_60d),
            gc_5_25, gc_75_200,
            bb_upper, bb_lower,
            _round(stoch_k), _round(stoch_d),
            _round(ytd_hi, 2), _round(ytd_lo, 2),
            break_ytd_high, _round(dev_ytd_high), _round(dev_ytd_low),
            nikkei_rel_1m,
            # ─ 追加財務 ─
            equity_ratio, ord_margin, ord_growth, psr, pcfr,
        ))

    # ─── DB 保存 ──────────────────────────────────────────────────────────────
    conn = get_conn()
    cur  = conn.cursor()
    all_cols = [
        "code", "updated_at", "close",
        "chg5d", "chg25d", "chg75d", "chg126d",
        "ma5", "ma25", "ma50", "ma75", "ma200", "ma200_slope",
        "dev_ma25", "dev_ma75",
        "vol_ratio", "vol20_ratio",
        "high52w", "low52w", "dev_high52w",
        "rsi14",
        "macd", "macd_signal", "macd_hist", "macd_gc",
        "turnover_day", "turnover_20d",
        "high20d", "high65d",
        "break_20d", "break_65d",
        "rev_growth", "op_growth", "eps_growth", "roic", "cf_positive",
        # 追加テクニカル
        "dev_ma200", "dev_low52w",
        "vol_ratio_6_25", "volatility_60d",
        "gc_5_25", "gc_75_200",
        "bb_upper", "bb_lower",
        "stoch_k", "stoch_d",
        "ytd_high", "ytd_low",
        "break_ytd_high", "dev_ytd_high", "dev_ytd_low",
        "nikkei_rel_1m",
        # 追加財務
        "equity_ratio", "ord_margin", "ord_growth", "psr", "pcfr",
    ]
    bulk_upsert(cur, "price_stats", all_cols, rows,
                update_cols=[c for c in all_cols if c != "code"])
    conn.commit()
    cur.close()
    conn.close()

    print(f"  price_stats: {len(rows)} 銘柄更新")
    return len(rows)


if __name__ == "__main__":
    n = run()
    print(f"完了: {n} 銘柄")
