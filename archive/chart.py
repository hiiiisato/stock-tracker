#!/usr/bin/env python3
from __future__ import annotations
"""
銘柄の株価チャートを生成してブラウザで表示するツール

使い方:
  python chart.py 3086          # 直近3ヶ月（デフォルト）
  python chart.py トヨタ
  python chart.py 3086 6        # 直近6ヶ月
  python chart.py 3086 1        # 直近1ヶ月
  python chart.py 3086 3086 8136 5803   # 複数銘柄を重ねて表示
"""

import sys
import os
import subprocess
from config import get_conn
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def find_stock(query: str):
    conn = get_conn()
    cur = conn.cursor()
    if query.isdigit():
        cur.execute("SELECT code, name FROM stocks WHERE code = %s AND is_active = TRUE", (query,))
    else:
        cur.execute(
            "SELECT code, name FROM stocks WHERE name LIKE %s AND is_active = TRUE ORDER BY LENGTH(name) LIMIT 1",
            (f"%{query}%",)
        )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return {"code": row[0], "name": row[1]} if row else None


def get_ohlcv(code: str, months: int = 3) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    limit = months * 23  # 月あたり約23営業日
    cur.execute("""
        SELECT date, open, high, low, close, volume
        FROM daily_prices
        WHERE code = %s
        ORDER BY date DESC
        LIMIT %s
    """, (code, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "date": r[0], "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": int(r[5]),
        }
        for r in reversed(rows)
    ]


def single_chart(stock: dict, months: int, out_path: str):
    """1銘柄のローソク足 + 出来高チャート"""
    data = get_ohlcv(stock["code"], months)
    if not data:
        print(f"データなし: {stock['code']}")
        return

    dates  = [d["date"]   for d in data]
    opens  = [d["open"]   for d in data]
    highs  = [d["high"]   for d in data]
    lows   = [d["low"]    for d in data]
    closes = [d["close"]  for d in data]
    vols   = [d["volume"] for d in data]
    bar_colors = ["#E84040" if c >= o else "#2CA02C" for o, c in zip(opens, closes)]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.75, 0.25],
    )
    fig.add_trace(go.Candlestick(
        x=dates, open=opens, high=highs, low=lows, close=closes,
        name="株価",
        increasing_line_color="#E84040",
        decreasing_line_color="#2CA02C",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=dates, y=vols, name="出来高",
        marker_color=bar_colors, opacity=0.7,
    ), row=2, col=1)

    fig.update_layout(
        title=f"{stock['name']}（{stock['code']}）  直近{months}ヶ月チャート",
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        height=620,
        font=dict(size=13),
        legend=dict(orientation="h", y=1.02),
    )
    fig.update_yaxes(title_text="株価（円）", row=1, col=1)
    fig.update_yaxes(title_text="出来高", row=2, col=1)
    fig.write_html(out_path)


def multi_chart(stocks: list[dict], months: int, out_path: str):
    """複数銘柄の終値を正規化して重ね描き（基準日=1.0）"""
    fig = go.Figure()
    for stock in stocks:
        data = get_ohlcv(stock["code"], months)
        if not data:
            continue
        base = data[0]["close"]
        dates  = [d["date"]             for d in data]
        normed = [d["close"] / base * 100 for d in data]
        fig.add_trace(go.Scatter(
            x=dates, y=normed,
            mode="lines",
            name=f"{stock['name']}（{stock['code']}）",
        ))

    labels = "・".join(f"{s['name']}({s['code']})" for s in stocks)
    fig.update_layout(
        title=f"比較チャート（期間基準=100）  {labels}  直近{months}ヶ月",
        xaxis_title="日付",
        yaxis_title="相対値（期間初=100）",
        template="plotly_dark",
        height=520,
        font=dict(size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.write_html(out_path)


def open_browser(path: str):
    subprocess.run(["open", path], check=False)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    # 末尾が数字1〜2桁 → 月数指定
    months = 3
    if args[-1].isdigit() and len(args[-1]) <= 2:
        months = int(args[-1])
        args = args[:-1]

    if not args:
        print("銘柄コードまたは銘柄名を指定してください")
        sys.exit(1)

    stocks = []
    for a in args:
        s = find_stock(a)
        if s:
            stocks.append(s)
        else:
            print(f"銘柄が見つかりません: {a}")

    if not stocks:
        sys.exit(1)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    if len(stocks) == 1:
        out = os.path.join(base_dir, f"chart_{stocks[0]['code']}.html")
        single_chart(stocks[0], months, out)
        print(f"チャート生成: {out}")
    else:
        codes = "_".join(s["code"] for s in stocks)
        out = os.path.join(base_dir, f"chart_{codes}.html")
        multi_chart(stocks, months, out)
        print(f"比較チャート生成: {out}")

    open_browser(out)


if __name__ == "__main__":
    main()
