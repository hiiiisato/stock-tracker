#!/usr/bin/env python3
from __future__ import annotations
"""
株式テーマ分析 Web アプリ（Flask）

Routes:
  GET /                     最新テーマレポートへリダイレクト
  GET /report/<YYYY-MM-DD>  テーマ別資金フローレポート
  GET /stock/<code>         銘柄詳細ページ
  GET /health               ヘルスチェック（Render 死活監視用）

起動:
  python app.py                  # 開発サーバー（localhost:5000）
  gunicorn app:app -w 2          # 本番（Render）
"""

import time
import threading
from datetime import date, timedelta

from flask import Flask, abort, redirect
import plotly.graph_objects as go

from config import get_conn
from theme_report import generate_report

app = Flask(__name__)

# ─── 簡易インメモリキャッシュ（TTL 付き） ──────────────────
_cache: dict[str, dict] = {}
_lock  = threading.Lock()
CACHE_TTL = 3600  # 1時間


def _get(key: str):
    with _lock:
        e = _cache.get(key)
        return e["v"] if e and time.time() - e["t"] < CACHE_TTL else None


def _set(key: str, val):
    with _lock:
        _cache[key] = {"v": val, "t": time.time()}


def _bust(key: str):
    with _lock:
        _cache.pop(key, None)


# ─── DB ヘルパー ─────────────────────────────────────────

def _latest_report_date() -> date | None:
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT MAX(date) FROM theme_daily_stats")
    d = cur.fetchone()[0]
    cur.close()
    conn.close()
    return d


# ══════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════

@app.route("/health")
def health():
    """Render のヘルスチェック用。DB 疎通も確認。"""
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        return "OK"
    except Exception as e:
        return f"NG: {e}", 503


@app.route("/")
def index():
    d = _latest_report_date()
    return redirect(f"/report/{d}") if d else ("データなし", 404)


@app.route("/report/<date_str>")
def report(date_str: str):
    try:
        report_date = date.fromisoformat(date_str)
    except ValueError:
        abort(400)

    key  = f"report_{date_str}"
    html = _get(key)
    if not html:
        print(f"[app] レポート生成: {date_str}")
        html = generate_report(report_date)  # HTML 文字列を返す
        _set(key, html)
    return html


@app.route("/stock/<code>")
def stock_detail(code: str):
    key  = f"stock_{code}"
    html = _get(key)
    if not html:
        html = _build_stock_page(code)
        _set(key, html)
    return html


# ══════════════════════════════════════════════════════════
#  銘柄詳細ページ生成
# ══════════════════════════════════════════════════════════

_REL = {
    3: ("コア", "#ffa657", "#3d1f00"),
    2: ("関連", "#56d364", "#122012"),
    1: ("周辺", "#6e7681", "#1c2128"),
}


