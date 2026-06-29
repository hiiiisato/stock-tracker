"""
株価テクニカル指標・財務指標を毎日計算して price_stats テーブルに保存する。

計算指標:
  chg5d/25d/75d/126d … N営業日騰落率(%)
  ma5/25/50/75/200   … 移動平均
  ma200_slope        … SMA200 の20日前比変化率(%)  ← Stage2 判定用
  dev_ma25/75        … 移動平均乖離率(%)
  vol_ratio          … 直近5日出来高 / 25日平均
  vol20_ratio        … 当日出来高 / 20日平均        ← 出来高サージ判定用
  high52w/low52w     … 52週高値・安値
  dev_high52w        … 52週高値からの乖離率(%)
  rsi14              … 14日RSI（Wilder スムージング）
  macd               … MACD(12,26,9)
  macd_signal        … MACDシグナル
  macd_hist          … MACDヒストグラム
  macd_gc            … ゴールデンクロスフラグ（直近5本以内）
  turnover_day       … 当日売買代金（億円 = close×volume/1e8）
  turnover_20d       … 20日平均売買代金（億円）
  high20d            … 20日高値
  high65d            … 65日高値（約3ヶ月）
  break_20d          … 20日高値更新フラグ（当日高値が直前20日の最高値以上）
  break_65d          … 65日高値更新フラグ
  rev_growth         … 売上高成長率YoY(%)  from financials
  op_growth          … 営業利益成長率YoY(%) from financials
  eps_growth         … EPS成長率YoY(%)     from financials net_income/shares
  roic               … ROIC(%) = op_income / (equity+debt)
  cf_positive        … 営業CF プラスフラグ（1/0）

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
    new_cols = [
        ("macd",         "DECIMAL(14,6)"),
        ("macd_signal",  "DECIMAL(14,6)"),
        ("macd_hist",    "DECIMAL(14,6)"),
        ("macd_gc",      "TINYINT DEFAULT 0"),
        ("turnover_day", "DECIMAL(12,2)"),
        ("turnover_20d", "DECIMAL(12,2)"),
        ("high20d",      "DECIMAL(14,2)"),
        ("high65d",      "DECIMAL(14,2)"),
        ("break_20d",    "TINYINT DEFAULT 0"),
        ("break_65d",    "TINYINT DEFAULT 0"),
        ("rev_growth",   "DECIMAL(8,2)"),
        ("op_growth",    "DECIMAL(8,2)"),
        ("eps_growth",   "DECIMAL(8,2)"),
        ("roic",         "DECIMAL(8,2)"),
        ("cf_positive",  "TINYINT DEFAULT 0"),
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
    financials テーブルから各銘柄の最新2期分の通期データを取得し、
    YoY成長率・ROIC・CF判定を計算して返す。
    戻り値: {code: {rev_growth, op_growth, eps_growth, roic, cf_positive}}
    """
    conn = get_conn()
    cur  = conn.cursor()

    # 最新2期分の通期データを取得
    cur.execute("""
        SELECT f.code, f.period_end,
               f.revenue, f.operating_income, f.net_income,
               f.total_equity, f.total_debt, f.cf_operating,
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

    # code → [latest_row, prior_row] に整理
    by_code: dict[str, list] = defaultdict(list)
    for r in rows:
        by_code[r[0]].append(r)

    result = {}
    for code, periods in by_code.items():
        if len(periods) < 1:
            continue

        latest = periods[0]   # period_end が新しい順（ORDER BY DESC）
        prior  = periods[1] if len(periods) >= 2 else None

        rev_l = float(latest[2]) if latest[2] is not None else None
        opi_l = float(latest[3]) if latest[3] is not None else None
        ni_l  = float(latest[4]) if latest[4] is not None else None
        eq_l  = float(latest[5]) if latest[5] is not None else None
        dbt_l = float(latest[6]) if latest[6] is not None else None
        cfo_l = float(latest[7]) if latest[7] is not None else None
        shr   = float(latest[8]) if latest[8] is not None else None

        rev_p = float(prior[2]) if (prior and prior[2] is not None) else None
        opi_p = float(prior[3]) if (prior and prior[3] is not None) else None
        ni_p  = float(prior[4]) if (prior and prior[4] is not None) else None

        def yoy(new, old):
            if new is None or old is None or old == 0:
                return None
            return (new / old - 1) * 100

        rev_growth = yoy(rev_l, rev_p)
        op_growth  = yoy(opi_l, opi_p)

        # EPS成長率 = 純利益/株数 の YoY
        eps_l = (ni_l / shr) if (ni_l is not None and shr and shr > 0) else None
        eps_p = (ni_p / shr) if (ni_p is not None and shr and shr > 0) else None
        eps_growth = yoy(eps_l, eps_p)

        # ROIC = 営業利益 / (自己資本 + 有利子負債) × 100
        roic = None
        cap = (eq_l or 0) + (dbt_l or 0)
        if opi_l is not None and cap > 0:
            roic = opi_l / cap * 100

        cf_positive = 1 if (cfo_l is not None and cfo_l > 0) else 0

        result[code] = {
            "rev_growth":  _round(rev_growth),
            "op_growth":   _round(op_growth),
            "eps_growth":  _round(eps_growth),
            "roic":        _round(roic),
            "cf_positive": cf_positive,
        }

    return result


def run() -> int:
    _ensure_table()

    # ─── 株価データ取得 ───────────────────────────────────────────────────────
    conn = get_conn()
    cur  = conn.cursor()

    # MA200(200日) + スロープ用20日前 + 65日高値 = 最低265営業日必要 → 500カレンダー日で確保
    date_from = (date.today() - timedelta(days=500)).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT code, date,
               COALESCE(adj_close, close) AS close,  -- 分割調整済を優先、なければ生値
               high, low, volume,
               close AS raw_close                     -- 売買代金計算は生値で
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
               MAX(COALESCE(adj_close, high)),   -- 調整済終値の52週高値
               MIN(COALESCE(adj_close, low))    -- 調整済終値の52週安値
        FROM daily_prices
        WHERE date >= %s AND close IS NOT NULL
        GROUP BY code
    """, (date_from_52w,))
    w52 = {r[0]: (float(r[1]) if r[1] else None, float(r[2]) if r[2] else None)
           for r in cur.fetchall()}

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
        highs     = [p[2] for p in prices]   # raw high（サポレジ用）
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
        dev_ma25 = (last - ma25) / ma25 * 100 if ma25 else None
        dev_ma75 = (last - ma75) / ma75 * 100 if ma75 else None

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
        if n >= 25:
            avg5  = sum(vols[-5:])  / 5
            avg25 = sum(vols[-25:]) / 25
            vol_ratio = avg5 / avg25 if avg25 else None

        vol20_ratio = None
        if n >= 20 and vols[-1] > 0:
            avg20 = sum(vols[-20:]) / 20
            vol20_ratio = vols[-1] / avg20 if avg20 else None

        # ─ 52週高値・安値 ─
        high52, low52 = w52.get(code, (None, None))
        dev_high52w = (last / high52 - 1) * 100 if (high52 and high52 > 0) else None

        # ─ RSI14 ─
        rsi14 = _rsi(closes)

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

        # ─ 財務指標（financials テーブルから）─
        fm = fin_metrics.get(code, {})
        rev_growth  = fm.get("rev_growth")
        op_growth   = fm.get("op_growth")
        eps_growth  = fm.get("eps_growth")
        roic        = fm.get("roic")
        cf_positive = fm.get("cf_positive", 0)

        rows.append((
            code, today, _round(last_raw, 2),   # close は表示用に生値
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
