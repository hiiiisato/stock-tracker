"""
銘柄マスタ・取引カレンダーの日次更新
"""
from __future__ import annotations
import re
import time
import requests
from datetime import date, timedelta
from config import get_conn, JQUANTS_BASE_URL, JQUANTS_HEADERS, bulk_upsert

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
YAHOO_HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; stock-tracker/1.0)"}


def update_stock_master() -> int:
    """銘柄マスタを最新状態に更新。新規上場・廃止・情報変更を反映する。"""
    r = requests.get(f"{JQUANTS_BASE_URL}/equities/master", headers=JQUANTS_HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()["data"]

    conn = get_conn()
    cur = conn.cursor()

    # 市場マスタ更新
    markets = list(set((d["Mkt"], d["MktNm"]) for d in data))
    bulk_upsert(cur, "markets", ["code", "name"], markets, update_cols=["name"])

    # 業種マスタ更新
    sectors = list(set((d["S33"], d["S33Nm"]) for d in data))
    bulk_upsert(cur, "sectors", ["code", "name"], sectors, update_cols=["name"])

    conn.commit()

    cur.execute("SELECT id, code FROM markets")
    market_map = {r[1]: r[0] for r in cur.fetchall()}
    cur.execute("SELECT id, code FROM sectors")
    sector_map = {r[1]: r[0] for r in cur.fetchall()}

    # APIが返す現在の全コード（4桁）
    active_codes = set()
    rows = []
    seen = set()
    for d in data:
        code4 = d["Code"][:4]
        if code4 in seen:
            continue
        seen.add(code4)
        active_codes.add(code4)
        rows.append((
            code4,
            d["CoName"],
            d["CoNameEn"] or None,
            market_map.get(d["Mkt"]),
            sector_map.get(d["S33"]),
            True,
        ))

    # アクティブ銘柄をUPSERT
    bulk_upsert(cur, "stocks",
        ["code", "name", "name_en", "market_id", "sector_id", "is_active"],
        rows,
        update_cols=["name", "name_en", "market_id", "sector_id", "is_active"])

    # APIに存在しない銘柄 → 上場廃止フラグ
    cur.execute("SELECT code FROM stocks WHERE is_active = TRUE")
    db_active = {r[0] for r in cur.fetchall()}
    delisted = db_active - active_codes
    if delisted:
        placeholders = ",".join(["%s"] * len(delisted))
        cur.execute(
            f"UPDATE stocks SET is_active=FALSE, delisted_date=CURDATE() WHERE code IN ({placeholders})",
            list(delisted),
        )
        print(f"  上場廃止フラグ更新: {len(delisted)} 銘柄")

    conn.commit()
    cur.close()
    conn.close()

    # J-Quants未収録の新規上場をYahoo Financeで補完
    n_new = _scan_new_alpha_listings()
    if n_new:
        print(f"  Yahoo補完: {n_new} 件の新規上場を追加")

    return len(rows) + n_new


def _yahoo_stock_info(code4: str) -> dict | None:
    """Yahoo Finance で銘柄の存在・名称・市場区分を確認。存在しなければ None。"""
    ticker = f"{code4}.T"
    try:
        r = requests.get(
            YAHOO_CHART_URL.format(ticker=ticker),
            params={"interval": "1d", "range": "5d"},
            headers=YAHOO_HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            return None
        result = r.json().get("chart", {}).get("result")
        if not result:
            return None
        meta = result[0].get("meta", {})
        # 日本株以外を除外（exchangeTimezoneName で判定）
        if "Tokyo" not in meta.get("exchangeTimezoneName", ""):
            return None
        name = meta.get("longName") or meta.get("shortName") or code4
        return {"code": code4, "name": name}
    except Exception:
        return None


def _scan_new_alpha_listings(max_scan: int = 200, max_consecutive_miss: int = 50) -> int:
    """
    J-Quants masterに未収録の新規上場（数字+アルファベットコード）をYahoo Financeでスキャン。
    DBの最大コード+1から順に試し、連続 max_consecutive_miss 回失敗したら打ち切る。
    見つかった銘柄を stocks テーブルへ登録する（market_id は翌日 J-Quants 反映後に更新）。
    """
    conn = get_conn()
    cur = conn.cursor()

    # DB内の最大アルファベット付きコードを取得
    cur.execute("SELECT MAX(code) FROM stocks WHERE code REGEXP '^[0-9]+[A-Z]$'")
    row = cur.fetchone()
    max_code = row[0] if row else None

    # スキャン不要ならスキップ
    if not max_code:
        cur.close()
        conn.close()
        return 0

    # 既存コードセット（重複登録防止）
    cur.execute("SELECT code FROM stocks")
    existing = {r[0] for r in cur.fetchall()}
    cur.close()
    conn.close()

    # 数値部分とアルファベット部分を分離（例: "552A" → 552, "A"）
    m = re.match(r'^(\d+)([A-Z])$', max_code)
    if not m:
        return 0
    num    = int(m.group(1))
    letter = m.group(2)

    new_rows        = []
    consecutive_miss = 0

    for i in range(1, max_scan + 1):
        candidate = f"{num + i}{letter}"
        if candidate in existing:
            consecutive_miss = 0
            continue

        time.sleep(0.15)  # Yahoo Finance レート制限対策
        info = _yahoo_stock_info(candidate)

        if info:
            new_rows.append((candidate, info["name"], None, None, None, True))
            consecutive_miss = 0
            print(f"    新規上場検出: {candidate}  {info['name']}")
        else:
            consecutive_miss += 1
            if consecutive_miss >= max_consecutive_miss:
                break

    if not new_rows:
        return 0

    conn = get_conn()
    cur  = conn.cursor()
    # market_id / sector_id は NULL のまま登録。
    # 翌日以降 J-Quants が反映した時点で update_stock_master() が自動的に更新する。
    bulk_upsert(
        cur, "stocks",
        ["code", "name", "name_en", "market_id", "sector_id", "is_active"],
        new_rows,
        update_cols=["name", "is_active"],
    )
    conn.commit()
    cur.close()
    conn.close()

    return len(new_rows)


def update_trading_calendar(date_from: date = None, date_to: date = None) -> int:
    """取引カレンダーを更新する。デフォルトは2024-03-30〜1年後。"""
    if date_from is None:
        date_from = date(2024, 3, 30)
    if date_to is None:
        date_to = date.today() + timedelta(days=365)

    params = {
        "date_from": date_from.strftime("%Y-%m-%d"),
        "date_to":   date_to.strftime("%Y-%m-%d"),
    }
    r = requests.get(f"{JQUANTS_BASE_URL}/markets/calendar", headers=JQUANTS_HEADERS, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()["data"]

    # HolDiv: "0"=休場, "1"=取引あり
    rows = [(d["Date"], d["HolDiv"] == "0") for d in data]

    conn = get_conn()
    cur = conn.cursor()
    bulk_upsert(cur, "trading_calendar", ["date", "is_holiday"], rows, update_cols=["is_holiday"])
    conn.commit()
    cur.close()
    conn.close()
    return len(rows)


if __name__ == "__main__":
    print("=== 銘柄マスタ更新 ===")
    n = update_stock_master()
    print(f"  {n} 銘柄を更新")

    print("=== 取引カレンダー更新 ===")
    n = update_trading_calendar()
    print(f"  {n} 日分を更新")
