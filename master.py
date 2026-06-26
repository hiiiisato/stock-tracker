"""
銘柄マスタ・取引カレンダーの日次更新
"""
import requests
from datetime import date, timedelta
from config import get_conn, JQUANTS_BASE_URL, JQUANTS_HEADERS, bulk_upsert


def update_stock_master() -> int:
    """銘柄マスタを最新状態に更新。新規上場・廃止・情報変更を反映する。"""
    r = requests.get(f"{JQUANTS_BASE_URL}/equities/master", headers=JQUANTS_HEADERS)
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
    return len(rows)


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
    r = requests.get(f"{JQUANTS_BASE_URL}/markets/calendar", headers=JQUANTS_HEADERS, params=params)
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
