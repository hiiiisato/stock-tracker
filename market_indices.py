"""
主要市場指数データ取得・管理モジュール
Yahoo Finance chart API から各指数を取得して DB に保存する。

対象指数 (INDEX_CONFIGS):
  ^N225      日経平均
  1306.T     TOPIX（ETF proxy）
  2516.T     グロース250（ETF proxy）
  ^DJI       NYダウ
  ^GSPC      S&P 500
  ^IXIC      NASDAQ
  USDJPY=X   ドル円
  000001.SS  上海総合

使い方:
  python market_indices.py          # 全指数を差分更新
  python market_indices.py --init   # 1年分を初回取得
  python market_indices.py --show   # 最新値を表示
"""

import sys
import time
import requests
from urllib.parse import quote
from datetime import date, datetime, timedelta
from config import get_conn

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# ══════════════════════════════════════════════════════════
#  指数設定 — 追加・変更はここだけ編集する
# ══════════════════════════════════════════════════════════
INDEX_CONFIGS = {
    "^N225":     {"name": "日経平均",   "group": "japan", "decimals": 2, "note": ""},
    "1306.T":    {"name": "TOPIX",     "group": "japan", "decimals": 2, "note": "ETF"},
    "2516.T":    {"name": "グロース250","group": "japan", "decimals": 2, "note": "ETF"},
    "^DJI":      {"name": "NYダウ",    "group": "us",    "decimals": 2, "note": ""},
    "^GSPC":     {"name": "S&P 500",   "group": "us",    "decimals": 2, "note": ""},
    "^IXIC":     {"name": "NASDAQ",    "group": "us",    "decimals": 2, "note": ""},
    "USDJPY=X":  {"name": "ドル円",    "group": "fx",    "decimals": 2, "note": ""},
    "000001.SS": {"name": "上海総合",  "group": "asia",  "decimals": 2, "note": ""},
}

CHART_API = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
SUMMARY_API = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}"


# ══════════════════════════════════════════════════════════
#  DB 初期化
# ══════════════════════════════════════════════════════════

