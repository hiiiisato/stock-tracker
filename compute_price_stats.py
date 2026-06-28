"""
株価テクニカル指標を毎日計算して price_stats テーブルに保存する。

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
            rsi14         DECIMAL(6,2)
        )
    """)
    # 既存テーブルへのカラム追加（初回マイグレーション: エラーは無視）
    for col, typedef in [
        ("chg126d",     "DECIMAL(8,2)"),
        ("ma50",        "DECIMAL(14,4)"),
        ("ma200",       "DECIMAL(14,4)"),
        ("ma200_slope", "DECIMAL(8,4)"),
        ("vol20_ratio", "DECIMAL(8,2)"),
        ("rsi14",       "DECIMAL(6,2)"),
    ]:
        try:
            cur.execute(f"ALTER TABLE price_stats ADD COLUMN {col} {typedef}")
        except Exception:
            pass
    conn.commit()
    cur.close()
    conn.close()


def run() -> int:
    _ensure_table()

    conn = get_conn()
    cur  = conn.cursor()

    # MA200(200日) + スロープ用20日前 = 220営業日必要 → 1年≈245営業日 → 400カレンダー日で確保
    date_from = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT code, date, close, high, low, volume
        FROM daily_prices
        WHERE date >= %s AND close IS NOT NULL AND close > 0
        ORDER BY code, date
    """, (date_from,))

    data: dict[str, list] = defaultdict(list)
    for code, dt, close, high, low, vol in cur.fetchall():
        data[code].append((dt, float(close), float(high or close), float(low or close), int(vol or 0)))

    # 52週高値・安値（別クエリで取得）
    date_from_52w = (date.today() - timedelta(days=380)).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT code, MAX(high), MIN(low)
        FROM daily_prices
        WHERE date >= %s AND high IS NOT NULL AND low IS NOT NULL
        GROUP BY code
    """, (date_from_52w,))
    w52 = {r[0]: (float(r[1]) if r[1] else None, float(r[2]) if r[2] else None)
           for r in cur.fetchall()}

    cur.close()
    conn.close()

    rows = []
    today = str(date.today())

    for code, prices in data.items():
        n = len(prices)
        if n < 5:
            continue

        closes = [p[1] for p in prices]
        vols   = [p[4] for p in prices]
        last   = closes[-1]

        # 移動平均
        ma5   = sum(closes[-5:])   / 5   if n >= 5   else None
        ma25  = sum(closes[-25:])  / 25  if n >= 25  else None
        ma50  = sum(closes[-50:])  / 50  if n >= 50  else None
        ma75  = sum(closes[-75:])  / 75  if n >= 75  else None
        ma200 = sum(closes[-200:]) / 200 if n >= 200 else None

        # SMA200 の20日前比変化率（Weinstein Stage 判定用）
        ma200_slope = None
        if n >= 220 and ma200 is not None:
            ma200_20d = sum(closes[-220:-20]) / 200
            if ma200_20d and ma200_20d > 0:
                ma200_slope = (ma200 - ma200_20d) / ma200_20d * 100

        # 乖離率
        dev_ma25 = (last - ma25) / ma25 * 100 if ma25 else None
        dev_ma75 = (last - ma75) / ma75 * 100 if ma75 else None

        # N日騰落率
        def chg_nd(n_days):
            if n <= n_days:
                return None
            past = closes[-(n_days + 1)]
            return (last / past - 1) * 100 if past else None

        chg5d   = chg_nd(5)
        chg25d  = chg_nd(25)
        chg75d  = chg_nd(75)
        chg126d = chg_nd(126)

        # 出来高比率
        vol_ratio = None
        if n >= 25:
            avg5  = sum(vols[-5:])  / 5
            avg25 = sum(vols[-25:]) / 25
            vol_ratio = avg5 / avg25 if avg25 else None

        vol20_ratio = None
        if n >= 20 and vols[-1] > 0:
            avg20 = sum(vols[-20:]) / 20
            vol20_ratio = vols[-1] / avg20 if avg20 else None

        # 52週高値・安値
        high52, low52 = w52.get(code, (None, None))
        dev_high52w = (last / high52 - 1) * 100 if (high52 and high52 > 0) else None

        # RSI14（Wilder スムージング）
        rsi14 = _rsi(closes)

        rows.append((
            code, today, _round(last, 2),
            _round(chg5d), _round(chg25d), _round(chg75d), _round(chg126d),
            _round(ma5, 4), _round(ma25, 4), _round(ma50, 4),
            _round(ma75, 4), _round(ma200, 4), _round(ma200_slope, 4),
            _round(dev_ma25), _round(dev_ma75),
            _round(vol_ratio), _round(vol20_ratio),
            _round(high52, 2), _round(low52, 2), _round(dev_high52w),
            _round(rsi14, 2),
        ))

    conn = get_conn()
    cur  = conn.cursor()
    bulk_upsert(cur, "price_stats",
        ["code", "updated_at", "close",
         "chg5d", "chg25d", "chg75d", "chg126d",
         "ma5", "ma25", "ma50", "ma75", "ma200", "ma200_slope",
         "dev_ma25", "dev_ma75",
         "vol_ratio", "vol20_ratio",
         "high52w", "low52w", "dev_high52w",
         "rsi14"],
        rows,
        update_cols=["updated_at", "close",
                     "chg5d", "chg25d", "chg75d", "chg126d",
                     "ma5", "ma25", "ma50", "ma75", "ma200", "ma200_slope",
                     "dev_ma25", "dev_ma75",
                     "vol_ratio", "vol20_ratio",
                     "high52w", "low52w", "dev_high52w",
                     "rsi14"])
    conn.commit()
    cur.close()
    conn.close()

    print(f"  price_stats: {len(rows)} 銘柄更新")
    return len(rows)


if __name__ == "__main__":
    n = run()
    print(f"完了: {n} 銘柄")
