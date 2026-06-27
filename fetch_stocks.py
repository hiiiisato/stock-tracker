"""
日本株価データを取得してSupabaseに保存するスクリプト
"""
import os
from datetime import date, timedelta
import pandas as pd
import yfinance as yf
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# 日経225構成銘柄の一部（テスト用）
# 本番では全銘柄リストをCSVや外部ソースから取得する
SAMPLE_STOCKS = {
    "7203": "トヨタ自動車",
    "6758": "ソニーグループ",
    "9984": "ソフトバンクグループ",
    "8306": "三菱UFJフィナンシャル",
    "6861": "キーエンス",
    "4063": "信越化学工業",
    "8035": "東京エレクトロン",
    "4519": "中外製薬",
    "9433": "KDDI",
    "7741": "HOYA",
    "6098": "リクルートホールディングス",
    "2413": "エムスリー",
    "6367": "ダイキン工業",
    "6501": "日立製作所",
    "9432": "日本電信電話",
    "4502": "武田薬品工業",
    "7267": "本田技研工業",
    "6902": "デンソー",
    "8058": "三菱商事",
    "3382": "セブン＆アイ・ホールディングス",
}


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def upsert_stocks(supabase: Client):
    """銘柄マスタを登録"""
    rows = [{"code": code, "name": name} for code, name in SAMPLE_STOCKS.items()]
    result = supabase.table("stocks").upsert(rows, on_conflict="code").execute()
    print(f"銘柄マスタ: {len(rows)}件登録")
    return result


def fetch_and_store_prices(supabase: Client, days: int = 30):
    """過去N日分の価格データを取得してDBに保存"""
    end = date.today()
    start = end - timedelta(days=days)

    tickers = [f"{code}.T" for code in SAMPLE_STOCKS.keys()]
    print(f"価格データ取得中: {start} 〜 {end}")

    raw = yf.download(tickers, start=start, end=end, progress=False)

    if raw.empty:
        print("データが取得できませんでした")
        return

    close = raw["Close"]
    rows = []

    for ticker in close.columns:
        code = ticker.replace(".T", "")
        series = close[ticker].dropna()

        prev_close = None
        for dt, price in series.items():
            change_pct = None
            if prev_close is not None and prev_close > 0:
                change_pct = round((price - prev_close) / prev_close * 100, 4)

            rows.append({
                "code": code,
                "date": str(dt.date()),
                "close": round(float(price), 2),
                "change_pct": change_pct,
            })
            prev_close = float(price)

    result = supabase.table("daily_prices").upsert(
        rows, on_conflict="code,date"
    ).execute()
    print(f"価格データ: {len(rows)}件保存")
    return result


def compute_daily_rankings(supabase: Client, target_date: date = None):
    """指定日の上昇率ランキングを作成"""
    if target_date is None:
        target_date = date.today() - timedelta(days=1)  # 前営業日

    result = supabase.table("daily_prices") \
        .select("code, change_pct, stocks(name)") \
        .eq("date", str(target_date)) \
        .not_.is_("change_pct", "null") \
        .order("change_pct", desc=True) \
        .limit(15) \
        .execute()

    rows = []
    for rank, item in enumerate(result.data, start=1):
        rows.append({
            "date": str(target_date),
            "rank": rank,
            "code": item["code"],
            "change_pct": item["change_pct"],
        })

    if rows:
        supabase.table("daily_rankings").upsert(
            rows, on_conflict="date,rank"
        ).execute()
        print(f"\n{target_date} 上昇率ランキング TOP{len(rows)}")
        print("-" * 40)
        for r in rows:
            name = next(
                (v["name"] for v in result.data if v["code"] == r["code"]),
                r["code"]
            )
            print(f"{r['rank']:2d}位  {r['code']} {name:<20} {r['change_pct']:+.2f}%")
    else:
        print(f"{target_date} のランキングデータなし（休場日の可能性）")

    return rows


def compute_weekly_rankings(supabase: Client):
    """直近1週間の上昇率ランキングを作成"""
    today = date.today()
    week_ending = today - timedelta(days=today.weekday() + 1)  # 直近の金曜日
    week_start = week_ending - timedelta(days=4)

    print(f"\n週次ランキング対象: {week_start} 〜 {week_ending}")

    result = supabase.table("daily_prices") \
        .select("code, date, close") \
        .gte("date", str(week_start)) \
        .lte("date", str(week_ending)) \
        .execute()

    if not result.data:
        print("データなし")
        return []

    df = pd.DataFrame(result.data)
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = df["close"].astype(float)

    weekly = (
        df.sort_values("date")
        .groupby("code")
        .apply(lambda g: (g.iloc[-1]["close"] - g.iloc[0]["close"]) / g.iloc[0]["close"] * 100
               if len(g) >= 2 else None)
        .dropna()
        .sort_values(ascending=False)
        .head(15)
        .reset_index()
    )
    weekly.columns = ["code", "change_pct_1w"]

    rows = []
    for rank, row in weekly.iterrows():
        rows.append({
            "week_ending": str(week_ending),
            "rank": rank + 1,
            "code": row["code"],
            "change_pct_1w": round(float(row["change_pct_1w"]), 4),
        })

    if rows:
        supabase.table("weekly_rankings").upsert(
            rows, on_conflict="week_ending,rank"
        ).execute()
        print(f"週次ランキング: {len(rows)}件保存")

    return rows


if __name__ == "__main__":
    supabase = get_supabase()

    print("=== Step 1: 銘柄マスタ登録 ===")
    upsert_stocks(supabase)

    print("\n=== Step 2: 価格データ取得（過去30日） ===")
    fetch_and_store_prices(supabase, days=30)

    print("\n=== Step 3: 日次ランキング作成 ===")
    compute_daily_rankings(supabase)

    print("\n=== Step 4: 週次ランキング作成 ===")
    compute_weekly_rankings(supabase)
