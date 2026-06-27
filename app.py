#!/usr/bin/env python3
from __future__ import annotations
"""
株式テーマ分析 Web アプリ（Flask）

Routes:
  GET /                     ダッシュボード（ホーム）
  GET /report/<YYYY-MM-DD>  テーマ別資金フローレポート
  GET /rankings             値上がり/値下がりランキング
  GET /watchlist            ウォッチリスト（登録銘柄一覧）
  POST /watchlist/add       ウォッチリストへ追加
  POST /watchlist/remove    ウォッチリストから削除
  GET /stock/<code>         銘柄詳細ページ
  GET /health               ヘルスチェック（Render 死活監視用）
"""

import time
import threading
from datetime import date, timedelta, datetime

from flask import Flask, abort, redirect, request
import plotly.graph_objects as go

from config import get_conn
from theme_report import generate_report

app = Flask(__name__)

# ─── 簡易インメモリキャッシュ（TTL 付き） ───────────────────────────────────
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


def _bust_prefix(prefix: str):
    with _lock:
        keys = [k for k in _cache if k.startswith(prefix)]
        for k in keys:
            _cache.pop(k, None)


# ─── 共通 UI ────────────────────────────────────────────────────────────────

_BASE_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0d1117;
  color: #c9d1d9;
  font-family: 'Helvetica Neue', Arial, 'Hiragino Kaku Gothic ProN', sans-serif;
  font-size: 14px;
  line-height: 1.6;
  padding-bottom: 48px;
}
a { color: #79c0ff; text-decoration: none; }
a:hover { text-decoration: underline; }

/* ─ Navigation ─ */
.nav {
  background: #161b22;
  border-bottom: 1px solid #30363d;
  padding: 0 20px;
  position: sticky;
  top: 0;
  z-index: 200;
  display: flex;
  align-items: center;
  height: 48px;
  gap: 0;
}
.nav-logo {
  font-weight: 700;
  font-size: 15px;
  color: #e6edf3;
  text-decoration: none;
  margin-right: 20px;
  white-space: nowrap;
}
.nav-logo:hover { text-decoration: none; color: #e6edf3; }
.nav-links { display: flex; align-items: center; gap: 4px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.nav-links::-webkit-scrollbar { display: none; }
.nav-link {
  color: #8b949e;
  font-size: 13px;
  text-decoration: none;
  padding: 0 10px;
  height: 48px;
  display: flex;
  align-items: center;
  border-bottom: 2px solid transparent;
  white-space: nowrap;
}
.nav-link:hover { color: #e6edf3; text-decoration: none; }
.nav-link.active { color: #e6edf3; border-bottom-color: #1f6feb; }

/* ─ Page layout ─ */
.page { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
.page-header { margin-bottom: 24px; }
.page-title { font-size: 20px; font-weight: 600; color: #e6edf3; }
.page-subtitle { font-size: 13px; color: #8b949e; margin-top: 4px; }

/* ─ Cards ─ */
.card {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 8px;
  overflow: hidden;
}
.card-header {
  background: #21262d;
  padding: 10px 16px;
  font-size: 12px;
  font-weight: 600;
  color: #8b949e;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 1px solid #30363d;
}
.card-body { padding: 16px; }

/* ─ Grids ─ */
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
.grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-bottom: 24px; }
.grid-aside { display: grid; grid-template-columns: 1fr 340px; gap: 16px; margin-bottom: 24px; align-items: start; }

/* ─ Tables ─ */
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; border-collapse: collapse; }
th {
  background: #21262d;
  padding: 8px 12px;
  color: #8b949e;
  font-size: 12px;
  font-weight: 600;
  text-align: right;
  white-space: nowrap;
  border-bottom: 1px solid #30363d;
}
th.left { text-align: left; }
td {
  padding: 8px 12px;
  border-bottom: 1px solid #1c2128;
  font-size: 13px;
  text-align: right;
  white-space: nowrap;
}
td.left { text-align: left; }
tr:last-child td { border-bottom: none; }
tr:hover { background: #1c2128; }
.tbl-link { color: #c9d1d9; }
.tbl-link:hover { color: #79c0ff; text-decoration: underline; }

/* ─ Colors ─ */
.up  { color: #E84040; font-weight: 600; }
.dn  { color: #3A9FE0; font-weight: 600; }
.muted { color: #8b949e; }

/* ─ Badges ─ */
.badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 700;
  padding: 2px 7px;
  border-radius: 12px;
  vertical-align: middle;
}
.badge-hot  { background: #3d0a0a; color: #E84040; }
.badge-warm { background: #3d2000; color: #ffa657; }
.badge-cold { background: #0a1e3d; color: #3A9FE0; }
.badge-neu  { background: #1c2128; color: #8b949e; }

/* ─ Market bar ─ */
.market-bar-wrap {
  background: #1c2128;
  border-radius: 4px;
  height: 10px;
  display: flex;
  overflow: hidden;
  margin: 8px 0;
}
.mb-up   { background: #E84040; }
.mb-flat { background: #484f58; }
.mb-dn   { background: #3A9FE0; }

/* ─ Watchlist form ─ */
.form-row { display: flex; gap: 8px; margin-bottom: 16px; }
.form-row input[type=text] {
  flex: 1;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 8px 12px;
  color: #e6edf3;
  font-size: 14px;
  min-width: 0;
}
.form-row input[type=text]:focus {
  outline: none;
  border-color: #1f6feb;
}
.btn {
  background: #1f6feb;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 8px 16px;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
}
.btn:hover { background: #388bfd; }
.btn-sm {
  background: #21262d;
  color: #8b949e;
  border: 1px solid #30363d;
  border-radius: 4px;
  padding: 3px 8px;
  font-size: 12px;
  cursor: pointer;
}
.btn-sm:hover { background: #30363d; color: #e6edf3; }
.alert {
  background: #1c2128;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 12px 16px;
  color: #8b949e;
  font-size: 13px;
  margin-bottom: 16px;
}

/* ─ 主要指数カード ─ */
.idx-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 16px;
}
.idx-card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 12px 14px; position: relative; overflow: hidden;
}
.idx-card-label { font-size: 11px; color: #8b949e; margin-bottom: 4px; display: flex; gap: 5px; align-items: center; }
.idx-note { font-size: 9px; background: #21262d; color: #484f58; border-radius: 3px; padding: 1px 4px; }
.idx-value { font-size: 20px; font-weight: 700; color: #e6edf3; letter-spacing: -0.5px; line-height: 1.1; margin-bottom: 3px; }
.idx-chg { font-size: 12px; font-weight: 600; }
.idx-date { font-size: 10px; color: #484f58; margin-top: 3px; }
.idx-accent {
  position: absolute; top: 0; left: 0; width: 3px; height: 100%; border-radius: 8px 0 0 8px;
}
.idx-accent.up   { background: #E84040; }
.idx-accent.down { background: #3A9FE0; }
.idx-accent.flat { background: #484f58; }

/* ─ 指数チャート ─ */
.idx-chart-wrap {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  overflow: hidden; margin-bottom: 20px;
}
.idx-chart-tabs {
  display: flex; gap: 0; border-bottom: 1px solid #30363d; padding: 0 10px;
  overflow-x: auto; scrollbar-width: none;
}
.idx-chart-tabs::-webkit-scrollbar { display: none; }
.idx-tab {
  padding: 8px 14px; font-size: 12px; font-weight: 600; color: #8b949e;
  border: none; background: none; cursor: pointer; border-bottom: 2px solid transparent;
  white-space: nowrap; transition: color .15s;
}
.idx-tab:hover { color: #c9d1d9; }
.idx-tab.active { color: #58a6ff; border-bottom-color: #388bfd; }
.idx-chart-body { padding: 4px; }

/* ─ Responsive ─ */
@media (max-width: 768px) {
  .page { padding: 12px 10px; }
  .grid-3 { grid-template-columns: 1fr; gap: 12px; }
  .grid-2 { grid-template-columns: 1fr; gap: 12px; }
  .grid-aside { grid-template-columns: 1fr; gap: 12px; }
  .page-title { font-size: 17px; }
  th, td { padding: 6px 8px; font-size: 12px; }
  .nav-logo { font-size: 13px; margin-right: 12px; }
  .idx-grid { grid-template-columns: repeat(2, 1fr); }
  .idx-value { font-size: 16px; }
}
"""


def _nav(active: str = "") -> str:
    links = [
        ("home",      "/",           "ホーム"),
        ("themes",    "#",           "テーマ分析"),
        ("rankings",  "/rankings",   "ランキング"),
        ("events",    "/events",     "イベント"),
        ("watchlist", "/watchlist",  "ウォッチリスト"),
    ]
    items = []
    for key, href, label in links:
        cls = 'nav-link active' if key == active else 'nav-link'
        items.append(f'<a class="{cls}" href="{href}">{label}</a>')

    return f"""<nav class="nav">
  <a class="nav-logo" href="/">📈 株式テーマ分析</a>
  <div class="nav-links">{"".join(items)}</div>
</nav>"""


def _page_html(title: str, body: str, active: str = "", extra_head: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title} | 株式テーマ分析</title>
  <style>{_BASE_CSS}</style>
  {extra_head}
</head>
<body>
{_nav(active)}
<div class="page">
{body}
</div>
</body>
</html>"""


# ─── DB ヘルパー ─────────────────────────────────────────────────────────────

def _latest_report_date() -> date | None:
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT MAX(date) FROM theme_daily_stats")
    d = cur.fetchone()[0]
    cur.close()
    conn.close()
    return d


def _ensure_watchlist():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INT AUTO_INCREMENT PRIMARY KEY,
            code VARCHAR(10) NOT NULL,
            memo VARCHAR(200) DEFAULT '',
            added_at DATETIME DEFAULT NOW(),
            UNIQUE KEY uq_code (code)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ════════════════════════════════════════════════════════════════════════
#  ホーム（ダッシュボード）
# ════════════════════════════════════════════════════════════════════════

def _fmt_chg(v) -> str:
    if v is None:
        return "-"
    f = float(v)
    return f'<span class="{"up" if f > 0 else "dn" if f < 0 else "muted"}">{f:+.2f}%</span>'


def _build_index_section() -> str:
    """主要指数カードとチャートのHTMLを生成する。"""
    try:
        from market_indices import get_latest_values, get_history_for_chart
    except Exception:
        return ""

    values  = get_latest_values()
    history = get_history_for_chart(days=90)

    if not values:
        return ""

    # ─ 指数カード ─
    def _card(v: dict) -> str:
        pct  = v["change_pct"]
        cls  = "up" if pct and pct > 0 else ("down" if pct and pct < 0 else "flat")
        arrow = "▲" if cls == "up" else ("▼" if cls == "down" else "")
        pct_str = f"{arrow}{abs(pct):.2f}%" if pct is not None else "—"
        pct_color = "#E84040" if cls == "up" else ("#3A9FE0" if cls == "down" else "#484f58")

        val = v["close"]
        if val >= 10000:
            val_str = f"{val:,.0f}"
        elif val >= 100:
            val_str = f"{val:,.2f}"
        else:
            val_str = f"{val:.3f}"

        note_html = f'<span class="idx-note">{v["note"]}</span>' if v["note"] else ""
        return f"""<div class="idx-card">
  <div class="idx-accent {cls}"></div>
  <div class="idx-card-label">{v['name']} {note_html}</div>
  <div class="idx-value">{val_str}</div>
  <div class="idx-chg" style="color:{pct_color}">{pct_str}</div>
  <div class="idx-date">{v['date']}</div>
</div>"""

    cards_html = "".join(_card(v) for v in values)
    idx_grid = f'<div class="idx-grid">{cards_html}</div>'

    # ─ 切り替えチャート ─
    if not history:
        return idx_grid

    syms = list(history.keys())
    # タブボタン
    tabs_html = "".join(
        f'<button class="idx-tab{" active" if i == 0 else ""}" '
        f'onclick="switchIdx({i})" id="tab-{i}">'
        f'{history[s]["name"]}</button>'
        for i, s in enumerate(syms)
    )

    # Plotly ローソク足データを JS 埋め込み用に変換
    import json
    traces_js = []
    for s in syms:
        h = history[s]
        traces_js.append({
            "sym":    s,
            "name":   h["name"],
            "dates":  h["dates"],
            "opens":  h["opens"],
            "highs":  h["highs"],
            "lows":   h["lows"],
            "closes": h["closes"],
        })
    traces_data = json.dumps(traces_js, ensure_ascii=False)

    chart_html = f"""<div class="idx-chart-wrap">
  <div class="idx-chart-tabs">{tabs_html}</div>
  <div class="idx-chart-body" id="idx-chart-div" style="height:320px"></div>
</div>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js" charset="utf-8"></script>
<script>
(function() {{
  var TRACES = {traces_data};
  var layout = {{
    template: "plotly_dark",
    paper_bgcolor: "#161b22",
    plot_bgcolor: "#161b22",
    margin: {{l:55,r:10,t:10,b:30}},
    xaxis: {{
      showgrid: false,
      tickfont: {{size:10}},
      rangeslider: {{visible:false}},
      type: "category",
      nticks: 8,
    }},
    yaxis: {{
      showgrid: true,
      gridcolor: "#21262d",
      tickfont: {{size:10}},
    }},
    showlegend: false,
    height: 320,
  }};
  var config = {{responsive:true, displayModeBar:false}};

  function draw(idx) {{
    var t = TRACES[idx];
    var data = [{{
      type: "candlestick",
      x: t.dates,
      open:  t.opens,
      high:  t.highs,
      low:   t.lows,
      close: t.closes,
      increasing: {{line: {{color:"#E84040"}}, fillcolor:"#E84040"}},
      decreasing: {{line: {{color:"#3A9FE0"}}, fillcolor:"#3A9FE0"}},
      name: t.name,
    }}];
    Plotly.newPlot("idx-chart-div", data, layout, config);
    document.querySelectorAll(".idx-tab").forEach(function(b,i) {{
      b.classList.toggle("active", i === idx);
    }});
  }}

  window.switchIdx = function(idx) {{ draw(idx); }};
  draw(0);
}})();
</script>"""

    return f"""<div style="margin-bottom:20px">
  <div class="page-section-header" style="font-size:13px;font-weight:600;color:#8b949e;margin-bottom:10px;letter-spacing:0.5px;text-transform:uppercase">
    主要指数
  </div>
  {idx_grid}
  {chart_html}
</div>"""


def _build_home() -> str:
    conn = get_conn()
    cur  = conn.cursor()

    # 最新日付
    cur.execute("SELECT MAX(date) FROM daily_prices WHERE close IS NOT NULL")
    latest_date: date = cur.fetchone()[0]

    # 市場概況
    cur.execute("""
        SELECT
          SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) AS up,
          SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) AS dn,
          SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) AS flat
        FROM daily_prices
        WHERE date = %s AND close IS NOT NULL AND change_pct IS NOT NULL
    """, (latest_date,))
    row = cur.fetchone()
    up_cnt, dn_cnt, flat_cnt = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    total = up_cnt + dn_cnt + flat_cnt or 1

    up_pct   = up_cnt   / total * 100
    dn_pct   = dn_cnt   / total * 100
    flat_pct = flat_cnt / total * 100

    # テーマ過熱度（最新日）
    cur.execute("""
        SELECT tc.name, tc.code, tds.heat_score, tds.avg_change_pct, tds.breadth_ratio
        FROM theme_daily_stats tds
        JOIN theme_categories tc ON tds.theme_id = tc.id
        WHERE tds.date = (SELECT MAX(date) FROM theme_daily_stats) AND tc.level = 2
        ORDER BY tds.heat_score DESC
    """)
    themes = cur.fetchall()  # (name, code, heat, avg_change_pct, breadth_ratio)

    # 本日の値上がりTOP5（日次銘柄から直接取得）
    cur.execute("""
        SELECT dp.code, s.name, dp.close, dp.change_pct
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        WHERE dp.date = %s
          AND dp.close IS NOT NULL
          AND dp.change_pct IS NOT NULL
          AND s.is_active = TRUE
        ORDER BY dp.change_pct DESC
        LIMIT 5
    """, (latest_date,))
    gainers = cur.fetchall()

    # 本日の値下がりTOP5
    cur.execute("""
        SELECT dp.code, s.name, dp.close, dp.change_pct
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        WHERE dp.date = %s
          AND dp.close IS NOT NULL
          AND dp.change_pct IS NOT NULL
          AND s.is_active = TRUE
        ORDER BY dp.change_pct ASC
        LIMIT 5
    """, (latest_date,))
    losers = cur.fetchall()

    # ウォッチリスト（最新価格付き）
    cur.execute("""
        SELECT w.code, s.name, dp.close, dp.change_pct, w.added_at
        FROM watchlist w
        JOIN stocks s ON w.code = s.code
        LEFT JOIN daily_prices dp ON dp.code = w.code AND dp.date = %s
        ORDER BY dp.change_pct DESC
    """, (latest_date,))
    watchlist = cur.fetchall()

    cur.close()
    conn.close()

    # ─ テーマレポートリンク ─
    report_link = f"/report/{latest_date}" if latest_date else "/report"

    # ─── 市場概況カード ───────────────────────────────────────────
    market_card = f"""<div class="card">
  <div class="card-header">市場概況 — {latest_date}</div>
  <div class="card-body">
    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px">
      <span class="up">▲上昇 {up_cnt:,}</span>
      <span class="muted">→変わらず {flat_cnt:,}</span>
      <span class="dn">▼下落 {dn_cnt:,}</span>
    </div>
    <div class="market-bar-wrap">
      <div class="mb-up"   style="width:{up_pct:.1f}%"></div>
      <div class="mb-flat" style="width:{flat_pct:.1f}%"></div>
      <div class="mb-dn"   style="width:{dn_pct:.1f}%"></div>
    </div>
    <div style="font-size:11px;color:#8b949e;text-align:right;margin-top:4px">
      全{total:,}銘柄 / 上昇率 {up_pct:.0f}%
    </div>
  </div>
</div>"""

    # ─── テーマ過熱度カード ───────────────────────────────────────
    def _heat_badge(h):
        h = float(h or 0)
        if h >= 3:   return f'<span class="badge badge-hot">熱い</span>'
        if h >= 1:   return f'<span class="badge badge-warm">上昇</span>'
        if h <= -1:  return f'<span class="badge badge-cold">冷却</span>'
        return f'<span class="badge badge-neu">中立</span>'

    theme_rows = ""
    for tname, tcode, heat, avg_chg, breadth in themes:
        h = float(heat or 0)
        c = float(avg_chg or 0)
        theme_rows += f"""<tr>
      <td class="left"><a class="tbl-link" href="{report_link}#{tcode}">{tname}</a></td>
      <td>{_heat_badge(h)}</td>
      <td class="{'up' if h > 0 else 'dn' if h < 0 else 'muted'}">{h:+.1f}</td>
      <td class="{'up' if c > 0 else 'dn' if c < 0 else 'muted'}">{c:+.2f}%</td>
    </tr>"""

    theme_card = f"""<div class="card">
  <div class="card-header">テーマ過熱度 <a href="{report_link}" style="float:right;font-size:11px;font-weight:normal">詳細レポート →</a></div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th class="left">テーマ</th>
        <th>状態</th>
        <th>スコア</th>
        <th>前日比</th>
      </tr></thead>
      <tbody>{theme_rows}</tbody>
    </table>
  </div>
</div>"""

    # ─── 値上がり/値下がりカード ──────────────────────────────────
    def _stock_rows(stocks, is_up: bool) -> str:
        rows = ""
        for code, name, close, chg in stocks:
            cl = float(close or 0)
            rows += f"""<tr>
        <td class="left">
          <a class="tbl-link" href="/stock/{code}">{name}</a>
          <span class="muted" style="font-size:11px"> {code}</span>
        </td>
        <td style="font-size:13px">{cl:,.0f}</td>
        <td>{_fmt_chg(chg)}</td>
      </tr>"""
        return rows

    gainers_card = f"""<div class="card">
  <div class="card-header">本日の値上がりTOP5 <a href="/rankings" style="float:right;font-size:11px;font-weight:normal">全件 →</a></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th class="left">銘柄</th><th>終値</th><th>騰落</th></tr></thead>
      <tbody>{_stock_rows(gainers, True)}</tbody>
    </table>
  </div>
</div>"""

    losers_card = f"""<div class="card">
  <div class="card-header">本日の値下がりTOP5 <a href="/rankings" style="float:right;font-size:11px;font-weight:normal">全件 →</a></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th class="left">銘柄</th><th>終値</th><th>騰落</th></tr></thead>
      <tbody>{_stock_rows(losers, False)}</tbody>
    </table>
  </div>
</div>"""

    # ─── ウォッチリストカード ──────────────────────────────────────
    if watchlist:
        wl_rows = ""
        for code, name, close, chg, added_at in watchlist:
            cl = float(close or 0) if close else None
            price_str = f"{cl:,.0f}" if cl else "-"
            wl_rows += f"""<tr>
        <td class="left">
          <a class="tbl-link" href="/stock/{code}">{name}</a>
          <span class="muted" style="font-size:11px"> {code}</span>
        </td>
        <td>{price_str}</td>
        <td>{_fmt_chg(chg)}</td>
      </tr>"""
        wl_card = f"""<div class="card" style="margin-top:0">
  <div class="card-header">ウォッチリスト（{len(watchlist)}銘柄） <a href="/watchlist" style="float:right;font-size:11px;font-weight:normal">管理 →</a></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th class="left">銘柄</th><th>終値</th><th>騰落</th></tr></thead>
      <tbody>{wl_rows}</tbody>
    </table>
  </div>
</div>"""
    else:
        wl_card = f"""<div class="card" style="margin-top:0">
  <div class="card-header">ウォッチリスト <a href="/watchlist" style="float:right;font-size:11px;font-weight:normal">管理 →</a></div>
  <div class="card-body">
    <p class="muted" style="font-size:13px">
      まだ銘柄が登録されていません。
      <a href="/watchlist">ウォッチリスト</a>から追加できます。
    </p>
  </div>
</div>"""

    # ─── ナビカード（3列） ──────────────────────────────────────────
    nav_cards = f"""<div class="grid-3">
  <a href="{report_link}" style="text-decoration:none">
    <div class="card" style="padding:16px;cursor:pointer;transition:border-color .15s"
         onmouseover="this.style.borderColor='#1f6feb'" onmouseout="this.style.borderColor='#30363d'">
      <div style="font-size:24px;margin-bottom:8px">📊</div>
      <div style="font-size:14px;font-weight:600;color:#e6edf3;margin-bottom:4px">テーマ分析</div>
      <div style="font-size:12px;color:#8b949e">資金フロー・過熱スコアの詳細レポート</div>
    </div>
  </a>
  <a href="/rankings" style="text-decoration:none">
    <div class="card" style="padding:16px;cursor:pointer;transition:border-color .15s"
         onmouseover="this.style.borderColor='#1f6feb'" onmouseout="this.style.borderColor='#30363d'">
      <div style="font-size:24px;margin-bottom:8px">🏆</div>
      <div style="font-size:14px;font-weight:600;color:#e6edf3;margin-bottom:4px">ランキング</div>
      <div style="font-size:12px;color:#8b949e">値上がり・値下がり・週間ランキング</div>
    </div>
  </a>
  <a href="/watchlist" style="text-decoration:none">
    <div class="card" style="padding:16px;cursor:pointer;transition:border-color .15s"
         onmouseover="this.style.borderColor='#1f6feb'" onmouseout="this.style.borderColor='#30363d'">
      <div style="font-size:24px;margin-bottom:8px">⭐</div>
      <div style="font-size:14px;font-weight:600;color:#e6edf3;margin-bottom:4px">ウォッチリスト</div>
      <div style="font-size:12px;color:#8b949e">登録銘柄{len(watchlist)}件の最新状況</div>
    </div>
  </a>
</div>"""

    # 主要指数セクション（DBから取得、失敗時は非表示）
    index_section = _build_index_section()

    body = f"""\
<div class="page-header">
  <div class="page-title">マーケット ダッシュボード</div>
  <div class="page-subtitle">最終更新: {latest_date}</div>
</div>

{index_section}

{nav_cards}

{market_card}

<div style="margin-bottom:24px"></div>

<div class="grid-aside">
  <div>{theme_card}</div>
  <div style="display:flex;flex-direction:column;gap:16px">
    {gainers_card}
    {losers_card}
    {wl_card}
  </div>
</div>"""

    return _page_html("ダッシュボード", body, active="home")


# ════════════════════════════════════════════════════════════════════════
#  ランキングページ
# ════════════════════════════════════════════════════════════════════════

def _build_rankings_page() -> str:
    conn = get_conn()
    cur  = conn.cursor()

    cur.execute("SELECT MAX(date) FROM daily_prices WHERE close IS NOT NULL")
    latest_date: date = cur.fetchone()[0]

    # 本日の値上がり TOP20
    cur.execute("""
        SELECT dp.code, s.name, dp.close, dp.change_pct,
               COALESCE(dp.turnover, dp.volume * dp.close) AS tval
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        WHERE dp.date = %s AND dp.close IS NOT NULL AND dp.change_pct IS NOT NULL
          AND s.is_active = TRUE
        ORDER BY dp.change_pct DESC
        LIMIT 20
    """, (latest_date,))
    gainers = cur.fetchall()

    # 本日の値下がり TOP20
    cur.execute("""
        SELECT dp.code, s.name, dp.close, dp.change_pct,
               COALESCE(dp.turnover, dp.volume * dp.close) AS tval
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        WHERE dp.date = %s AND dp.close IS NOT NULL AND dp.change_pct IS NOT NULL
          AND s.is_active = TRUE
        ORDER BY dp.change_pct ASC
        LIMIT 20
    """, (latest_date,))
    losers = cur.fetchall()

    # 週間値上がり TOP20（7日前から latest_date の騰落率）
    week_ago = latest_date - timedelta(days=7)
    cur.execute("""
        SELECT dp.code, s.name, dp.close, dp.change_pct,
               (dp.close - prev.close) / prev.close * 100 AS weekly_chg
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        JOIN (
            SELECT code, close
            FROM daily_prices
            WHERE date = (
                SELECT MAX(date) FROM daily_prices WHERE date <= %s
            )
        ) prev ON dp.code = prev.code
        WHERE dp.date = %s AND dp.close IS NOT NULL AND prev.close IS NOT NULL
          AND prev.close > 0 AND s.is_active = TRUE
        ORDER BY weekly_chg DESC
        LIMIT 20
    """, (week_ago, latest_date))
    weekly = cur.fetchall()

    # 売買代金 TOP20
    cur.execute("""
        SELECT dp.code, s.name, dp.close, dp.change_pct,
               COALESCE(dp.turnover, dp.volume * dp.close) AS tval
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        WHERE dp.date = %s AND dp.close IS NOT NULL
          AND COALESCE(dp.turnover, dp.volume * dp.close) IS NOT NULL
          AND s.is_active = TRUE
        ORDER BY tval DESC
        LIMIT 20
    """, (latest_date,))
    turnover = cur.fetchall()

    cur.close()
    conn.close()

    def _rank_table(stocks, cols=("終値", "騰落率", "売買代金"), weekly=False) -> str:
        rows = ""
        for i, row in enumerate(stocks, 1):
            code, name, close, chg = row[0], row[1], row[2], row[3]
            extra = row[4] if len(row) > 4 else None
            cl = float(close or 0)
            chg_str = _fmt_chg(chg if not weekly else extra)
            if weekly:
                extra_str = _fmt_chg(extra)
                tval_str  = ""
            else:
                extra_str = ""
                tval_str  = f"{float(extra or 0)/1e8:,.1f}億" if extra else "-"

            rows += f"""<tr>
          <td class="muted" style="width:32px;text-align:center">{i}</td>
          <td class="left">
            <a class="tbl-link" href="/stock/{code}">{name}</a>
            <span class="muted" style="font-size:11px"> {code}</span>
          </td>
          <td>{cl:,.0f}</td>
          <td>{extra_str if weekly else chg_str}</td>
          <td class="muted">{tval_str}</td>
        </tr>"""

        header_cols = "<th style='width:32px'>#</th><th class='left'>銘柄</th>"
        if weekly:
            header_cols += "<th>終値</th><th>週間騰落</th><th></th>"
        else:
            header_cols += "<th>終値</th><th>騰落率</th><th>売買代金</th>"

        return f"""<div class="table-wrap">
      <table>
        <thead><tr>{header_cols}</tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""

    def _tval_table(stocks) -> str:
        rows = ""
        for i, (code, name, close, chg, tval) in enumerate(stocks, 1):
            cl = float(close or 0)
            tv = float(tval or 0) if tval else 0
            rows += f"""<tr>
          <td class="muted" style="width:32px;text-align:center">{i}</td>
          <td class="left">
            <a class="tbl-link" href="/stock/{code}">{name}</a>
            <span class="muted" style="font-size:11px"> {code}</span>
          </td>
          <td>{cl:,.0f}</td>
          <td>{_fmt_chg(chg)}</td>
          <td class="muted">{tv/1e8:,.1f}億</td>
        </tr>"""
        return f"""<div class="table-wrap">
      <table>
        <thead><tr>
          <th style="width:32px">#</th><th class="left">銘柄</th>
          <th>終値</th><th>騰落率</th><th>売買代金</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""

    body = f"""\
<div class="page-header">
  <div class="page-title">ランキング</div>
  <div class="page-subtitle">{latest_date} 時点</div>
</div>

<div class="grid-2">
  <div class="card">
    <div class="card-header">▲ 本日の値上がり TOP20</div>
    {_rank_table(gainers)}
  </div>
  <div class="card">
    <div class="card-header">▼ 本日の値下がり TOP20</div>
    {_rank_table(losers)}
  </div>
</div>

<div class="grid-2">
  <div class="card">
    <div class="card-header">週間値上がり TOP20</div>
    {_rank_table(weekly, weekly=True)}
  </div>
  <div class="card">
    <div class="card-header">売買代金 TOP20</div>
    {_tval_table(turnover)}
  </div>
</div>"""

    return _page_html(f"ランキング {latest_date}", body, active="rankings")


# ════════════════════════════════════════════════════════════════════════
#  イベントページ
# ════════════════════════════════════════════════════════════════════════

_EVENTS_CSS = """
.ev-controls {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 20px;
}
.ev-date-nav { display: flex; align-items: center; gap: 6px; }
.ev-date-label {
  font-size: 18px; font-weight: 700; color: #e6edf3; padding: 0 8px;
}
.ev-nav-btn {
  background: #21262d; border: 1px solid #30363d; color: #8b949e;
  border-radius: 6px; padding: 5px 10px; text-decoration: none; font-size: 14px;
}
.ev-nav-btn:hover { background: #30363d; color: #e6edf3; }
.ev-period-tabs { display: flex; gap: 4px; margin-left: auto; }
.ev-period-tab {
  padding: 5px 14px; border-radius: 6px; font-size: 13px; font-weight: 600;
  text-decoration: none; color: #8b949e; border: 1px solid #30363d;
}
.ev-period-tab.active { background: #388bfd; border-color: #388bfd; color: #fff; }
.ev-period-tab:hover:not(.active) { background: #21262d; color: #e6edf3; }

.ev-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
}
.ev-col-header {
  font-size: 14px; font-weight: 700; padding: 8px 14px;
  border-radius: 6px 6px 0 0; margin-bottom: 0;
  display: flex; align-items: center; gap: 8px;
}
.ev-col-header.up   { background: rgba(232,64,64,0.12); color: #E84040; border: 1px solid rgba(232,64,64,0.25); border-bottom: none; }
.ev-col-header.down { background: rgba(58,159,224,0.12); color: #3A9FE0; border: 1px solid rgba(58,159,224,0.25); border-bottom: none; }
.ev-col-header .ev-count { font-size: 12px; font-weight: 400; opacity: 0.7; }

.ev-card-list { display: flex; flex-direction: column; gap: 0; }
.ev-card {
  background: #161b22; border: 1px solid #30363d; border-top: none;
  padding: 12px 14px;
}
.ev-card:last-child { border-radius: 0 0 6px 6px; }
.ev-card-top {
  display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; flex-wrap: wrap;
}
.ev-pct {
  font-size: 18px; font-weight: 700; min-width: 70px;
}
.ev-pct.up   { color: #E84040; }
.ev-pct.down { color: #3A9FE0; }
.ev-stock-link {
  font-size: 14px; font-weight: 600; color: #e6edf3; text-decoration: none; flex: 1;
}
.ev-stock-link:hover { color: #58a6ff; }
.ev-rank { font-size: 11px; color: #484f58; }
.ev-news-list { font-size: 12px; color: #8b949e; line-height: 1.65; }
.ev-news-item { display: flex; gap: 6px; }
.ev-news-cat {
  font-size: 10px; font-weight: 700; padding: 1px 5px; border-radius: 3px;
  background: #21262d; color: #8b949e; white-space: nowrap; align-self: flex-start; margin-top: 2px;
}
.ev-news-cat.材料 { background: rgba(63,185,80,0.15); color: #3fb950; }
.ev-news-cat.開示 { background: rgba(255,166,87,0.15); color: #ffa657; }
.ev-news-cat.業績,
.ev-news-cat.決算 { background: rgba(88,166,255,0.15); color: #58a6ff; }
.ev-news-cat.注目 { background: rgba(188,140,255,0.15); color: #bc8cff; }
.ev-news-title { color: #8b949e; }

/* AI 要約ブロック */
.ev-ai-summary {
  margin: 8px 0 4px; border-left: 2px solid #388bfd;
  padding-left: 10px; display: flex; flex-direction: column; gap: 6px;
}
.ev-summary-section { display: flex; flex-direction: column; gap: 2px; }
.ev-summary-section.sources { flex-direction: row; align-items: center; flex-wrap: wrap; gap: 4px; }
.ev-summary-label {
  font-size: 10px; font-weight: 700; color: #388bfd; letter-spacing: 0.03em;
}
.ev-summary-section.sources .ev-summary-label { white-space: nowrap; }
.ev-summary-body {
  font-size: 12px; color: #c9d1d9; line-height: 1.7; margin: 0;
}
.ev-source-badge {
  font-size: 10px; padding: 1px 6px; border-radius: 10px;
  background: #21262d; color: #8b949e; border: 1px solid #30363d;
}

/* 元データ折りたたみ */
.ev-raw-toggle {
  margin-top: 6px;
}
.ev-raw-toggle summary {
  font-size: 11px; color: #484f58; cursor: pointer; user-select: none;
  list-style: none; display: flex; align-items: center; gap: 4px;
}
.ev-raw-toggle summary::-webkit-details-marker { display: none; }
.ev-raw-toggle summary::before { content: "▶"; font-size: 8px; transition: transform 0.15s; }
.ev-raw-toggle[open] summary::before { transform: rotate(90deg); }
.ev-raw-toggle .ev-news-list { margin-top: 4px; }

.ev-empty {
  background: #161b22; border: 1px solid #30363d; border-top: none;
  padding: 30px; text-align: center; color: #484f58; font-size: 13px;
  border-radius: 0 0 6px 6px;
}
.ev-no-data {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 60px; text-align: center; color: #484f58;
}

@media (max-width: 768px) {
  .ev-grid { grid-template-columns: 1fr; }
  .ev-date-label { font-size: 15px; }
}
"""


def _render_ai_summary(ai_text: str) -> str:
    """AI要約テキスト（【変動理由】【背景・詳細】【参考ソース】形式）をHTMLに変換。"""
    if not ai_text or not ai_text.strip():
        return ""
    import re
    # セクションを抽出
    sections = re.split(r'【([^】]+)】', ai_text.strip())
    # sections[0] は空文字 or 前置き、以降は [ラベル, 内容, ラベル, 内容, ...] の繰り返し
    parts = []
    i = 1
    while i < len(sections) - 1:
        label   = sections[i].strip()
        content = sections[i + 1].strip()
        i += 2
        if not content:
            continue
        if label == "参考ソース":
            # 箇条書きをインラインバッジに変換
            sources = [s.lstrip("・- ").strip() for s in content.split("\n") if s.strip()]
            badges  = "".join(f'<span class="ev-source-badge">{s}</span>' for s in sources)
            parts.append(
                f'<div class="ev-summary-section sources">'
                f'<span class="ev-summary-label">参考</span>{badges}</div>'
            )
        else:
            icon = "📌" if label == "変動理由" else "📋"
            parts.append(
                f'<div class="ev-summary-section">'
                f'<span class="ev-summary-label">{icon} {label}</span>'
                f'<p class="ev-summary-body">{content}</p></div>'
            )
    if not parts:
        return f'<div class="ev-summary-body">{ai_text.strip()}</div>'
    return f'<div class="ev-ai-summary">{"".join(parts)}</div>'


def _render_news_items(news_text: str, collapsed: bool = False) -> str:
    """保存済みニューステキストをHTML化。collapsed=True のとき折りたたみ表示。"""
    if not news_text:
        return ""
    lines = [l for l in news_text.strip().split("\n") if l.strip()]
    items = []
    for line in lines:
        # "[MM/DD HH:MM][カテゴリ] タイトル" 形式
        cat = ""
        title = line
        if line.startswith("[") and "][" in line:
            try:
                cat_end = line.index("][") + 1
                close   = line.index("]", cat_end + 1)
                cat   = line[cat_end + 1:close]
                title = line[close + 2:].strip()
            except ValueError:
                pass
        cat_cls = cat if cat in {"材料","開示","業績","決算","注目"} else ""
        cat_html = f'<span class="ev-news-cat {cat_cls}">{cat}</span>' if cat else ""
        items.append(
            f'<div class="ev-news-item">{cat_html}'
            f'<span class="ev-news-title">{title}</span></div>'
        )
    list_html = f'<div class="ev-news-list">{"".join(items)}</div>'
    if collapsed and items:
        return (f'<details class="ev-raw-toggle">'
                f'<summary>元データ（{len(items)}件）</summary>'
                f'{list_html}</details>')
    return list_html


def _build_events_page(event_date_str: str = None, period: str = "daily") -> str:
    from event_researcher import (
        get_events_for_date, get_available_event_dates,
        RESEARCH_THRESHOLD_PCT,
    )

    # 利用可能な日付一覧
    avail_dates = get_available_event_dates(period=period, limit=30)

    if not avail_dates:
        body = f"""<style>{_EVENTS_CSS}</style>
<div class="ev-no-data">
  <p style="font-size:28px;margin-bottom:8px">📭</p>
  <p>イベントデータがまだありません。</p>
  <p style="font-size:12px;margin-top:8px">daily_run.py 実行後にデータが表示されます。</p>
</div>"""
        return _page_html("イベント履歴", body, active="events")

    # 表示日付の決定
    if event_date_str:
        try:
            from datetime import date as _date
            target = _date.fromisoformat(event_date_str)
        except ValueError:
            target = avail_dates[0]
    else:
        target = avail_dates[0]

    # 前後の日付ナビゲーション
    try:
        idx = avail_dates.index(target)
    except ValueError:
        idx = 0
        target = avail_dates[0]

    prev_date = avail_dates[idx + 1] if idx + 1 < len(avail_dates) else None
    next_date = avail_dates[idx - 1] if idx > 0 else None

    # イベントデータ取得
    data = get_events_for_date(target, period=period)
    gainers = data["gainers"]
    losers  = data["losers"]

    # 日付ナビゲーション HTML
    prev_href = f"/events?date={prev_date}&period={period}" if prev_date else "#"
    next_href = f"/events?date={next_date}&period={period}" if next_date else "#"
    prev_cls  = "ev-nav-btn" if prev_date else "ev-nav-btn" + ' style="opacity:0.3;pointer-events:none"'

    date_nav = f"""<div class="ev-date-nav">
  <a class="ev-nav-btn" href="{prev_href}" {"style='opacity:0.3;pointer-events:none'" if not prev_date else ""}>◀</a>
  <span class="ev-date-label">{target.strftime('%Y年%m月%d日')}</span>
  <a class="ev-nav-btn" href="{next_href}" {"style='opacity:0.3;pointer-events:none'" if not next_date else ""}>▶</a>
</div>"""

    # 期間タブ
    period_tabs = f"""<div class="ev-period-tabs">
  <a class="ev-period-tab {'active' if period == 'daily' else ''}" href="/events?date={target}&period=daily">日次</a>
  <a class="ev-period-tab {'active' if period == 'weekly' else ''}" href="/events?date={target}&period=weekly">週次</a>
</div>"""

    threshold = RESEARCH_THRESHOLD_PCT

    # 上昇・下落カラムのHTML生成
    def _col_cards(stocks: list, direction: str) -> str:
        arrow = "▲" if direction == "up" else "▼"
        color = "up" if direction == "up" else "down"
        label = "上昇" if direction == "up" else "下落"
        header = (f'<div class="ev-col-header {color}">'
                  f'{arrow} {label}銘柄 <span class="ev-count">±{threshold:.0f}%超え {len(stocks)}件</span>'
                  f'</div>')
        if not stocks:
            return header + '<div class="ev-empty">該当なし</div>'

        cards = []
        for s in stocks:
            code = s["code"]
            name = s["name"] or code
            pct  = float(s["change_pct"] or 0)
            rk   = s["ranking"]
            rk_str = f"第{rk}位" if rk else ""
            ai_html   = _render_ai_summary(s.get("ai_summary") or "")
            news_html  = _render_news_items(s["news_items"] or "",
                                            collapsed=bool(ai_html))
            sign = "+" if pct > 0 else ""
            cards.append(f"""<div class="ev-card">
  <div class="ev-card-top">
    <span class="ev-pct {color}">{arrow}{sign}{pct:.1f}%</span>
    <a class="ev-stock-link" href="/stock/{code}">{name}（{code}）</a>
    <span class="ev-rank">{rk_str}</span>
  </div>
  {ai_html}
  {news_html}
</div>""")
        return header + f'<div class="ev-card-list">{"".join(cards)}</div>'

    body = f"""<style>{_EVENTS_CSS}</style>

<div class="ev-controls">
  {date_nav}
  {period_tabs}
</div>

<div class="ev-grid">
  <div>{_col_cards(gainers, "up")}</div>
  <div>{_col_cards(losers,  "down")}</div>
</div>"""

    return _page_html(f"イベント {target}", body, active="events")


# ════════════════════════════════════════════════════════════════════════
#  ウォッチリストページ
# ════════════════════════════════════════════════════════════════════════

def _build_watchlist_page(msg: str = "") -> str:
    conn = get_conn()
    cur  = conn.cursor()

    cur.execute("SELECT MAX(date) FROM daily_prices WHERE close IS NOT NULL")
    latest_date: date = cur.fetchone()[0]

    cur.execute("""
        SELECT w.code, s.name, dp.close, dp.change_pct,
               dp.volume, w.added_at
        FROM watchlist w
        JOIN stocks s ON w.code = s.code
        LEFT JOIN daily_prices dp ON dp.code = w.code AND dp.date = %s
        ORDER BY w.added_at DESC
    """, (latest_date,))
    items = cur.fetchall()

    cur.close()
    conn.close()

    msg_html = f'<div class="alert">{msg}</div>' if msg else ""

    if items:
        rows = ""
        for code, name, close, chg, vol, added_at in items:
            cl = float(close or 0) if close else None
            rows += f"""<tr>
          <td class="left">
            <a class="tbl-link" href="/stock/{code}"><strong>{name}</strong></a>
            <span class="muted" style="font-size:11px"> {code}</span>
          </td>
          <td>{f"{cl:,.0f}" if cl else "-"}</td>
          <td>{_fmt_chg(chg)}</td>
          <td class="muted">{f"{int(vol or 0):,}" if vol else "-"}</td>
          <td class="muted" style="font-size:11px">{str(added_at)[:10]}</td>
          <td>
            <form method="POST" action="/watchlist/remove" style="display:inline">
              <input type="hidden" name="code" value="{code}">
              <button type="submit" class="btn-sm">削除</button>
            </form>
          </td>
        </tr>"""

        table_html = f"""<div class="card" style="margin-bottom:16px">
      <div class="card-header">登録銘柄（{len(items)}件）</div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th class="left">銘柄</th>
            <th>終値</th><th>騰落率</th><th>出来高</th>
            <th>登録日</th><th></th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>"""
    else:
        table_html = '<div class="alert">ウォッチリストに銘柄が登録されていません。</div>'

    body = f"""\
<div class="page-header">
  <div class="page-title">ウォッチリスト</div>
  <div class="page-subtitle">登録した銘柄の最新状況を確認</div>
</div>

{msg_html}

<div class="card" style="margin-bottom:24px">
  <div class="card-header">銘柄を追加</div>
  <div class="card-body">
    <form method="POST" action="/watchlist/add">
      <div class="form-row">
        <input type="text" name="code" placeholder="証券コード（例: 7203）" autocomplete="off">
        <button type="submit" class="btn">追加</button>
      </div>
      <p style="font-size:12px;color:#8b949e">
        東証上場銘柄のコードを入力してください（アルファベット含む英数字 4-5 文字）
      </p>
    </form>
  </div>
</div>

{table_html}"""

    return _page_html("ウォッチリスト", body, active="watchlist")


# ════════════════════════════════════════════════════════════════════════
#  銘柄詳細ページ
# ════════════════════════════════════════════════════════════════════════

_REL = {
    3: ("コア", "#ffa657", "#3d1f00"),
    2: ("関連", "#56d364", "#122012"),
    1: ("周辺", "#6e7681", "#1c2128"),
}

_FUND_TTL_DAYS = 7  # 7日以上古ければ再取得

_STOCK_CSS = """
.s-header { margin-bottom: 20px; }
.s-name { font-size: 22px; font-weight: 700; color: #e6edf3; line-height: 1.3; }
.s-meta { font-size: 12px; color: #8b949e; margin-top: 4px; }
.s-price-row {
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 16px 20px; margin-bottom: 20px;
}
.s-price { font-size: 38px; font-weight: 700; color: #e6edf3; letter-spacing: -1px; }
.s-chg { font-size: 18px; font-weight: 600; }
.s-sub { font-size: 12px; color: #8b949e; }
.s-wl-btn { margin-left: auto; }

/* 4列キーメトリクス */
.key-metrics {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 20px;
}
.km-card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 14px 12px; text-align: center;
}
.km-label { font-size: 11px; color: #8b949e; margin-bottom: 6px; }
.km-value { font-size: 22px; font-weight: 700; color: #e6edf3; line-height: 1.1; }
.km-sub   { font-size: 11px; color: #8b949e; margin-top: 5px; }

/* チャート＋指標の2列レイアウト */
.chart-metrics-row {
  display: grid; grid-template-columns: 1fr 280px; gap: 16px; margin-bottom: 20px; align-items: start;
}
.chart-box {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 8px; overflow: hidden; min-width: 0;
}
.metrics-panel {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden;
}
.mp-title {
  background: #21262d; padding: 8px 14px; font-size: 11px;
  font-weight: 600; color: #8b949e; border-bottom: 1px solid #30363d;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.mp-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 14px; border-bottom: 1px solid #1c2128; font-size: 13px;
}
.mp-row:last-child { border-bottom: none; }
.mp-key { color: #8b949e; font-size: 12px; }
.mp-val { font-weight: 600; color: #e6edf3; }

/* テーマバッジ */
.theme-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 20px; }
.theme-badge {
  display: inline-flex; align-items: center; gap: 5px;
  border-radius: 6px; padding: 4px 10px; text-decoration: none;
}
.theme-badge:hover { opacity: 0.85; }

/* 価格テーブル */
.price-section-header {
  font-size: 13px; font-weight: 600; color: #e6edf3;
  border-bottom: 1px solid #30363d; padding-bottom: 8px; margin: 20px 0 12px;
}

/* 指標更新日 */
.fund-note { font-size: 11px; color: #484f58; text-align: right; margin-bottom: 10px; }

/* イベント履歴 */
.event-list { display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px; }
.event-card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 12px 14px;
}
.event-header {
  display: flex; align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap;
}
.event-date { font-size: 12px; color: #8b949e; }
.event-badge {
  font-size: 11px; font-weight: 700; padding: 2px 8px;
  border-radius: 4px; line-height: 1.6;
}
.event-badge.up   { background: rgba(232,64,64,0.15); color: #E84040; }
.event-badge.down { background: rgba(58,159,224,0.15); color: #3A9FE0; }
.event-period { font-size: 11px; color: #484f58; }
.event-news { font-size: 12px; color: #8b949e; line-height: 1.7; white-space: pre-wrap; }

/* 株詳細: AI 要約（イベントページと共通クラスを再利用） */
.event-card .ev-ai-summary { margin: 6px 0 4px; }
.event-card .ev-raw-toggle summary { color: #484f58; }
.event-card .ev-news-list { font-size: 12px; color: #8b949e; line-height: 1.65; }
.event-card .ev-news-item { display: flex; gap: 6px; }
.event-card .ev-news-title { color: #8b949e; }

/* メモ */
.memo-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 14px; }
.memo-card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 10px 14px; display: flex; align-items: flex-start; gap: 10px;
}
.memo-content { flex: 1; font-size: 13px; color: #c9d1d9; white-space: pre-wrap; line-height: 1.6; }
.memo-meta { font-size: 11px; color: #484f58; white-space: nowrap; }
.memo-del { color: #484f58; background: none; border: none; cursor: pointer; font-size: 14px; padding: 0; }
.memo-del:hover { color: #E84040; }
.memo-form { display: flex; gap: 8px; align-items: flex-end; }
.memo-form textarea {
  flex: 1; background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  color: #e6edf3; font-size: 13px; padding: 8px 10px; resize: vertical;
  font-family: inherit; min-height: 60px;
}
.memo-form textarea:focus { outline: none; border-color: #58a6ff; }

@media (max-width: 768px) {
  .s-price { font-size: 28px; }
  .s-chg   { font-size: 15px; }
  .key-metrics { grid-template-columns: repeat(2, 1fr); }
  .km-value { font-size: 18px; }
  .chart-metrics-row { grid-template-columns: 1fr; }
}
"""


def _build_stock_page(code: str) -> str:
    conn = get_conn()
    cur  = conn.cursor()

    # 銘柄基本情報
    cur.execute("""
        SELECT s.code, s.name, m.name AS market, sec.name AS sector
        FROM stocks s
        LEFT JOIN markets  m   ON s.market_id  = m.id
        LEFT JOIN sectors  sec ON s.sector_id  = sec.id
        WHERE s.code = %s
    """, (code,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    s_code, s_name, market, sector = row

    # 価格データ（直近3ヶ月）
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

    # ファンダメンタルズ（列名で取得してdict化 → 列順変更に強い）
    cur.execute("""
        SELECT code, shares_outstanding, eps_ttm, eps_forward, bps,
               dividend_rate, annual_dps, payout_ratio, roe, roa, debt_to_equity,
               operating_margin, profit_margin, beta, market_cap,
               per, pbr, div_yield, updated_at
        FROM stock_fundamentals WHERE code = %s
    """, (code,))
    fund_raw = cur.fetchone()
    fund_cols = ["code","shares_outstanding","eps_ttm","eps_forward","bps",
                 "dividend_rate","annual_dps","payout_ratio","roe","roa","debt_to_equity",
                 "operating_margin","profit_margin","beta","market_cap",
                 "per","pbr","div_yield","updated_at"]
    fund = dict(zip(fund_cols, fund_raw)) if fund_raw else {}

    # 直近1年の配当金合計（dividendsテーブルから）
    from_div = date.today() - timedelta(days=365)
    cur.execute("""
        SELECT SUM(amount) FROM dividends
        WHERE code = %s AND ex_date >= %s
    """, (code, from_div))
    div_row = cur.fetchone()
    div_ttm = float(div_row[0]) if div_row and div_row[0] else None

    # イベント履歴（price_events）
    cur.execute("""
        SELECT event_date, direction, change_pct, ranking, period,
               news_items, ai_summary
        FROM price_events
        WHERE code = %s
        ORDER BY event_date DESC, period
        LIMIT 15
    """, (code,))
    ev_cols = ["event_date","direction","change_pct","ranking","period",
               "news_items","ai_summary"]
    events = [dict(zip(ev_cols, r)) for r in cur.fetchall()]

    # メモ（stock_memos）
    cur.execute("""
        SELECT id, content, created_at
        FROM stock_memos
        WHERE code = %s
        ORDER BY created_at DESC
        LIMIT 20
    """, (code,))
    memo_cols = ["id","content","created_at"]
    memos = [dict(zip(memo_cols, r)) for r in cur.fetchall()]

    cur.close()
    conn.close()

    # ─ ファンダメンタルズ オンデマンド取得 ─
    need_fetch = not fund
    if not need_fetch:
        upd = fund.get("updated_at")
        if upd:
            upd_dt = upd if isinstance(upd, datetime) else datetime.combine(upd, datetime.min.time())
            need_fetch = (datetime.now() - upd_dt).days >= _FUND_TTL_DAYS
    if need_fetch:
        try:
            from fundamentals import fetch_one_on_demand, recompute_price_metrics
            print(f"[app] ファンダメンタルズ取得: {code}")
            if fetch_one_on_demand(code):
                recompute_price_metrics()
                conn2 = get_conn(); cur2 = conn2.cursor()
                cur2.execute("""
                    SELECT code, shares_outstanding, eps_ttm, eps_forward, bps,
                           dividend_rate, annual_dps, payout_ratio, roe, roa, debt_to_equity,
                           operating_margin, profit_margin, beta, market_cap,
                           per, pbr, div_yield, updated_at
                    FROM stock_fundamentals WHERE code = %s
                """, (code,))
                fund_raw2 = cur2.fetchone()
                fund = dict(zip(fund_cols, fund_raw2)) if fund_raw2 else {}
                cur2.close(); conn2.close()
        except Exception as e:
            print(f"[app] ファンダメンタルズ取得失敗: {code} / {e}")

    # ─ 最新価格 ─
    if prices:
        latest    = prices[-1]
        cur_price = float(latest[4] or 0)
        price_str = f"{cur_price:,.0f}"
        chg       = float(latest[6] or 0)
        vol       = int(latest[5] or 0)
        price_date = str(latest[0])
    else:
        cur_price, price_str, chg, vol, price_date = 0.0, "—", 0.0, 0, "—"

    chg_cls   = "up" if chg > 0 else ("dn" if chg < 0 else "muted")
    chg_arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "")

    # ─ ファンダメンタルズ値を取り出す ─
    def _fv(key):
        v = fund.get(key)
        return float(v) if v is not None else None

    shares   = _fv("shares_outstanding")
    eps_ttm  = _fv("eps_ttm")
    eps_fwd  = _fv("eps_forward")
    bps_val  = _fv("bps")
    ann_dps  = _fv("annual_dps")
    payout   = _fv("payout_ratio")
    roe      = _fv("roe")
    roa      = _fv("roa")
    dte      = _fv("debt_to_equity")
    op_mgn   = _fv("operating_margin")
    pr_mgn   = _fv("profit_margin")
    beta     = _fv("beta")

    # 最新株価で再計算（PER/PBR/時価総額は毎日変わる）
    mktcap  = cur_price * shares if cur_price and shares else _fv("market_cap")
    per_ttm = cur_price / eps_ttm if cur_price and eps_ttm and eps_ttm > 0 else _fv("per")
    per_fwd = cur_price / eps_fwd if cur_price and eps_fwd and eps_fwd > 0 else None
    pbr     = cur_price / bps_val if cur_price and bps_val and bps_val > 0 else _fv("pbr")
    dps_use = div_ttm if div_ttm else ann_dps
    div_yld = (dps_use / cur_price * 100) if dps_use and cur_price else _fv("div_yield")

    fund_updated = str(fund.get("updated_at", ""))[:10] or "—"

    # ─ 表示ヘルパー ─
    def _fmtv(v, fmt="{:.1f}", sfx=""):
        return f"{fmt.format(float(v))}{sfx}" if v is not None else "—"

    def _pct(v):
        return f"{float(v)*100:.1f}%" if v is not None else "—"

    def _mktcap(v):
        if not v: return "—"
        v = float(v)
        return f"{v/1e12:.2f}兆円" if v >= 1e12 else f"{v/1e8:.0f}億円"

    def _color(v, invert=False):
        if v is None: return ""
        pos = float(v) > 0
        if invert:
            pos = not pos
        return "color:#E84040" if pos else "color:#3A9FE0"

    # ─ キーメトリクス 4カード ─
    has_fund = bool(fund)
    def _km(label, val, sub="", color=""):
        col = f";{color}" if color else ""
        return f"""<div class="km-card">
  <div class="km-label">{label}</div>
  <div class="km-value" style="color:#e6edf3{col}">{val}</div>
  {f'<div class="km-sub">{sub}</div>' if sub else ""}
</div>"""

    if has_fund:
        key_metrics_html = f"""<div class="key-metrics">
  {_km("時価総額", _mktcap(mktcap))}
  {_km("PER（実績）",
       _fmtv(per_ttm, "{:.1f}", "倍") if per_ttm and 0 < per_ttm < 500 else "—",
       f"EPS {_fmtv(eps_ttm, '{:.2f}', '円')}")}
  {_km("PBR",
       _fmtv(pbr, "{:.2f}", "倍") if pbr and 0 < pbr < 100 else "—",
       f"BPS {_fmtv(bps_val, '{:,.0f}', '円')}")}
  {_km("配当利回り",
       _fmtv(div_yld, "{:.2f}", "%") if div_yld else "—",
       f"年間 {_fmtv(dps_use, '{:.0f}', '円')}",
       "color:#ffa657" if div_yld and float(div_yld) >= 3 else "")}
</div>"""
    else:
        key_metrics_html = '<div class="alert" style="margin-bottom:16px">指標データを取得中です。しばらくお待ちください。</div>'

    # ─ ローソク足チャート ─
    if prices:
        dates  = [p[0] for p in prices]
        opens  = [float(p[1] or 0) for p in prices]
        highs  = [float(p[2] or 0) for p in prices]
        lows   = [float(p[3] or 0) for p in prices]
        closes = [float(p[4] or 0) for p in prices]
        fig = go.Figure(go.Candlestick(
            x=dates, open=opens, high=highs, low=lows, close=closes,
            increasing_line_color="#E84040", decreasing_line_color="#3A9FE0",
            name="株価",
        ))
        fig.update_layout(
            template="plotly_dark", height=320,
            margin=dict(l=50, r=10, t=10, b=30),
            xaxis_rangeslider_visible=False,
            font=dict(size=11),
            paper_bgcolor="#161b22",
            plot_bgcolor="#161b22",
        )
        chart_div = fig.to_html(full_html=False, include_plotlyjs="cdn",
                                config={"responsive": True})
    else:
        chart_div = '<p class="muted" style="padding:40px;text-align:center">価格データなし</p>'

    # ─ 詳細指標パネル ─
    def _mp_row(key, val, color=""):
        col = f' style="{color}"' if color else ''
        return f'<div class="mp-row"><span class="mp-key">{key}</span><span class="mp-val"{col}>{val}</span></div>'

    if has_fund:
        metrics_panel = f"""<div class="metrics-panel">
  <div class="mp-title">詳細指標</div>
  {_mp_row("ROE（自己資本利益率）", _pct(roe), _color(roe))}
  {_mp_row("ROA（総資産利益率）",   _pct(roa), _color(roa))}
  {_mp_row("営業利益率",           _pct(op_mgn), _color(op_mgn))}
  {_mp_row("純利益率",             _pct(pr_mgn), _color(pr_mgn))}
  <div class="mp-row" style="background:#0d1117"></div>
  {_mp_row("D/E比率",  _fmtv(dte, "{:.1f}", "倍"),
           "color:#E84040" if dte and dte > 150 else "")}
  {_mp_row("配当性向",             _pct(payout))}
  {_mp_row("PER（予想）",
           _fmtv(per_fwd, "{:.1f}", "倍") if per_fwd and 0 < per_fwd < 500 else "—")}
  {_mp_row("予想EPS",              _fmtv(eps_fwd, "{:.2f}", "円"))}
  {_mp_row("ベータ",               _fmtv(beta, "{:.2f}"))}
  <div style="padding:6px 14px;font-size:10px;color:#484f58">
    更新: {fund_updated}
  </div>
</div>"""
    else:
        metrics_panel = ""

    # ─ テーマバッジ ─
    report_date = _latest_report_date()
    report_link = f"/report/{report_date}" if report_date else "/"
    badges = []
    for tname, tcode, rel in themes:
        lbl, fg, bg = _REL.get(rel, ("?", "#aaa", "#222"))
        badges.append(
            f'<a class="theme-badge" href="{report_link}" style="background:{bg}">'
            f'<span style="color:{fg};font-size:11px;font-weight:700">{lbl}</span>'
            f'<span style="color:#c9d1d9;font-size:13px">{tname}</span>'
            f'</a>'
        )
    theme_html = f'<div class="theme-badges">{"".join(badges)}</div>' if badges else \
                 '<p class="muted" style="font-size:13px">テーマ未分類</p>'

    # ─ 直近20日テーブル ─
    recent20 = list(reversed(prices[-20:]))
    trows = ""
    for p in recent20:
        c = float(p[6] or 0)
        cls = "up" if c > 0 else ("dn" if c < 0 else "muted")
        tv = float(p[5] or 0) * float(p[4] or 0)
        tv_str = f"{tv/1e8:.2f}億" if tv >= 1e8 else (f"{tv/1e4:.0f}万" if tv > 0 else "—")
        trows += (
            f'<tr>'
            f'<td class="left">{p[0]}</td>'
            f'<td>{float(p[4] or 0):,.0f}</td>'
            f'<td class="{cls}">{c:+.1f}%</td>'
            f'<td class="muted" style="font-size:12px">{int(p[5] or 0):,}</td>'
            f'<td class="muted" style="font-size:12px">{tv_str}</td>'
            f'</tr>'
        )

    # チャート＋指標の配置（指標なしなら1列）
    if metrics_panel:
        chart_section = f"""<div class="chart-metrics-row">
  <div class="chart-box">{chart_div}</div>
  {metrics_panel}
</div>"""
    else:
        chart_section = f'<div class="chart-box" style="margin-bottom:20px">{chart_div}</div>'

    # ─ イベント履歴 HTML ─
    if events:
        ev_cards = []
        for ev in events:
            d      = str(ev["event_date"])
            direc  = ev["direction"]
            pct    = float(ev["change_pct"] or 0)
            rk     = ev["ranking"]
            period = "日次" if ev["period"] == "daily" else "週次"
            arrow  = "▲" if direc == "up" else "▼"
            rk_str = f"（{period}第{rk}位）" if rk else f"（{period}）"
            ai_html   = _render_ai_summary(ev.get("ai_summary") or "")
            news_html = _render_news_items(ev["news_items"] or "",
                                           collapsed=bool(ai_html))
            ev_cards.append(f"""<div class="event-card">
  <div class="event-header">
    <span class="event-date">{d}</span>
    <span class="event-badge {direc}">{arrow}{abs(pct):.1f}%</span>
    <span class="event-period">{rk_str}</span>
  </div>
  {ai_html}
  {news_html}
</div>""")
        events_html = f'<p class="price-section-header">イベント履歴</p><div class="event-list">{"".join(ev_cards)}</div>'
    else:
        events_html = ""

    # ─ メモ HTML ─
    memo_cards = ""
    for m in memos:
        created = str(m["created_at"])[:16]
        content = m["content"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        memo_cards += f"""<div class="memo-card">
  <div class="memo-content">{content}</div>
  <div style="text-align:right">
    <div class="memo-meta">{created}</div>
    <form method="POST" action="/memo/delete" style="margin-top:4px">
      <input type="hidden" name="id" value="{m['id']}">
      <input type="hidden" name="code" value="{s_code}">
      <button type="submit" class="memo-del" title="削除">✕</button>
    </form>
  </div>
</div>"""
    memos_html = f"""<p class="price-section-header">メモ</p>
<div class="memo-list">{memo_cards if memo_cards else '<p class="muted" style="font-size:13px">メモなし</p>'}</div>
<form class="memo-form" method="POST" action="/memo/add">
  <input type="hidden" name="code" value="{s_code}">
  <textarea name="content" placeholder="メモを追加..." rows="2"></textarea>
  <button type="submit" class="btn-sm">追加</button>
</form>"""

    body = f"""\
<style>{_STOCK_CSS}</style>

<div class="s-header">
  <div class="s-name">{s_name}</div>
  <div class="s-meta">
    {s_code}
    {f"&nbsp;｜&nbsp;{market}" if market else ""}
    {f"&nbsp;｜&nbsp;{sector}" if sector else ""}
  </div>
</div>

<div class="s-price-row">
  <div>
    <span class="s-price">¥{price_str}</span>
    <span class="s-chg {chg_cls}" style="margin-left:10px">
      {chg_arrow} {abs(chg):.2f}%
    </span>
    <span class="s-sub" style="margin-left:8px">前日比</span>
  </div>
  <div class="s-sub">
    出来高 &nbsp;<strong style="color:#c9d1d9">{vol:,}</strong> 株
    &nbsp;｜&nbsp; {price_date}
  </div>
  <div class="s-wl-btn">
    <form method="POST" action="/watchlist/add">
      <input type="hidden" name="code" value="{s_code}">
      <input type="hidden" name="next" value="/stock/{s_code}">
      <button type="submit" class="btn-sm">⭐ ウォッチリスト</button>
    </form>
  </div>
</div>

{key_metrics_html}

{chart_section}

<p class="price-section-header">所属テーマ</p>
{theme_html}

<p class="price-section-header">直近20営業日</p>
<div class="table-wrap">
  <div class="card">
    <table>
      <thead><tr>
        <th class="left">日付</th><th>終値</th><th>前日比</th>
        <th>出来高</th><th>売買代金</th>
      </tr></thead>
      <tbody>{trows}</tbody>
    </table>
  </div>
</div>

{events_html}

{memos_html}"""

    return _page_html(f"{s_name}（{s_code}）", body, active="")


# ════════════════════════════════════════════════════════════════════════
#  Routes
# ════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT 1")
        cur.close(); conn.close()
        return "OK"
    except Exception as e:
        return f"NG: {e}", 503


@app.route("/")
def index():
    key  = f"home_{date.today()}"
    html = _get(key)
    if not html:
        print("[app] ダッシュボード生成")
        html = _build_home()
        _set(key, html)
    return html


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
        html = generate_report(report_date)
        _set(key, html)
    return html


@app.route("/rankings")
def rankings():
    key  = f"rankings_{date.today()}"
    html = _get(key)
    if not html:
        print("[app] ランキング生成")
        html = _build_rankings_page()
        _set(key, html)
    return html


@app.route("/events")
def events_page():
    event_date = request.args.get("date", "")
    period     = request.args.get("period", "daily")
    if period not in ("daily", "weekly"):
        period = "daily"
    key = f"events_{event_date}_{period}"
    html = _get(key)
    if not html:
        html = _build_events_page(event_date or None, period)
        _set(key, html)
    return html


@app.route("/watchlist")
def watchlist_page():
    return _build_watchlist_page()


@app.route("/watchlist/add", methods=["POST"])
def watchlist_add():
    code = request.form.get("code", "").strip().upper()
    next_url = request.form.get("next", "/watchlist")
    if not code:
        return redirect(next_url)
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            "INSERT IGNORE INTO watchlist (code) VALUES (%s)", (code,)
        )
        conn.commit()
        cur.close(); conn.close()
        _bust_prefix("home_")
    except Exception as e:
        print(f"[watchlist_add] error: {e}")
    return redirect(next_url)


@app.route("/watchlist/remove", methods=["POST"])
def watchlist_remove():
    code = request.form.get("code", "").strip()
    if code:
        try:
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute("DELETE FROM watchlist WHERE code = %s", (code,))
            conn.commit()
            cur.close(); conn.close()
            _bust_prefix("home_")
        except Exception as e:
            print(f"[watchlist_remove] error: {e}")
    return redirect("/watchlist")


@app.route("/memo/add", methods=["POST"])
def memo_add():
    code    = request.form.get("code", "").strip().upper()
    content = request.form.get("content", "").strip()
    if code and content:
        try:
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute(
                "INSERT INTO stock_memos (code, content) VALUES (%s, %s)",
                (code, content)
            )
            conn.commit()
            cur.close(); conn.close()
            _bust_prefix(f"stock_{code}")
        except Exception as e:
            print(f"[memo_add] error: {e}")
    return redirect(f"/stock/{code}")


@app.route("/memo/delete", methods=["POST"])
def memo_delete():
    memo_id = request.form.get("id", "").strip()
    code    = request.form.get("code", "").strip().upper()
    if memo_id and code:
        try:
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute("DELETE FROM stock_memos WHERE id = %s", (memo_id,))
            conn.commit()
            cur.close(); conn.close()
            _bust_prefix(f"stock_{code}")
        except Exception as e:
            print(f"[memo_delete] error: {e}")
    return redirect(f"/stock/{code}")


@app.route("/stock/<code>")
def stock_detail(code: str):
    key  = f"stock_{code}"
    html = _get(key)
    if not html:
        html = _build_stock_page(code)
        _set(key, html)
    return html


# ════════════════════════════════════════════════════════════════════════
#  起動
# ════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _ensure_watchlist()
    app.run(debug=True, host="0.0.0.0", port=5000)