def _build_stock_page(code: str) -> str:
    conn = get_conn()
    cur  = conn.cursor()

    # 基本情報
    cur.execute("""
        SELECT s.code, s.name, m.name AS market, sec.name AS sector
        FROM stocks s
        LEFT JOIN markets  m   ON s.market_id  = m.id
        LEFT JOIN sectors  sec ON s.sector_id  = sec.id
        WHERE s.code = %s
    """, (code,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        abort(404)
    s_code, s_name, market, sector = row

    # 直近3ヶ月の価格
    from_dt = date.today() - timedelta(days=92)
    cur.execute("""
        SELECT date, open, high, low, close, volume, change_pct
        FROM daily_prices
        WHERE code = %s AND date >= %s AND close IS NOT NULL
        ORDER BY date
    """, (code, from_dt))
    prices = cur.fetchall()

    # テーマ所属
    cur.execute("""
        SELECT tc.name, tc.code, st.relevance
        FROM stock_themes st
        JOIN theme_categories tc ON st.theme_id = tc.id
        WHERE st.code = %s AND tc.level = 2
        ORDER BY tc.sort_order
    """, (code,))
    themes = cur.fetchall()

    cur.close()
    conn.close()

    # 最新価格・騰落
    if prices:
        latest     = prices[-1]
        price_str  = f"{float(latest[4] or 0):,.0f}"
        chg        = float(latest[6] or 0)
    else:
        price_str, chg = "-", 0.0

    chg_style = "color:#E84040" if chg > 0 else ("color:#3A9FE0" if chg < 0 else "color:#c9d1d9")

    # ローソク足チャート（Plotly、Plotly.js CDN 埋め込み）
    if prices:
        dates  = [p[0]           for p in prices]
        opens  = [float(p[1] or 0) for p in prices]
        highs  = [float(p[2] or 0) for p in prices]
        lows   = [float(p[3] or 0) for p in prices]
        closes = [float(p[4] or 0) for p in prices]

        fig = go.Figure(go.Candlestick(
            x=dates, open=opens, high=highs, low=lows, close=closes,
            increasing_line_color="#E84040",
            decreasing_line_color="#3A9FE0",
            name="株価",
        ))
        fig.update_layout(
            template="plotly_dark", height=380,
            margin=dict(l=50, r=10, t=20, b=30),
            xaxis_rangeslider_visible=False,
            font=dict(size=12),
        )
        chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn",
                                 config={"responsive": True})
    else:
        chart_html = '<p style="color:#8b949e;padding:20px">価格データなし</p>'

    # テーマバッジ（クリックでテーマレポートへ）
    report_date = _latest_report_date()
    report_link = f"/report/{report_date}" if report_date else "/"
    badges = []
    for tname, tcode, rel in themes:
        lbl, fg, bg = _REL.get(rel, ("?", "#aaa", "#222"))
        badges.append(
            f'<a href="{report_link}" style="text-decoration:none">'
            f'<span style="display:inline-flex;align-items:center;gap:6px;'
            f'background:{bg};border-radius:6px;padding:4px 10px;margin:3px">'
            f'<span style="color:{fg};font-size:11px;font-weight:700">{lbl}</span>'
            f'<span style="color:#c9d1d9;font-size:13px">{tname}</span>'
            f'</span></a>'
        )
    theme_html = "".join(badges) if badges else '<span style="color:#8b949e">なし</span>'

    # 直近20日テーブル
    recent20 = list(reversed(prices[-20:]))
    trows = ""
    for p in recent20:
        c = float(p[6] or 0)
        cs = "color:#E84040" if c > 0 else ("color:#3A9FE0" if c < 0 else "")
        trows += (
            f'<tr>'
            f'<td>{p[0]}</td>'
            f'<td style="text-align:right">{float(p[4] or 0):,.0f}</td>'
            f'<td style="text-align:right;{cs}">{c:+.1f}%</td>'
            f'<td style="text-align:right;color:#8b949e">{int(p[5] or 0):,}</td>'
            f'</tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{s_name}（{s_code}）</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0d1117;color:#c9d1d9;font-family:'Helvetica Neue',Arial,sans-serif;
          font-size:14px;line-height:1.6;padding-bottom:40px}}
    .wrap{{max-width:960px;margin:0 auto;padding:20px 16px}}
    .back{{color:#79c0ff;text-decoration:none;font-size:13px}}
    .back:hover{{text-decoration:underline}}
    h1{{font-size:22px;color:#e6edf3;margin:12px 0 4px}}
    .meta{{color:#8b949e;font-size:13px;margin-bottom:16px}}
    .price-row{{display:flex;align-items:baseline;gap:14px;margin-bottom:20px;flex-wrap:wrap}}
    .price{{font-size:36px;font-weight:700;color:#e6edf3}}
    .chg{{font-size:22px;font-weight:600}}
    h2{{font-size:15px;font-weight:600;color:#e6edf3;
        border-bottom:1px solid #30363d;padding-bottom:8px;margin:24px 0 12px}}
    .themes{{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px}}
    .table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
    table{{width:100%;border-collapse:collapse;background:#161b22;border-radius:8px;overflow:hidden}}
    th{{background:#21262d;padding:8px 12px;color:#8b949e;font-size:12px;text-align:right;white-space:nowrap}}
    th.left{{text-align:left}}
    td{{padding:7px 12px;border-bottom:1px solid #1c2128;font-size:13px;text-align:right;white-space:nowrap}}
    td.left{{text-align:left}}
    tr:last-child td{{border-bottom:none}}
    tr:hover{{background:#1c2128}}
    @media(max-width:768px){{
      .wrap{{padding:12px 10px}}
      h1{{font-size:18px}}
      .price{{font-size:28px}}
      .chg{{font-size:18px}}
      h2{{font-size:14px;margin:16px 0 8px}}
      th,td{{padding:5px 8px;font-size:12px}}
    }}
  </style>
</head>
<body>
<div class="wrap">
  <a class="back" href="{report_link}">← テーマレポートへ戻る</a>
  <h1>{s_name}</h1>
  <div class="meta">{s_code} &nbsp;|&nbsp; {market or "—"} &nbsp;|&nbsp; {sector or "—"}</div>

  <div class="price-row">
    <span class="price">{price_str}</span>
    <span class="chg" style="{chg_style}">{chg:+.1f}%</span>
    <span style="color:#8b949e;font-size:13px">（前日比）</span>
  </div>

  <h2>所属テーマ</h2>
  <div class="themes">{theme_html}</div>

  <h2>株価チャート（直近3ヶ月）</h2>
  {chart_html}

  <h2>直近20営業日</h2>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="left">日付</th>
          <th>終値</th>
          <th>前日比</th>
          <th>出来高</th>
        </tr>
      </thead>
      <tbody>{trows}</tbody>
    </table>
  </div>
</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════
#  起動
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