def ensure_table():
    """market_index_prices テーブルが存在しなければ作成する。"""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_index_prices (
            symbol      VARCHAR(20)   NOT NULL,
            date        DATE          NOT NULL,
            open        DECIMAL(14,4),
            high        DECIMAL(14,4),
            low         DECIMAL(14,4),
            close       DECIMAL(14,4),
            volume      BIGINT,
            change_pct  DECIMAL(8,4),
            PRIMARY KEY (symbol, date),
            INDEX idx_mip_date (date),
            INDEX idx_mip_symbol (symbol)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════════════════════
#  Yahoo Finance 接続
# ══════════════════════════════════════════════════════════

def _get_session_and_crumb():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get("https://fc.yahoo.com", timeout=10)
    r = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
    return session, r.text.strip()


def _encode_symbol(sym: str) -> str:
    """^N225 → %5EN225 のように URL に使えるシンボルに変換。"""
    return quote(sym, safe="")


# ══════════════════════════════════════════════════════════
#  データ取得
# ══════════════════════════════════════════════════════════

def _fetch_history(sym: str, session: requests.Session, crumb: str,
                   days: int = 400) -> list:
    """
    指定シンボルの OHLCV 履歴を取得して辞書リストで返す。
    戻り値: [{"date": date, "open": float, "high": float, "low": float,
              "close": float, "volume": int, "change_pct": float}, ...]
    """
    end_ts   = int(datetime.now().timestamp())
    start_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    url = CHART_API.format(sym=_encode_symbol(sym))
    params = {
        "interval": "1d",
        "period1":  start_ts,
        "period2":  end_ts,
        "crumb":    crumb,
    }
    try:
        r = session.get(url, params=params, timeout=20)
        data = r.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return []
        chart = result[0]
        timestamps = chart.get("timestamp", [])
        quote      = chart.get("indicators", {}).get("quote", [{}])[0]
        opens  = quote.get("open",   [])
        highs  = quote.get("high",   [])
        lows   = quote.get("low",    [])
        closes = quote.get("close",  [])
        vols   = quote.get("volume", [])

        rows = []
        prev_close = None
        for i, ts in enumerate(timestamps):
            c = closes[i] if i < len(closes) else None
            if c is None:
                prev_close = None
                continue
            o = opens[i]  if i < len(opens)  else None
            h = highs[i]  if i < len(highs)  else None
            l = lows[i]   if i < len(lows)   else None
            v = vols[i]   if i < len(vols)   else None
            chg_pct = round((c - prev_close) / prev_close * 100, 4) if prev_close else None
            rows.append({
                "date":       datetime.fromtimestamp(ts).date(),
                "open":       round(float(o), 4) if o is not None else None,
                "high":       round(float(h), 4) if h is not None else None,
                "low":        round(float(l), 4) if l is not None else None,
                "close":      round(float(c), 4),
                "volume":     int(v) if v is not None else None,
                "change_pct": chg_pct,
            })
            prev_close = c
        return rows
    except Exception as e:
        print(f"  [indices] {sym}: {e}")
        return []


# ══════════════════════════════════════════════════════════
#  DB 書き込み
# ══════════════════════════════════════════════════════════

def _upsert(sym: str, rows: list):
    if not rows:
        return
    conn = get_conn()
    cur  = conn.cursor()
    cur.executemany("""
        INSERT INTO market_index_prices
          (symbol, date, open, high, low, close, volume, change_pct)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          open=VALUES(open), high=VALUES(high), low=VALUES(low),
          close=VALUES(close), volume=VALUES(volume), change_pct=VALUES(change_pct)
    """, [(sym, r["date"], r["open"], r["high"], r["low"],
           r["close"], r["volume"], r["change_pct"]) for r in rows])
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════════════════════
#  公開 API
# ══════════════════════════════════════════════════════════

def fetch_and_store(init: bool = False) -> int:
    """
    全指数を取得して DB に保存する。
    init=True のとき 1 年分を全件取得、False のとき直近 7 日分の差分更新。
    """
    ensure_table()
    days = 400 if init else 10
    session, crumb = _get_session_and_crumb()
    print(f"  指数データ取得: {'初回1年分' if init else '差分7日'} / {len(INDEX_CONFIGS)} 指数")
    total = 0
    for sym, cfg in INDEX_CONFIGS.items():
        rows = _fetch_history(sym, session, crumb, days=days)
        if rows:
            _upsert(sym, rows)
            total += len(rows)
            print(f"    {cfg['name']:10} ({sym}) - {len(rows)} 件")
        else:
            print(f"    {cfg['name']:10} ({sym}) - データなし")
        time.sleep(0.3)
    return total


def get_latest_values() -> list:
    """
    各指数の最新値（終値・前日比）を返す。
    戻り値: [{"symbol": str, "name": str, "close": float,
               "change_pct": float, "date": date, "note": str}, ...]
    """
    conn = get_conn()
    cur  = conn.cursor()
    results = []
    for sym, cfg in INDEX_CONFIGS.items():
        cur.execute("""
            SELECT close, change_pct, date
            FROM market_index_prices
            WHERE symbol = %s AND close IS NOT NULL
            ORDER BY date DESC
            LIMIT 1
        """, (sym,))
        row = cur.fetchone()
        if row:
            results.append({
                "symbol":     sym,
                "name":       cfg["name"],
                "group":      cfg["group"],
                "close":      float(row[0]),
                "change_pct": float(row[1]) if row[1] is not None else None,
                "date":       row[2],
                "note":       cfg["note"],
                "decimals":   cfg["decimals"],
            })
    cur.close()
    conn.close()
    return results


def _split_factor_map(cur, sym: str, from_date) -> dict:
    """ETF(.T)の株式分割・併合の調整係数を daily_prices から取得する。
    daily_prices は J-Quants公式AdjustmentFactorで adj_close が調整済み。
    factor(日) = adj_close / close（併合前<1・調整不要日=1）を返し、生OHLCに掛けて調整する。
    market_index_prices は生値OHLCのため、これを掛けないと併合日でチャートが崩れる
    （例: 1306は2026-03-30に10:1併合し 3827→376 に段差）。"""
    code = sym[:-2] if sym.endswith(".T") else sym   # "1306.T" -> "1306"
    cur.execute("""
        SELECT date, close, adj_close FROM daily_prices
        WHERE code = %s AND date >= %s AND close IS NOT NULL AND close > 0 AND adj_close IS NOT NULL
    """, (code, from_date))
    return {r[0]: float(r[2]) / float(r[1]) for r in cur.fetchall() if r[1]}


def get_history_for_chart(days: int = 90) -> dict:
    """
    ローソク足チャート用に各指数の直近 N 日間の OHLC データを返す。
    戻り値: {symbol: {"name": str, "dates": [...], "opens": [...],
                      "highs": [...], "lows": [...], "closes": [...]}, ...}
    ※ 国内ETF(1306/2516等 .T)は株式分割・併合を J-Quants公式係数で調整して返す。
    """
    from_date = date.today() - timedelta(days=days)
    conn = get_conn()
    cur  = conn.cursor()
    result = {}
    for sym, cfg in INDEX_CONFIGS.items():
        cur.execute("""
            SELECT date, open, high, low, close
            FROM market_index_prices
            WHERE symbol = %s AND date >= %s AND close IS NOT NULL
            ORDER BY date
        """, (sym, from_date))
        rows = cur.fetchall()
        if not rows:
            continue
        # ETFは分割・併合を調整（indexや為替は対象外＝係数マップが空で恒等）
        fmap = _split_factor_map(cur, sym, from_date) if sym.endswith(".T") else {}

        def _adj(r, i):
            v = r[i] if r[i] is not None else r[4]
            return round(float(v) * fmap.get(r[0], 1.0), 4)

        result[sym] = {
            "name":   cfg["name"],
            "dates":  [str(r[0]) for r in rows],
            "opens":  [_adj(r, 1) for r in rows],
            "highs":  [_adj(r, 2) for r in rows],
            "lows":   [_adj(r, 3) for r in rows],
            "closes": [_adj(r, 4) for r in rows],
        }
    cur.close()
    conn.close()
    return result


if __name__ == "__main__":
    ensure_table()
    init = "--init" in sys.argv
    if "--show" in sys.argv:
        vals = get_latest_values()
        for v in vals:
            arrow = "▲" if (v["change_pct"] or 0) > 0 else "▼"
            pct   = v["change_pct"] or 0
            print(f"  {v['name']:10} {v['close']:12,.2f}  {arrow}{abs(pct):.2f}%  ({v['date']})")
    else:
        n = fetch_and_store(init=init)
        print(f"  完了: 合計 {n:,} 件保存")
