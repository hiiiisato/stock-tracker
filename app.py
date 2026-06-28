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
import json as _json
from datetime import date, timedelta, datetime

import requests as _requests
from bs4 import BeautifulSoup as _BS
from flask import Flask, abort, redirect, request, jsonify
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

/* ─ Nav Search ─ */
.nav-search { position: relative; margin-left: auto; }
.nav-search-input {
  background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
  color: #c9d1d9; font-size: 13px; padding: 5px 10px 5px 30px;
  width: 200px; outline: none; transition: width 0.2s, border-color 0.2s;
}
.nav-search-input:focus { border-color: #388bfd; width: 280px; }
.nav-search-icon {
  position: absolute; left: 8px; top: 50%; transform: translateY(-50%);
  color: #8b949e; font-size: 13px; pointer-events: none;
}
.nav-search-dropdown {
  position: absolute; top: calc(100% + 6px); right: 0;
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  min-width: 320px; max-height: 400px; overflow-y: auto;
  box-shadow: 0 8px 24px rgba(0,0,0,0.5); z-index: 999; display: none;
}
.nav-search-dropdown.show { display: block; }
.nav-search-item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 14px; cursor: pointer; text-decoration: none;
  border-bottom: 1px solid #21262d;
}
.nav-search-item:last-child { border-bottom: none; }
.nav-search-item:hover { background: #21262d; }
.nav-search-code {
  font-size: 12px; font-weight: 700; color: #58a6ff;
  min-width: 40px;
}
.nav-search-name { font-size: 13px; color: #e6edf3; flex: 1; }
.nav-search-market { font-size: 11px; color: #484f58; }
.nav-search-price { font-size: 12px; color: #e6edf3; text-align: right; }
.nav-search-chg { font-size: 11px; min-width: 52px; text-align: right; }
.nav-search-chg.up { color: #E84040; }
.nav-search-chg.dn { color: #3A9FE0; }
.nav-search-empty { padding: 20px; text-align: center; color: #484f58; font-size: 13px; }

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

/* ── チャートグリッドビュー ── */
.cg-toolbar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 16px;
}
.cg-view-btn {
  background: #21262d; border: 1px solid #30363d; border-radius: 4px;
  color: #8b949e; font-size: 12px; padding: 4px 14px; cursor: pointer; transition: all 0.15s;
}
.cg-view-btn.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.cg-period-btn {
  background: #21262d; border: 1px solid #30363d; border-radius: 4px;
  color: #8b949e; font-size: 12px; padding: 3px 10px; cursor: pointer; transition: all 0.15s;
}
.cg-period-btn.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.cg-sort-select {
  background: #21262d; border: 1px solid #30363d; border-radius: 4px;
  color: #c9d1d9; font-size: 12px; padding: 3px 8px; cursor: pointer;
}
.cg-grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
}
.cg-card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden;
  transition: border-color 0.15s;
}
.cg-card:hover { border-color: #58a6ff; }
.cg-card-hd {
  display: flex; justify-content: space-between; align-items: flex-start;
  padding: 10px 12px 4px; cursor: pointer;
}
.cg-name { font-size: 13px; font-weight: 600; color: #e6edf3; }
.cg-code-label { font-size: 11px; color: #8b949e; }
.cg-price { font-size: 14px; font-weight: 600; color: #e6edf3; text-align: right; }
.cg-chg  { font-size: 12px; font-weight: 600; text-align: right; }
.cg-plot { height: 150px; }
.cg-loading { text-align: center; color: #8b949e; font-size: 12px; padding: 40px; }
.cg-metrics {
  display: flex; border-top: 1px solid #21262d; background: #0d1117; padding: 4px 0;
}
.cg-metric { flex: 1; text-align: center; padding: 3px 2px; }
.cg-metric-lbl { font-size: 9px; color: #484f58; display: block; }
.cg-metric-val { font-size: 11px; color: #8b949e; font-weight: 600; display: block; }
.cg-ma-btn {
  background: transparent; border: 1px solid #30363d; border-radius: 4px;
  color: #484f58; font-size: 11px; padding: 2px 7px; cursor: pointer; transition: all 0.15s;
}
.cg-ma-btn[data-ma="25"].active { color: #f0b429; border-color: #f0b429; }
.cg-ma-btn[data-ma="75"].active { color: #a371f7; border-color: #a371f7; }
.cg-pg-btn {
  background: #21262d; border: 1px solid #30363d; border-radius: 4px;
  color: #8b949e; font-size: 12px; padding: 3px 10px; cursor: pointer; transition: all 0.15s;
}
.cg-pg-btn:hover:not(:disabled) { border-color: #58a6ff; color: #58a6ff; }
.cg-pg-btn:disabled { opacity: 0.35; cursor: default; }

/* ─ Responsive ─ */
@media (max-width: 900px) { .cg-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 560px) { .cg-grid { grid-template-columns: 1fr; } }
@media (max-width: 768px) {
  .page { padding: 12px 10px; }
  .grid-3 { grid-template-columns: 1fr; gap: 12px; }
  .grid-2 { grid-template-columns: 1fr; gap: 12px; }
  .grid-aside { grid-template-columns: 1fr; gap: 12px; }
  .page-title { font-size: 17px; }
  th, td { padding: 6px 8px; font-size: 12px; }
  .idx-grid { grid-template-columns: repeat(2, 1fr); }
  .idx-value { font-size: 16px; }
  /* ─ Mobile nav: 2行レイアウト ─ */
  .nav { flex-wrap: wrap; height: auto; padding: 0 12px; }
  .nav-logo { font-size: 13px; margin-right: 0; padding: 13px 0; flex: 1 0 auto; }
  .nav-search { margin-left: 0; padding: 10px 0; }
  .nav-search-input { width: 140px; }
  .nav-search-input:focus { width: 180px; }
  .nav-links {
    order: 3; width: 100%;
    border-top: 1px solid #21262d;
    padding: 4px 0; gap: 0;
    overflow-x: auto; -webkit-overflow-scrolling: touch;
  }
  .nav-links::-webkit-scrollbar { display: none; }
  .nav-link { height: 36px; font-size: 12px; padding: 0 10px; }
}
"""


def _nav(active: str = "") -> str:
    links = [
        ("home",      "/",           "ホーム"),
        ("screen",    "/screen",     "スクリーニング"),
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
  <div class="nav-search" id="navSearch">
    <span class="nav-search-icon">🔍</span>
    <input class="nav-search-input" id="navSearchInput"
           type="text" placeholder="銘柄コード・銘柄名で検索"
           autocomplete="off" spellcheck="false">
    <div class="nav-search-dropdown" id="navSearchDropdown"></div>
  </div>
</nav>
<script>
(function(){{
  var inp = document.getElementById('navSearchInput');
  var dd  = document.getElementById('navSearchDropdown');
  var timer;
  inp.addEventListener('input', function(){{
    clearTimeout(timer);
    var q = this.value.trim();
    if(q.length < 1){{ dd.innerHTML=''; dd.classList.remove('show'); return; }}
    timer = setTimeout(function(){{
      fetch('/api/search?q='+encodeURIComponent(q))
        .then(function(r){{return r.json();}})
        .then(function(data){{
          if(!data.length){{
            dd.innerHTML='<div class="nav-search-empty">該当なし</div>';
          }} else {{
            dd.innerHTML = data.map(function(s){{
              var chg = s.change_pct;
              var chgCls = chg>0?'up':(chg<0?'dn':'');
              var chgStr = chg!=null?(chg>0?'+':'')+chg.toFixed(2)+'%':'—';
              var price  = s.close!=null?s.close.toLocaleString('ja-JP',{{maximumFractionDigits:0}})+'円':'—';
              return '<a class="nav-search-item" href="/stock/'+s.code+'">'
                +'<span class="nav-search-code">'+s.code+'</span>'
                +'<span class="nav-search-name">'+s.name+'</span>'
                +'<span class="nav-search-market">'+s.market+'</span>'
                +'<span class="nav-search-price">'+price+'</span>'
                +'<span class="nav-search-chg '+chgCls+'">'+chgStr+'</span>'
                +'</a>';
            }}).join('');
          }}
          dd.classList.add('show');
        }});
    }}, 200);
  }});
  document.addEventListener('click', function(e){{
    if(!document.getElementById('navSearch').contains(e.target)){{
      dd.classList.remove('show');
    }}
  }});
  inp.addEventListener('keydown', function(e){{
    if(e.key==='Escape'){{ dd.classList.remove('show'); inp.blur(); }}
  }});
}})();
</script>"""


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
    history = get_history_for_chart(days=400)

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
  <div style="display:flex;gap:8px;padding:4px 0 6px;flex-wrap:wrap;align-items:center">
    <div style="display:flex;gap:3px">
      <button class="cg-period-btn idx-period-btn" data-period="1M">1M</button>
      <button class="cg-period-btn idx-period-btn active" data-period="3M">3M</button>
      <button class="cg-period-btn idx-period-btn" data-period="6M">6M</button>
      <button class="cg-period-btn idx-period-btn" data-period="1Y">1Y</button>
    </div>
    <div style="display:flex;gap:3px">
      <button class="cg-ma-btn idx-ma-btn" data-ma="25">MA25</button>
      <button class="cg-ma-btn idx-ma-btn" data-ma="75">MA75</button>
    </div>
  </div>
  <div class="idx-chart-body" id="idx-chart-div" style="height:320px"></div>
</div>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js" charset="utf-8"></script>
<script>
(function() {{
  var TRACES = {traces_data};
  var IDX_PERIOD = '3M';
  var IDX_MA = [];
  var IDX_CURRENT = 0;

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
      automargin: true,
    }},
    showlegend: false,
    height: 320,
  }};
  var config = {{responsive:true, displayModeBar:false}};

  function idxCalcMA(closes, period) {{
    var result = new Array(closes.length).fill(null);
    for (var i = period - 1; i < closes.length; i++) {{
      var sum = 0;
      for (var j = i - period + 1; j <= i; j++) sum += closes[j];
      result[i] = parseFloat((sum / period).toFixed(2));
    }}
    return result;
  }}

  function draw(idx) {{
    IDX_CURRENT = idx;
    var t = TRACES[idx];
    var dayMap = {{'1M':30,'3M':90,'6M':180,'1Y':365}};
    var days  = dayMap[IDX_PERIOD] || 90;
    var start = Math.max(0, t.dates.length - days);
    var dates  = t.dates.slice(start);
    var opens  = t.opens.slice(start);
    var highs  = t.highs.slice(start);
    var lows   = t.lows.slice(start);
    var closes = t.closes.slice(start);

    var data = [{{
      type: "candlestick",
      x: dates, open: opens, high: highs, low: lows, close: closes,
      increasing: {{line: {{color:"#E84040"}}, fillcolor:"#E84040"}},
      decreasing: {{line: {{color:"#3A9FE0"}}, fillcolor:"#3A9FE0"}},
      name: t.name,
    }}];

    IDX_MA.forEach(function(maPeriod) {{
      var maFull  = idxCalcMA(t.closes, maPeriod);
      var maSlice = maFull.slice(start);
      data.push({{
        type: "scatter",
        x: dates, y: maSlice,
        mode: "lines",
        line: {{width:1.5, color: maPeriod === 25 ? "#f0b429" : "#a371f7"}},
        name: "MA" + maPeriod,
      }});
    }});

    Plotly.newPlot("idx-chart-div", data, layout, config);
    document.querySelectorAll(".idx-tab").forEach(function(b,i) {{
      b.classList.toggle("active", i === idx);
    }});
  }}

  window.switchIdx = function(idx) {{ draw(idx); }};

  document.querySelectorAll('.idx-period-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      IDX_PERIOD = this.dataset.period;
      document.querySelectorAll('.idx-period-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      this.classList.add('active');
      draw(IDX_CURRENT);
    }});
  }});

  document.querySelectorAll('.idx-ma-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var ma = parseInt(this.dataset.ma);
      var pos = IDX_MA.indexOf(ma);
      if (pos >= 0) {{ IDX_MA.splice(pos, 1); this.classList.remove('active'); }}
      else {{ IDX_MA.push(ma); this.classList.add('active'); }}
      draw(IDX_CURRENT);
    }});
  }});

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
        SELECT tc.id, tc.name, tc.code, tds.heat_score, tds.avg_change_pct, tds.breadth_ratio
        FROM theme_daily_stats tds
        JOIN theme_categories tc ON tds.theme_id = tc.id
        WHERE tds.date = (SELECT MAX(date) FROM theme_daily_stats) AND tc.level = 2
        ORDER BY tds.heat_score DESC
    """)
    themes = cur.fetchall()  # (id, name, code, heat, avg_change_pct, breadth_ratio)

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
    for tid, tname, tcode, heat, avg_chg, breadth in themes:
        h = float(heat or 0)
        c = float(avg_chg or 0)
        theme_rows += f"""<tr>
      <td class="left"><a class="tbl-link" href="/theme/{tid}">{tname}</a></td>
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
#  チャートグリッド共通部品
# ════════════════════════════════════════════════════════════════════════

def _chart_grid_toolbar(codes_js: str, show_added_sort: bool = False) -> str:
    added_opt = '<option value="added">登録順</option>' if show_added_sort else ""
    return f"""<div class="cg-toolbar">
  <div style="display:flex;gap:4px">
    <button class="cg-view-btn active" id="btn-list">☰ リスト</button>
    <button class="cg-view-btn" id="btn-chart">⊞ チャート</button>
  </div>
  <div id="cg-chart-opts" style="display:none;flex-wrap:wrap;gap:6px;align-items:center">
    <div style="display:flex;gap:3px">
      <button class="cg-period-btn active" data-period="1M">1M</button>
      <button class="cg-period-btn" data-period="3M">3M</button>
      <button class="cg-period-btn" data-period="6M">6M</button>
      <button class="cg-period-btn" data-period="1Y">1Y</button>
    </div>
    <div style="display:flex;gap:3px">
      <button class="cg-ma-btn" data-ma="25">MA25</button>
      <button class="cg-ma-btn" data-ma="75">MA75</button>
    </div>
    <select class="cg-sort-select" id="cg-sort">
      {added_opt}
      <option value="chg_desc">前日比 ↓</option>
      <option value="chg_asc">前日比 ↑</option>
      <option value="cap_desc">時価総額 ↓</option>
      <option value="cap_asc">時価総額 ↑</option>
    </select>
    <div style="display:flex;gap:4px;align-items:center;margin-left:4px">
      <select id="cg-per-sel" class="cg-sort-select">
        <option value="50">50件</option>
        <option value="100">100件</option>
        <option value="200">200件</option>
      </select>
      <button id="cg-pg-prev" class="cg-pg-btn">◀</button>
      <span id="cg-pg-info" style="font-size:11px;color:#8b949e;white-space:nowrap;padding:0 2px"></span>
      <button id="cg-pg-next" class="cg-pg-btn">▶</button>
    </div>
  </div>
</div>
<script>var CG_CODES={codes_js};</script>"""


def _chart_grid_script() -> str:
    return """<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<script>
(function(){
  var PERIOD    = '1M';
  var ACTIVE_MA = [];
  var allData   = null;
  var loaded    = false;
  var CG_PAGE   = 0;
  var CG_PER    = 50;

  function show(id, vis) {
    var el = document.getElementById(id);
    if (el) el.style.display = vis;
  }

  function setView(v) {
    var isChart = (v === 'chart');
    show('view-list',  isChart ? 'none' : '');
    show('view-chart', isChart ? ''     : 'none');
    var btnList  = document.getElementById('btn-list');
    var btnChart = document.getElementById('btn-chart');
    if (btnList)  btnList.classList.toggle('active',  !isChart);
    if (btnChart) btnChart.classList.toggle('active', isChart);
    var opts = document.getElementById('cg-chart-opts');
    if (opts) opts.style.display = isChart ? 'flex' : 'none';
    if (isChart && !loaded) loadData();
  }

  function loadData() {
    if (!CG_CODES || !CG_CODES.length) {
      var g = document.getElementById('cg-grid');
      if (g) g.innerHTML = '<div class="cg-loading">銘柄が登録されていません</div>';
      return;
    }
    fetch('/api/chart_grid?codes=' + CG_CODES.join(','))
      .then(function(r) { return r.json(); })
      .then(function(data) {
        allData = data;
        loaded  = true;
        buildGrid();
      })
      .catch(function() {
        var g = document.getElementById('cg-grid');
        if (g) g.innerHTML = '<div class="cg-loading">読み込み失敗</div>';
      });
  }

  function filterPrices(prices) {
    var days = {'1M':31,'3M':92,'6M':183,'1Y':366};
    var n    = days[PERIOD] || 31;
    var from = new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);
    return prices.filter(function(p) { return p.date >= from; });
  }

  function getSortKey() {
    var sel = document.getElementById('cg-sort');
    return sel ? sel.value : 'added';
  }

  function updatePagination(totalPages) {
    var info = document.getElementById('cg-pg-info');
    var prev = document.getElementById('cg-pg-prev');
    var next = document.getElementById('cg-pg-next');
    if (info) info.textContent = totalPages > 1 ? (CG_PAGE + 1) + ' / ' + totalPages : '';
    if (prev) prev.disabled = (CG_PAGE === 0);
    if (next) next.disabled = (CG_PAGE >= totalPages - 1);
  }

  function buildGrid() {
    var v      = getSortKey();
    var sorted = allData.slice().sort(function(a, b) {
      if (v === 'chg_desc') return (b.change_pct || 0) - (a.change_pct || 0);
      if (v === 'chg_asc')  return (a.change_pct || 0) - (b.change_pct || 0);
      if (v === 'cap_desc') return (b.market_cap  || 0) - (a.market_cap || 0);
      if (v === 'cap_asc')  return (a.market_cap  || 0) - (b.market_cap || 0);
      return 0;
    });
    var totalPages = Math.max(1, Math.ceil(sorted.length / CG_PER));
    CG_PAGE = Math.min(CG_PAGE, totalPages - 1);
    var page = sorted.slice(CG_PAGE * CG_PER, (CG_PAGE + 1) * CG_PER);
    updatePagination(totalPages);
    var grid = document.getElementById('cg-grid');
    if (!grid) return;
    grid.innerHTML = '';
    page.forEach(function(item) {
      var chg     = item.change_pct;
      var chgStr  = chg != null ? (chg > 0 ? '+' : '') + chg.toFixed(2) + '%' : '—';
      var chgCol  = chg > 0 ? '#E84040' : chg < 0 ? '#3A9FE0' : '#8b949e';
      var chartId = 'cgc-' + item.code;
      var fmtPer  = item.per  ? item.per.toFixed(1)  + 'x' : '—';
      var fmtPbr  = item.pbr  ? item.pbr.toFixed(2)  + 'x' : '—';
      var fmtYld  = item.div_yield ? item.div_yield.toFixed(2) + '%' : '—';
      var card    = document.createElement('div');
      card.className = 'cg-card';
      card.innerHTML =
        '<div class="cg-card-hd">' +
          '<div><div class="cg-name">' + item.name + '</div>' +
          '<div class="cg-code-label">' + item.code + '</div></div>' +
          '<div><div class="cg-price">' + (item.close ? item.close.toLocaleString() : '-') + '</div>' +
          '<div class="cg-chg" style="color:' + chgCol + '">' + chgStr + '</div></div>' +
        '</div>' +
        '<div id="' + chartId + '" class="cg-plot"></div>' +
        '<div class="cg-metrics">' +
          '<div class="cg-metric"><span class="cg-metric-lbl">PER</span><span class="cg-metric-val">' + fmtPer + '</span></div>' +
          '<div class="cg-metric"><span class="cg-metric-lbl">PBR</span><span class="cg-metric-val">' + fmtPbr + '</span></div>' +
          '<div class="cg-metric"><span class="cg-metric-lbl">配当</span><span class="cg-metric-val">' + fmtYld + '</span></div>' +
        '</div>';
      card.querySelector('.cg-card-hd').addEventListener('click', function() {
        window.location.href = '/stock/' + item.code;
      });
      grid.appendChild(card);
      drawChart(chartId, filterPrices(item.prices), item.prices);
    });
  }

  function calcMA(closes, period) {
    var result = [];
    for (var i = 0; i < closes.length; i++) {
      if (i < period - 1) { result.push(null); continue; }
      var sum = 0;
      for (var j = i - period + 1; j <= i; j++) sum += closes[j];
      result.push(sum / period);
    }
    return result;
  }

  function drawChart(id, prices, fullPrices) {
    if (!prices.length || typeof Plotly === 'undefined') return;
    var minDate = prices[0].date;
    var traces  = [{
      type: 'candlestick',
      x:     prices.map(function(p) { return p.date;  }),
      open:  prices.map(function(p) { return p.open;  }),
      high:  prices.map(function(p) { return p.high;  }),
      low:   prices.map(function(p) { return p.low;   }),
      close: prices.map(function(p) { return p.close; }),
      increasing: {line:{color:'#E84040'}, fillcolor:'rgba(232,64,64,0.5)'},
      decreasing: {line:{color:'#3A9FE0'}, fillcolor:'rgba(58,159,224,0.5)'},
      showlegend: false,
    }];
    var maColors = {'25':'#f0b429', '75':'#a371f7'};
    var src = fullPrices && fullPrices.length ? fullPrices : prices;
    ACTIVE_MA.forEach(function(period) {
      var maVals = calcMA(src.map(function(p){return p.close;}), period);
      var maDates = [], maData = [];
      src.forEach(function(p, i) {
        if (p.date >= minDate) { maDates.push(p.date); maData.push(maVals[i]); }
      });
      traces.push({
        type:'scatter', mode:'lines',
        x: maDates, y: maData,
        line:{color: maColors[String(period)] || '#fff', width:1},
        showlegend:false, hoverinfo:'skip',
      });
    });
    Plotly.react(id, traces, {
      paper_bgcolor:'#161b22', plot_bgcolor:'#161b22',
      height:150, margin:{l:2,r:65,t:4,b:20},
      xaxis:{type:'category',nticks:4,tickfont:{size:9},color:'#6e7681',showgrid:false,rangeslider:{visible:false}},
      yaxis:{tickfont:{size:9},color:'#6e7681',gridcolor:'#21262d',side:'right',automargin:true},
      showlegend:false,
    }, {responsive:true, displayModeBar:false});
  }

  // ── イベントリスナー ──
  var btnList  = document.getElementById('btn-list');
  var btnChart = document.getElementById('btn-chart');
  if (btnList)  btnList.addEventListener('click',  function() { setView('list');  });
  if (btnChart) btnChart.addEventListener('click', function() { setView('chart'); });

  document.querySelectorAll('.cg-period-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.querySelectorAll('.cg-period-btn').forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
      PERIOD = btn.dataset.period;
      if (allData) buildGrid();
    });
  });

  document.querySelectorAll('.cg-ma-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var period = parseInt(btn.dataset.ma);
      var idx = ACTIVE_MA.indexOf(period);
      if (idx >= 0) { ACTIVE_MA.splice(idx, 1); btn.classList.remove('active'); }
      else          { ACTIVE_MA.push(period);    btn.classList.add('active');    }
      if (allData) buildGrid();
    });
  });

  var sortSel = document.getElementById('cg-sort');
  if (sortSel) sortSel.addEventListener('change', function() { CG_PAGE = 0; if (allData) buildGrid(); });

  var perSel = document.getElementById('cg-per-sel');
  if (perSel) perSel.addEventListener('change', function() {
    CG_PER  = parseInt(this.value) || 50;
    CG_PAGE = 0;
    if (allData) buildGrid();
  });

  var pgPrev = document.getElementById('cg-pg-prev');
  var pgNext = document.getElementById('cg-pg-next');
  if (pgPrev) pgPrev.addEventListener('click', function() {
    if (CG_PAGE > 0) { CG_PAGE--; if (allData) buildGrid(); }
  });
  if (pgNext) pgNext.addEventListener('click', function() {
    CG_PAGE++; if (allData) buildGrid();
  });

})();
</script>"""


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

    codes_js = _json.dumps([r[0] for r in items])

    body = f"""\
<div class="page-header">
  <div class="page-title">ウォッチリスト</div>
  <div class="page-subtitle">登録した銘柄の最新状況を確認</div>
</div>
{msg_html}
{_chart_grid_toolbar(codes_js, show_added_sort=True)}
<div id="view-list">
  <div class="card" style="margin-bottom:24px">
    <div class="card-header">銘柄を追加</div>
    <div class="card-body">
      <form method="POST" action="/watchlist/add">
        <div class="form-row">
          <input type="text" name="code" placeholder="証券コード（例: 7203）" autocomplete="off">
          <button type="submit" class="btn">追加</button>
        </div>
        <p style="font-size:12px;color:#8b949e">東証上場銘柄のコードを入力してください</p>
      </form>
    </div>
  </div>
  {table_html}
</div>
<div id="view-chart" style="display:none">
  <div class="cg-grid" id="cg-grid"><div class="cg-loading">読み込み中...</div></div>
</div>
{_chart_grid_script()}"""

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

/* 会社概要 */
.co-box {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 14px 18px; margin-bottom: 20px; font-size: 13px;
}
.co-title {
  font-size: 11px; font-weight: 600; color: #8b949e; text-transform: uppercase;
  letter-spacing: 0.5px; margin-bottom: 10px;
}
.co-desc { color: #c9d1d9; line-height: 1.7; margin-bottom: 10px; }
.co-meta { display: flex; flex-wrap: wrap; gap: 6px 20px; }
.co-kv { font-size: 12px; color: #8b949e; }
.co-kv span { color: #c9d1d9; }

/* チャートコントロール */
.chart-controls {
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 8px; padding: 8px 8px 4px;
}
.period-btns, .ma-btns { display: flex; align-items: center; gap: 4px; }
.ma-label { font-size: 11px; color: #8b949e; margin-right: 2px; }
.period-btn, .ma-btn {
  background: #21262d; border: 1px solid #30363d; border-radius: 4px;
  color: #8b949e; font-size: 12px; padding: 3px 10px;
  cursor: pointer; transition: all 0.15s;
}
.period-btn:hover, .ma-btn:hover { border-color: #58a6ff; color: #58a6ff; }
.period-btn.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.ma-btn.active { background: #21262d; border-color: var(--ma-color,#58a6ff); color: var(--ma-color,#58a6ff); }

@media (max-width: 768px) {
  .s-price { font-size: 28px; }
  .s-chg   { font-size: 15px; }
  .key-metrics { grid-template-columns: repeat(2, 1fr); }
  .km-value { font-size: 18px; }
  .chart-metrics-row { grid-template-columns: 1fr; }
}

/* ─ ページタブ ─ */
.pg-tabs {
  display: flex; border-bottom: 2px solid #30363d; margin-bottom: 24px;
}
.pg-tab {
  padding: 10px 24px; font-size: 14px; font-weight: 600;
  color: #8b949e; background: none; border: none;
  border-bottom: 2px solid transparent; margin-bottom: -2px;
  cursor: pointer; transition: color 0.15s; white-space: nowrap;
}
.pg-tab:hover { color: #e6edf3; }
.pg-tab.active { color: #e6edf3; border-bottom-color: #58a6ff; }
.pg-panel { display: none; }
.pg-panel.active { display: block; }

/* ─ 会社概要（改善版） ─ */
.co-facts-grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 16px;
}
.co-fact {
  background: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 10px 14px;
}
.co-fact-lbl { font-size: 10px; color: #484f58; text-transform: uppercase; letter-spacing: 0.4px; display: block; margin-bottom: 3px; }
.co-fact-val { font-size: 13px; font-weight: 600; color: #c9d1d9; }
.co-biz-divider { border: none; border-top: 1px solid #21262d; margin: 14px 0; }
.co-biz-summary { font-size: 13px; color: #c9d1d9; line-height: 1.75; }
.co-biz-toggle { margin-top: 10px; }
.co-biz-toggle summary {
  font-size: 12px; color: #388bfd; cursor: pointer; user-select: none;
  list-style: none; display: inline-flex; align-items: center; gap: 4px;
}
.co-biz-toggle summary::-webkit-details-marker { display: none; }
.co-biz-toggle summary::before { content: "▶"; font-size: 9px; transition: transform 0.2s; }
.co-biz-toggle[open] summary::before { transform: rotate(90deg); }
.co-biz-full {
  font-size: 13px; color: #8b949e; line-height: 1.75; margin-top: 10px;
  border-left: 2px solid #30363d; padding-left: 14px;
}

/* ─ 業績タブ ─ */
.fin-ctrl-bar {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 18px; flex-wrap: wrap; gap: 8px;
}
.fin-type-btns { display: flex; gap: 4px; }
.fin-type-btn {
  background: #21262d; border: 1px solid #30363d; border-radius: 6px;
  color: #8b949e; font-size: 13px; padding: 5px 18px; cursor: pointer; transition: all 0.15s;
}
.fin-type-btn.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.fin-charts-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px;
}
.fin-chart-box {
  background: #161b22; border: 1px solid #30363d; border-radius: 10px;
  padding: 10px 10px 6px; overflow: hidden;
}
.fin-chart-box.full { grid-column: 1 / -1; }
.fin-chart-hd {
  display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; padding: 0 2px;
}
.fin-chart-title { font-size: 11px; font-weight: 700; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
.fin-chart-unit { font-size: 10px; color: #484f58; }
.fin-table-wrap {
  background: #161b22; border: 1px solid #30363d; border-radius: 10px;
  overflow-x: auto; margin-bottom: 24px;
}
.fin-table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 580px; }
.fin-table th {
  background: #21262d; color: #8b949e; font-weight: 700; font-size: 11px;
  padding: 9px 14px; text-align: right; border-bottom: 1px solid #30363d;
  white-space: nowrap; text-transform: uppercase; letter-spacing: 0.4px;
}
.fin-table th:first-child { text-align: left; }
.fin-table td {
  padding: 9px 14px; border-bottom: 1px solid #1c2128;
  text-align: right; color: #c9d1d9; white-space: nowrap;
}
.fin-table td:first-child { text-align: left; color: #8b949e; font-size: 12px; }
.fin-table tr:last-child td { border-bottom: none; }
.fin-table tr:hover td { background: #1c2128; }
.fin-forecast-row td { color: #ffa657 !important; }

@media (max-width: 900px) { .fin-charts-grid { grid-template-columns: 1fr; } .fin-chart-box.full { grid-column: auto; } }
@media (max-width: 768px) { .co-facts-grid { grid-template-columns: repeat(2, 1fr); } }
"""


_SCREEN_CSS = """
.sc-wrap { max-width: 1100px; margin: 0 auto; }
.sc-page-title {
  font-size: 20px; font-weight: 700; color: #e6edf3; margin-bottom: 16px;
}
/* プリセット */
.sc-presets { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }
.sc-preset {
  background: #21262d; border: 1px solid #30363d; border-radius: 20px;
  color: #8b949e; font-size: 13px; padding: 5px 14px; cursor: pointer;
  transition: all 0.15s;
}
.sc-preset:hover  { border-color: #58a6ff; color: #58a6ff; }
.sc-preset.active { background: #1f3451; border-color: #58a6ff; color: #58a6ff; }
/* フィルターパネル */
.sc-filters {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 16px 20px; margin-bottom: 16px;
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px 24px;
}
.sc-filter-label { font-size: 11px; color: #8b949e; margin-bottom: 6px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
.sc-range { display: flex; align-items: center; gap: 6px; }
.sc-range input[type="number"] {
  width: 80px; background: #0d1117; border: 1px solid #30363d; border-radius: 5px;
  color: #e6edf3; font-size: 13px; padding: 5px 8px; text-align: right;
}
.sc-range input[type="number"]:focus { outline: none; border-color: #58a6ff; }
.sc-range-sep { color: #484f58; font-size: 12px; }
.sc-market-checks { display: flex; flex-wrap: wrap; gap: 6px 14px; }
.sc-chk { display: flex; align-items: center; gap: 5px; font-size: 13px; color: #c9d1d9; cursor: pointer; }
.sc-chk input { accent-color: #58a6ff; width: 14px; height: 14px; }
/* 結果ヘッダー */
.sc-result-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px;
}
.sc-count { font-size: 13px; color: #8b949e; }
.sc-sort {
  background: #21262d; border: 1px solid #30363d; border-radius: 5px;
  color: #c9d1d9; font-size: 13px; padding: 4px 8px; cursor: pointer;
}
/* 結果テーブル */
.sc-table-wrap { overflow-x: auto; }
.sc-table {
  width: 100%; border-collapse: collapse; font-size: 13px;
}
.sc-table th {
  background: #161b22; color: #8b949e; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.4px;
  padding: 8px 10px; border-bottom: 1px solid #30363d; white-space: nowrap;
  cursor: pointer; user-select: none;
}
.sc-table th:hover { color: #c9d1d9; }
.sc-table th.sort-asc::after  { content: ' ↑'; color: #58a6ff; }
.sc-table th.sort-desc::after { content: ' ↓'; color: #58a6ff; }
.sc-table td { padding: 8px 10px; border-bottom: 1px solid #1c2128; white-space: nowrap; }
.sc-table tr:hover td { background: #1c2128; }
.sc-table .sc-code { font-family: monospace; font-size: 12px; color: #8b949e; }
.sc-table .sc-name a { color: #79c0ff; }
.sc-table .sc-name a:hover { text-decoration: underline; }
.sc-table .num { text-align: right; }
.sc-table .up  { color: #E84040; }
.sc-table .dn  { color: #3A9FE0; }
.sc-empty { text-align: center; padding: 48px; color: #484f58; }
.sc-loading { text-align: center; padding: 48px; color: #8b949e; }
@media (max-width: 640px) {
  .sc-filters { grid-template-columns: 1fr; }
}
"""

# 会社概要キャッシュ（24h）
_co_cache: dict = {}
_CO_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

def _fetch_company_info(code: str) -> dict:
    """kabutan から会社概要を取得して24時間キャッシュする。失敗時は {}。"""
    entry = _co_cache.get(code)
    if entry and time.time() - entry["ts"] < 86400:
        return entry["data"]

    data: dict = {}
    url = f"https://kabutan.jp/stock/info?code={code}"
    try:
        r = _requests.get(url, headers=_CO_HEADERS, timeout=8)
        if r.status_code != 200:
            return {}
        soup = _BS(r.text, "html.parser")

        # 事業内容
        for sel in [".company_body p", ".company_body", "#company_info_main p"]:
            el = soup.select_one(sel)
            if el and el.text.strip():
                data["business"] = el.text.strip()[:600]
                break

        # 会社プロフィールテーブル
        tbl = soup.find("table", id="company_profile") or soup.find("table", class_="company_profile")
        if tbl:
            for tr in tbl.find_all("tr"):
                th = tr.find("th"); td = tr.find("td")
                if th and td:
                    data[th.text.strip()] = td.text.strip()
    except Exception:
        pass

    _co_cache[code] = {"ts": time.time(), "data": data}
    return data


def _build_screen_page() -> str:
    return _page_html("スクリーニング", f"""<style>{_SCREEN_CSS}</style>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<div class="sc-wrap">
  <div class="sc-page-title">スクリーニング</div>

  <div class="sc-presets" id="presets">
    <button class="sc-preset" data-preset="high-div">高配当 3%+</button>
    <button class="sc-preset" data-preset="low-pbr">低PBR &lt;1倍</button>
    <button class="sc-preset" data-preset="high-roe">高ROE 15%+</button>
    <button class="sc-preset" data-preset="value">バリュー株</button>
    <button class="sc-preset" data-preset="growth">グロース株</button>
    <button class="sc-preset" data-preset="momentum">急騰 25日+15%</button>
    <button class="sc-preset" data-preset="oversold">急落 25日-15%</button>
    <button class="sc-preset" data-preset="ma-cross-up">MA25上抜け</button>
    <button class="sc-preset" data-preset="reset" style="margin-left:auto">リセット</button>
  </div>

  <div class="sc-filters">
    <div>
      <div class="sc-filter-label">PER（倍）</div>
      <div class="sc-range">
        <input type="number" id="per-min" placeholder="下限" min="0" max="9999" step="0.1">
        <span class="sc-range-sep">〜</span>
        <input type="number" id="per-max" placeholder="上限" min="0" max="9999" step="0.1">
      </div>
    </div>
    <div>
      <div class="sc-filter-label">PBR（倍）</div>
      <div class="sc-range">
        <input type="number" id="pbr-min" placeholder="下限" min="0" max="999" step="0.1">
        <span class="sc-range-sep">〜</span>
        <input type="number" id="pbr-max" placeholder="上限" min="0" max="999" step="0.1">
      </div>
    </div>
    <div>
      <div class="sc-filter-label">ROE（%）</div>
      <div class="sc-range">
        <input type="number" id="roe-min" placeholder="下限" min="-9999" max="9999" step="0.1">
        <span class="sc-range-sep">〜</span>
        <input type="number" id="roe-max" placeholder="上限" min="-9999" max="9999" step="0.1">
      </div>
    </div>
    <div>
      <div class="sc-filter-label">配当利回り（%）</div>
      <div class="sc-range">
        <input type="number" id="div-min" placeholder="下限" min="0" max="99" step="0.1">
        <span class="sc-range-sep">〜</span>
        <input type="number" id="div-max" placeholder="上限" min="0" max="99" step="0.1">
      </div>
    </div>
    <div>
      <div class="sc-filter-label">市場</div>
      <div class="sc-market-checks">
        <label class="sc-chk"><input type="checkbox" value="プライム" checked> プライム</label>
        <label class="sc-chk"><input type="checkbox" value="スタンダード" checked> スタンダード</label>
        <label class="sc-chk"><input type="checkbox" value="グロース" checked> グロース</label>
        <label class="sc-chk"><input type="checkbox" value="other" checked> その他</label>
      </div>
    </div>
    <div>
      <div class="sc-filter-label">25日間騰落率（%）</div>
      <div class="sc-range">
        <input type="number" id="chg25-min" placeholder="下限" min="-99" max="999" step="1">
        <span class="sc-range-sep">〜</span>
        <input type="number" id="chg25-max" placeholder="上限" min="-99" max="999" step="1">
      </div>
    </div>
    <div>
      <div class="sc-filter-label">75日間騰落率（%）</div>
      <div class="sc-range">
        <input type="number" id="chg75-min" placeholder="下限" min="-99" max="999" step="1">
        <span class="sc-range-sep">〜</span>
        <input type="number" id="chg75-max" placeholder="上限" min="-99" max="999" step="1">
      </div>
    </div>
    <div>
      <div class="sc-filter-label">25日線乖離率（%）</div>
      <div class="sc-range">
        <input type="number" id="devma25-min" placeholder="下限" min="-99" max="999" step="1">
        <span class="sc-range-sep">〜</span>
        <input type="number" id="devma25-max" placeholder="上限" min="-99" max="999" step="1">
      </div>
    </div>
    <div>
      <div class="sc-filter-label">52週高値からの乖離率（%）</div>
      <div class="sc-range">
        <input type="number" id="dev52h-min" placeholder="下限" min="-99" max="0" step="1">
        <span class="sc-range-sep">〜</span>
        <input type="number" id="dev52h-max" placeholder="上限" min="-99" max="0" step="1">
      </div>
    </div>
  </div>

  <div class="sc-result-header">
    <span class="sc-count" id="sc-count">読み込み中...</span>
    <div style="display:flex;gap:4px;margin:0 8px">
      <button class="cg-view-btn active" id="sc-btn-list">☰ リスト</button>
      <button class="cg-view-btn" id="sc-btn-chart">⊞ チャート</button>
    </div>
    <select class="sc-sort" id="sc-sort">
      <option value="market_cap-desc">時価総額 大きい順</option>
      <option value="market_cap-asc">時価総額 小さい順</option>
      <option value="chg25d-desc">25日騰落率 高い順</option>
      <option value="chg25d-asc">25日騰落率 低い順</option>
      <option value="chg75d-desc">75日騰落率 高い順</option>
      <option value="chg75d-asc">75日騰落率 低い順</option>
      <option value="dev_ma25-desc">MA25乖離率 高い順</option>
      <option value="dev_ma25-asc">MA25乖離率 低い順</option>
      <option value="per-asc">PER 小さい順</option>
      <option value="per-desc">PER 大きい順</option>
      <option value="pbr-asc">PBR 小さい順</option>
      <option value="pbr-desc">PBR 大きい順</option>
      <option value="roe-desc">ROE 高い順</option>
      <option value="roe-asc">ROE 低い順</option>
      <option value="div_yield-desc">配当利回り 高い順</option>
      <option value="change_pct-desc">前日上昇率 高い順</option>
      <option value="change_pct-asc">前日下落率 高い順</option>
    </select>
  </div>

  <div id="sc-chart-wrap" style="display:none">
    <div style="display:flex;gap:6px;padding:8px 0;flex-wrap:wrap;align-items:center">
      <div style="display:flex;gap:3px">
        <button class="cg-period-btn sc-period-btn active" data-period="1M">1M</button>
        <button class="cg-period-btn sc-period-btn" data-period="3M">3M</button>
        <button class="cg-period-btn sc-period-btn" data-period="6M">6M</button>
        <button class="cg-period-btn sc-period-btn" data-period="1Y">1Y</button>
      </div>
      <div style="display:flex;gap:3px">
        <button class="cg-ma-btn sc-ma-btn" data-ma="25">MA25</button>
        <button class="cg-ma-btn sc-ma-btn" data-ma="75">MA75</button>
      </div>
      <span id="sc-chart-note" style="font-size:12px;color:#484f58"></span>
      <div style="display:flex;gap:4px;align-items:center;margin-left:auto">
        <select id="sc-per-sel" class="cg-sort-select">
          <option value="50">50件</option>
          <option value="100">100件</option>
          <option value="200">200件</option>
        </select>
        <button id="sc-pg-prev" class="cg-pg-btn">◀</button>
        <span id="sc-pg-info" style="font-size:11px;color:#8b949e;white-space:nowrap;padding:0 2px"></span>
        <button id="sc-pg-next" class="cg-pg-btn">▶</button>
      </div>
    </div>
    <div class="cg-grid" id="sc-cg-grid"></div>
  </div>

  <div class="sc-table-wrap">
    <table class="sc-table" id="sc-table">
      <thead><tr>
        <th>コード</th>
        <th>銘柄名</th>
        <th>市場</th>
        <th class="num" data-col="close">株価</th>
        <th class="num" data-col="change_pct">前日比</th>
        <th class="num" data-col="chg25d">25日騰落</th>
        <th class="num" data-col="chg75d">75日騰落</th>
        <th class="num" data-col="dev_ma25">MA25乖離</th>
        <th class="num" data-col="market_cap">時価総額</th>
        <th class="num" data-col="per">PER</th>
        <th class="num" data-col="pbr">PBR</th>
        <th class="num" data-col="roe">ROE</th>
        <th class="num" data-col="div_yield">配当利回り</th>
      </tr></thead>
      <tbody id="sc-tbody"><tr><td colspan="13" class="sc-loading">データ読み込み中...</td></tr></tbody>
    </table>
  </div>
</div>

<script>
(function(){{
  var stocks=[], sortCol='market_cap', sortDir=-1;
  var PRESETS={{
    'high-div':    {{divMin:3}},
    'low-pbr':     {{pbrMax:1, perMin:0}},
    'high-roe':    {{roeMin:15}},
    'value':       {{pbrMax:1.5, perMin:0, perMax:20}},
    'growth':      {{roeMin:20, perMin:0}},
    'momentum':    {{chg25Min:15}},
    'oversold':    {{chg25Max:-15}},
    'ma-cross-up': {{devma25Min:3, devma25Max:15}},
    'reset':       {{}},
  }};

  function fmt(v, dec, sfx) {{
    if(v===null||v===undefined) return '<span style="color:#484f58">—</span>';
    return v.toFixed(dec)+sfx;
  }}
  function fmtCap(v) {{
    if(!v) return '<span style="color:#484f58">—</span>';
    if(v>=1e12) return (v/1e12).toFixed(1)+'兆';
    return Math.round(v/1e8)+'億';
  }}
  function _v(id) {{ return parseFloat(document.getElementById(id).value)||null; }}
  function getFilters() {{
    return {{
      perMin:    _v('per-min'),   perMax:    _v('per-max'),
      pbrMin:    _v('pbr-min'),   pbrMax:    _v('pbr-max'),
      roeMin:    _v('roe-min'),   roeMax:    _v('roe-max'),
      divMin:    _v('div-min'),   divMax:    _v('div-max'),
      chg25Min:  _v('chg25-min'), chg25Max:  _v('chg25-max'),
      chg75Min:  _v('chg75-min'), chg75Max:  _v('chg75-max'),
      devma25Min:_v('devma25-min'),devma25Max:_v('devma25-max'),
      dev52hMin: _v('dev52h-min'),dev52hMax: _v('dev52h-max'),
      markets: Array.from(document.querySelectorAll('.sc-market-checks input:checked')).map(function(e){{return e.value;}}),
    }};
  }}
  function passFilter(s, f) {{
    if(f.perMin!==null    && (s.per===null||s.per<f.perMin))       return false;
    if(f.perMax!==null    && (s.per===null||s.per>f.perMax))       return false;
    if(f.pbrMin!==null    && (s.pbr===null||s.pbr<f.pbrMin))       return false;
    if(f.pbrMax!==null    && (s.pbr===null||s.pbr>f.pbrMax))       return false;
    if(f.roeMin!==null    && (s.roe===null||s.roe<f.roeMin))       return false;
    if(f.roeMax!==null    && (s.roe===null||s.roe>f.roeMax))       return false;
    if(f.divMin!==null    && (s.div_yield===null||s.div_yield<f.divMin)) return false;
    if(f.divMax!==null    && (s.div_yield===null||s.div_yield>f.divMax)) return false;
    if(f.chg25Min!==null  && (s.chg25d===null||s.chg25d<f.chg25Min))   return false;
    if(f.chg25Max!==null  && (s.chg25d===null||s.chg25d>f.chg25Max))   return false;
    if(f.chg75Min!==null  && (s.chg75d===null||s.chg75d<f.chg75Min))   return false;
    if(f.chg75Max!==null  && (s.chg75d===null||s.chg75d>f.chg75Max))   return false;
    if(f.devma25Min!==null && (s.dev_ma25===null||s.dev_ma25<f.devma25Min)) return false;
    if(f.devma25Max!==null && (s.dev_ma25===null||s.dev_ma25>f.devma25Max)) return false;
    if(f.dev52hMin!==null  && (s.dev_high52w===null||s.dev_high52w<f.dev52hMin)) return false;
    if(f.dev52hMax!==null  && (s.dev_high52w===null||s.dev_high52w>f.dev52hMax)) return false;
    var m=s.market||'';
    var matchMarket=false;
    f.markets.forEach(function(mk){{
      if(mk==='other'){{ if(!['プライム','スタンダード','グロース'].some(function(x){{return m.includes(x);}})) matchMarket=true; }}
      else if(m.includes(mk)) matchMarket=true;
    }});
    if(!matchMarket) return false;
    return true;
  }}
  function render() {{
    var f=getFilters();
    var filtered=stocks.filter(function(s){{return passFilter(s,f);}});
    filtered.sort(function(a,b){{
      var av=a[sortCol], bv=b[sortCol];
      if(av===null&&bv===null) return 0;
      if(av===null) return 1;
      if(bv===null) return -1;
      return (av-bv)*sortDir;
    }});
    document.getElementById('sc-count').textContent=filtered.length+'銘柄ヒット';
    var rows='';
    var MAX=500;
    filtered.slice(0,MAX).forEach(function(s){{
      var chg=s.change_pct;
      var chgCls=chg>0?'up':(chg<0?'dn':'');
      var chgStr=chg!==null?(chg>0?'+':'')+chg.toFixed(2)+'%':'—';
      function fmtChg(v) {{
        if(v===null||v===undefined) return '<span style="color:#484f58">—</span>';
        var cls=v>0?'up':(v<0?'dn':'');
        return '<span class="'+cls+'">'+(v>0?'+':'')+v.toFixed(2)+'%</span>';
      }}
      rows+='<tr>';
      rows+='<td class="sc-code">'+s.code+'</td>';
      rows+='<td class="sc-name"><a href="/stock/'+s.code+'">'+s.name+'</a></td>';
      rows+='<td style="font-size:11px;color:#8b949e">'+s.market+'</td>';
      rows+='<td class="num">'+(s.close?s.close.toLocaleString('ja-JP',{{maximumFractionDigits:0}}):'—')+'</td>';
      rows+='<td class="num '+chgCls+'">'+chgStr+'</td>';
      rows+='<td class="num">'+fmtChg(s.chg25d)+'</td>';
      rows+='<td class="num">'+fmtChg(s.chg75d)+'</td>';
      rows+='<td class="num">'+fmtChg(s.dev_ma25)+'</td>';
      rows+='<td class="num">'+fmtCap(s.market_cap)+'</td>';
      rows+='<td class="num">'+fmt(s.per,1,'倍')+'</td>';
      rows+='<td class="num">'+fmt(s.pbr,2,'倍')+'</td>';
      rows+='<td class="num">'+(s.roe!==null?fmt(s.roe,1,'%'):'<span style="color:#484f58">—</span>')+'</td>';
      rows+='<td class="num">'+(s.div_yield!==null?fmt(s.div_yield,2,'%'):'<span style="color:#484f58">—</span>')+'</td>';
      rows+='</tr>';
    }});
    if(!filtered.length) rows='<tr><td colspan="13" class="sc-empty">条件に一致する銘柄がありません</td></tr>';
    else if(filtered.length>MAX) rows+='<tr><td colspan="13" style="text-align:center;padding:10px;color:#484f58;font-size:12px">'+filtered.length+'件中 上位'+MAX+'件を表示</td></tr>';
    document.getElementById('sc-tbody').innerHTML=rows;
  }}

  // Sort by column header click
  document.querySelectorAll('.sc-table th[data-col]').forEach(function(th){{
    th.addEventListener('click',function(){{
      var col=th.dataset.col;
      if(sortCol===col){{ sortDir*=-1; }}
      else {{ sortCol=col; sortDir=-1; }}
      document.querySelectorAll('.sc-table th').forEach(function(t){{t.className=t.className.replace(/\\bsort-(asc|desc)\\b/g,'').trim();}});
      th.classList.add(sortDir===-1?'sort-desc':'sort-asc');
      var sel=document.getElementById('sc-sort');
      sel.value=sortCol+(sortDir===-1?'-desc':'-asc');
      render();
    }});
  }});

  // Sort select change
  document.getElementById('sc-sort').addEventListener('change',function(){{
    var v=this.value.split('-');
    sortCol=v[0]; sortDir=v[1]==='desc'?-1:1;
    render();
  }});

  // Filter inputs
  document.querySelectorAll('.sc-filters input').forEach(function(el){{
    el.addEventListener('input', render);
  }});
  document.querySelectorAll('.sc-market-checks input').forEach(function(el){{
    el.addEventListener('change', render);
  }});

  // Preset buttons
  document.querySelectorAll('.sc-preset').forEach(function(btn){{
    btn.addEventListener('click',function(){{
      var p=PRESETS[btn.dataset.preset]||{{}};
      // clear all
      ['per-min','per-max','pbr-min','pbr-max','roe-min','roe-max','div-min','div-max',
       'chg25-min','chg25-max','chg75-min','chg75-max',
       'devma25-min','devma25-max','dev52h-min','dev52h-max'].forEach(function(id){{
        document.getElementById(id).value='';
      }});
      if(p.perMin!==undefined)    document.getElementById('per-min').value=p.perMin;
      if(p.perMax!==undefined)    document.getElementById('per-max').value=p.perMax;
      if(p.pbrMin!==undefined)    document.getElementById('pbr-min').value=p.pbrMin;
      if(p.pbrMax!==undefined)    document.getElementById('pbr-max').value=p.pbrMax;
      if(p.roeMin!==undefined)    document.getElementById('roe-min').value=p.roeMin;
      if(p.roeMax!==undefined)    document.getElementById('roe-max').value=p.roeMax;
      if(p.divMin!==undefined)    document.getElementById('div-min').value=p.divMin;
      if(p.divMax!==undefined)    document.getElementById('div-max').value=p.divMax;
      if(p.chg25Min!==undefined)  document.getElementById('chg25-min').value=p.chg25Min;
      if(p.chg25Max!==undefined)  document.getElementById('chg25-max').value=p.chg25Max;
      if(p.devma25Min!==undefined)document.getElementById('devma25-min').value=p.devma25Min;
      if(p.devma25Max!==undefined)document.getElementById('devma25-max').value=p.devma25Max;
      document.querySelectorAll('.sc-preset').forEach(function(b){{b.classList.remove('active');}});
      if(btn.dataset.preset!=='reset') btn.classList.add('active');
      render();
    }});
  }});

  // Load data
  fetch('/api/screen').then(function(r){{return r.json();}}).then(function(data){{
    stocks=data;
    render();
  }}).catch(function(){{
    document.getElementById('sc-count').textContent='読み込み失敗';
  }});

  // ── チャートビュー ──────────────────────────────────────────────────────────
  var scView    = 'list';
  var SC_PERIOD = '1M';
  var SC_MA     = [];
  var SC_PAGE   = 0;
  var SC_PER    = 50;
  var allScData       = null;
  var lastScCodeOrder = [];

  function scCalcMA(closes, period) {{
    return closes.map(function(_, i) {{
      if (i < period - 1) return null;
      var s = 0;
      for (var j = i - period + 1; j <= i; j++) s += closes[j];
      return s / period;
    }});
  }}

  function scDrawChart(id, prices, fullPrices) {{
    if (!prices || !prices.length || typeof Plotly === 'undefined') return;
    var minDate = prices[0].date;
    var traces = [{{
      type:'candlestick',
      x:prices.map(function(p){{return p.date;}}),
      open:prices.map(function(p){{return p.open;}}),
      high:prices.map(function(p){{return p.high;}}),
      low:prices.map(function(p){{return p.low;}}),
      close:prices.map(function(p){{return p.close;}}),
      increasing:{{line:{{color:'#E84040'}},fillcolor:'rgba(232,64,64,0.5)'}},
      decreasing:{{line:{{color:'#3A9FE0'}},fillcolor:'rgba(58,159,224,0.5)'}},
      showlegend:false,
    }}];
    var maColors = {{'25':'#f0b429','75':'#a371f7'}};
    var src = fullPrices && fullPrices.length ? fullPrices : prices;
    SC_MA.forEach(function(period) {{
      var maVals = scCalcMA(src.map(function(p){{return p.close;}}), period);
      var maDates=[], maData=[];
      src.forEach(function(p,i){{if(p.date>=minDate){{maDates.push(p.date);maData.push(maVals[i]);}}  }});
      traces.push({{type:'scatter',mode:'lines',x:maDates,y:maData,
        line:{{color:maColors[String(period)]||'#fff',width:1}},showlegend:false,hoverinfo:'skip'}});
    }});
    Plotly.react(id, traces, {{
      paper_bgcolor:'#161b22', plot_bgcolor:'#161b22',
      height:150, margin:{{l:2,r:65,t:4,b:20}},
      xaxis:{{type:'category',nticks:4,tickfont:{{size:9}},color:'#6e7681',showgrid:false,rangeslider:{{visible:false}}}},
      yaxis:{{tickfont:{{size:9}},color:'#6e7681',gridcolor:'#21262d',side:'right',automargin:true}},
      showlegend:false,
    }}, {{responsive:true,displayModeBar:false}});
  }}

  function scFilterPrices(prices) {{
    var days = {{'1M':31,'3M':92,'6M':183,'1Y':366}};
    var n = days[SC_PERIOD] || 31;
    var from = new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);
    return prices.filter(function(p){{return p.date >= from;}});
  }}

  function scGetFilteredCodes(limit) {{
    var f = getFilters();
    var filtered = stocks.filter(function(s){{return passFilter(s,f);}});
    filtered.sort(function(a,b){{
      var av=a[sortCol],bv=b[sortCol];
      if(av===null&&bv===null)return 0;
      if(av===null)return 1;
      if(bv===null)return -1;
      return (av-bv)*sortDir;
    }});
    return filtered.slice(0, limit||200).map(function(s){{return s.code;}});
  }}

  function scUpdatePagination(totalPages, total) {{
    var info = document.getElementById('sc-pg-info');
    var prev = document.getElementById('sc-pg-prev');
    var next = document.getElementById('sc-pg-next');
    var note = document.getElementById('sc-chart-note');
    if (info) info.textContent = totalPages > 1 ? (SC_PAGE+1)+' / '+totalPages : '';
    if (prev) prev.disabled = (SC_PAGE === 0);
    if (next) next.disabled = (SC_PAGE >= totalPages - 1);
    if (note) note.textContent = '全'+total+'件';
  }}

  function scBuildGrid(data, codeOrder) {{
    allScData = data;
    lastScCodeOrder = codeOrder;
    var grid = document.getElementById('sc-cg-grid');
    if (!data || !data.length) {{
      grid.innerHTML='<div style="color:#8b949e;padding:20px">データなし</div>';
      scUpdatePagination(1, 0); return;
    }}
    var sorted = data.slice().sort(function(a,b){{return codeOrder.indexOf(a.code)-codeOrder.indexOf(b.code);}});
    var totalPages = Math.max(1, Math.ceil(sorted.length / SC_PER));
    SC_PAGE = Math.min(SC_PAGE, totalPages - 1);
    var page = sorted.slice(SC_PAGE * SC_PER, (SC_PAGE+1) * SC_PER);
    scUpdatePagination(totalPages, sorted.length);
    grid.innerHTML='';
    page.forEach(function(item) {{
      var prices = item.prices;
      if (!prices || !prices.length) return;
      var shown = scFilterPrices(prices);
      if (!shown.length) shown = prices.slice(-30);
      var n = prices.length;
      var chg = n>=2?(prices[n-1].close/prices[n-2].close-1)*100:null;
      var chgStr=chg!==null?(chg>=0?'+':'')+chg.toFixed(2)+'%':'—';
      var chgCol=chg>0?'#E84040':chg<0?'#3A9FE0':'#8b949e';
      var cl=prices[n-1].close;
      var fmtPer=item.per?item.per.toFixed(1)+'x':'—';
      var fmtPbr=item.pbr?item.pbr.toFixed(2)+'x':'—';
      var fmtYld=item.div_yield?item.div_yield.toFixed(2)+'%':'—';
      var cid='sc-cgc-'+item.code;
      var card=document.createElement('div');
      card.className='cg-card';
      card.innerHTML=
        '<div class="cg-card-hd">'+
          '<div><div class="cg-name">'+item.name+'</div><div class="cg-code-label">'+item.code+'</div></div>'+
          '<div><div class="cg-price">'+(cl?cl.toLocaleString('ja-JP',{{maximumFractionDigits:0}}):'—')+'</div>'+
          '<div class="cg-chg" style="color:'+chgCol+'">'+chgStr+'</div></div>'+
        '</div>'+
        '<div id="'+cid+'" class="cg-plot"><div class="cg-loading">読み込み中...</div></div>'+
        '<div class="cg-metrics">'+
          '<div class="cg-metric"><span class="cg-metric-lbl">PER</span><span class="cg-metric-val">'+fmtPer+'</span></div>'+
          '<div class="cg-metric"><span class="cg-metric-lbl">PBR</span><span class="cg-metric-val">'+fmtPbr+'</span></div>'+
          '<div class="cg-metric"><span class="cg-metric-lbl">配当</span><span class="cg-metric-val">'+fmtYld+'</span></div>'+
        '</div>';
      card.querySelector('.cg-card-hd').addEventListener('click',function(){{window.location.href='/stock/'+item.code;}});
      grid.appendChild(card);
      scDrawChart(cid, shown, prices);
    }});
  }}

  function refreshScGrid() {{
    if (allScData) scBuildGrid(allScData, lastScCodeOrder);
  }}

  function loadScChart() {{
    SC_PAGE = 0;
    var codes = scGetFilteredCodes(200);
    if (!codes.length) {{
      allScData = []; lastScCodeOrder = [];
      document.getElementById('sc-cg-grid').innerHTML='<div style="color:#8b949e;padding:20px">条件に一致する銘柄がありません</div>';
      scUpdatePagination(1, 0);
      return;
    }}
    document.getElementById('sc-cg-grid').innerHTML='<div style="color:#8b949e;padding:30px;text-align:center">チャートを読み込み中...</div>';
    fetch('/api/chart_grid?codes='+codes.join(','))
      .then(function(r){{return r.json();}})
      .then(function(data){{scBuildGrid(data, codes);}})
      .catch(function(){{document.getElementById('sc-cg-grid').innerHTML='<div style="color:#e84040;padding:20px">読み込み失敗</div>';}});
  }}

  function setScView(v) {{
    scView=v;
    var tableWrap=document.querySelector('.sc-table-wrap');
    var chartWrap=document.getElementById('sc-chart-wrap');
    var btnList=document.getElementById('sc-btn-list');
    var btnChart=document.getElementById('sc-btn-chart');
    if (v==='chart') {{
      tableWrap.style.display='none'; chartWrap.style.display='';
      btnList.classList.remove('active'); btnChart.classList.add('active');
      loadScChart();
    }} else {{
      tableWrap.style.display=''; chartWrap.style.display='none';
      btnList.classList.add('active'); btnChart.classList.remove('active');
    }}
  }}

  document.getElementById('sc-btn-list').addEventListener('click',function(){{setScView('list');}});
  document.getElementById('sc-btn-chart').addEventListener('click',function(){{setScView('chart');}});

  document.querySelectorAll('.sc-period-btn').forEach(function(btn){{
    btn.addEventListener('click',function(){{
      document.querySelectorAll('.sc-period-btn').forEach(function(b){{b.classList.remove('active');}});
      btn.classList.add('active');
      SC_PERIOD=btn.dataset.period;
      if(scView==='chart') refreshScGrid();
    }});
  }});

  document.querySelectorAll('.sc-ma-btn').forEach(function(btn){{
    btn.addEventListener('click',function(){{
      var period=parseInt(btn.dataset.ma);
      var idx=SC_MA.indexOf(period);
      if(idx>=0){{SC_MA.splice(idx,1);btn.classList.remove('active');}}
      else{{SC_MA.push(period);btn.classList.add('active');}}
      if(scView==='chart') refreshScGrid();
    }});
  }});

  var scPerSel = document.getElementById('sc-per-sel');
  if(scPerSel) scPerSel.addEventListener('change', function(){{
    SC_PER = parseInt(this.value) || 50;
    SC_PAGE = 0;
    refreshScGrid();
  }});
  var scPgPrev = document.getElementById('sc-pg-prev');
  if(scPgPrev) scPgPrev.addEventListener('click', function(){{
    if(SC_PAGE > 0){{ SC_PAGE--; refreshScGrid(); }}
  }});
  var scPgNext = document.getElementById('sc-pg-next');
  if(scPgNext) scPgNext.addEventListener('click', function(){{
    SC_PAGE++; refreshScGrid();
  }});
}})();
</script>
""", active="screen")


def _build_stock_page(code: str) -> str:
    conn = get_conn()
    cur  = conn.cursor()

    # 銘柄基本情報
    cur.execute("""
        SELECT s.code, s.name, m.name AS market, sec.name AS sector,
               s.business_description, s.biz_updated_at
        FROM stocks s
        LEFT JOIN markets  m   ON s.market_id  = m.id
        LEFT JOIN sectors  sec ON s.sector_id  = sec.id
        WHERE s.code = %s
    """, (code,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    s_code, s_name, market, sector, s_biz_desc, s_biz_updated = row

    # 価格データ（直近3年）
    from_dt = date.today() - timedelta(days=365 * 3)
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
    from_div = date.today() - timedelta(days=366)
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

    # ─ 業績実績（通期 + 四半期、最新12件ずつ） ─
    fin_annual_rows: list = []
    fin_quarterly_rows: list = []
    try:
        cur.execute("""
            SELECT period_end, period_type,
                   revenue, operating_income, ordinary_income, net_income,
                   total_assets, total_equity, cf_operating
            FROM financials
            WHERE code = %s AND period_type = 'A'
            ORDER BY period_end DESC LIMIT 10
        """, (code,))
        fin_annual_rows = list(reversed(cur.fetchall()))

        cur.execute("""
            SELECT period_end, period_type,
                   revenue, operating_income, ordinary_income, net_income,
                   total_assets, total_equity, cf_operating
            FROM financials
            WHERE code = %s AND period_type = 'Q'
            ORDER BY period_end DESC LIMIT 8
        """, (code,))
        fin_quarterly_rows = list(reversed(cur.fetchall()))
    except Exception as e:
        print(f"[app] financials取得失敗: {code} / {e}")

    # ─ 業績予想（最新announced_atの通期のみ） ─
    forecast_rows: list = []
    try:
        cur.execute("""
            SELECT fiscal_year_end, revenue, operating_income, ordinary_income, net_income, div_per_share
            FROM financials_forecast
            WHERE code = %s AND period_type = 'A'
              AND announced_at = (
                  SELECT MAX(announced_at) FROM financials_forecast
                  WHERE code = %s AND period_type = 'A'
              )
            ORDER BY fiscal_year_end
            LIMIT 3
        """, (code, code))
        forecast_rows = cur.fetchall()
    except Exception as e:
        print(f"[app] financials_forecast取得失敗: {code} / {e}")

    # ─ 配当全履歴（グラフ用） ─
    div_all_rows: list = []
    try:
        cur.execute("""
            SELECT ex_date, amount FROM dividends
            WHERE code = %s ORDER BY ex_date
        """, (code,))
        div_all_rows = cur.fetchall()
    except Exception as e:
        print(f"[app] dividends全件取得失敗: {code} / {e}")

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
    chg_sign  = "+" if chg > 0 else ""

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

    # ─ 業績データ JSON 生成 ─
    import collections as _col

    def _to_oku(v):
        return round(float(v) / 1e8, 1) if v is not None else None

    def _safe_pct(a, b):
        try:
            return round(float(a) / float(b) * 100, 1) if a is not None and b and float(b) != 0 else None
        except Exception:
            return None

    # 配当を年別に集計 (ex_date の年で集計)
    _div_by_year: dict = _col.defaultdict(float)
    for _ex_date, _amount in div_all_rows:
        _div_by_year[str(_ex_date)[:4]] += float(_amount or 0)

    def _build_fin_rows(rows, fc_rows=None, shares_cnt=None):
        # r indices (financials): [0]period_end [1]period_type [2]revenue
        #   [3]operating_income [4]ordinary_income [5]net_income
        #   [6]total_assets [7]total_equity [8]cf_operating
        # r indices (forecast):   [0]fiscal_year_end [1]revenue
        #   [2]operating_income [3]ordinary_income [4]net_income [5]div_per_share
        result = []
        for r in rows:
            period_end, period_type = str(r[0]), r[1]
            lbl = period_end[:7].replace("-", "/")
            rev  = _to_oku(r[2])
            op   = _to_oku(r[3])
            ord_ = _to_oku(r[4])
            net  = _to_oku(r[5])
            ta   = _to_oku(r[6])
            te   = _to_oku(r[7])
            cf   = _to_oku(r[8])
            yr   = period_end[:4]
            dps  = round(_div_by_year[yr], 1) if yr in _div_by_year else None
            # ROE = 当期純利益 / 自己資本 × 100
            roe_val = _safe_pct(r[5], r[7])
            # EPS = 当期純利益(円) / 発行済株式数
            eps_val = None
            if shares_cnt and r[5] is not None:
                try:
                    eps_val = round(float(r[5]) / float(shares_cnt), 1)
                except Exception:
                    pass
            # 配当性向 = DPS / EPS × 100
            payout_val = None
            if dps is not None and eps_val is not None and eps_val > 0:
                payout_val = round(dps / eps_val * 100, 1)
            result.append({
                "label": lbl, "period_end": period_end,
                "revenue": rev, "op": op, "ord": ord_, "net": net,
                "total_assets": ta, "total_equity": te, "cf_op": cf,
                "op_mgn":  _safe_pct(r[3], r[2]),
                "ord_mgn": _safe_pct(r[4], r[2]),
                "net_mgn": _safe_pct(r[5], r[2]),
                "dps": dps, "payout": payout_val, "roe": roe_val, "eps": eps_val,
                "is_forecast": False,
            })
        if fc_rows:
            for r in fc_rows:
                fend = str(r[0])
                lbl  = fend[:7].replace("-", "/") + "(P)"
                rev  = _to_oku(r[1])
                op   = _to_oku(r[2])
                ord_ = _to_oku(r[3])
                net  = _to_oku(r[4])
                dps  = float(r[5]) if r[5] is not None else None
                fc_eps = None
                if shares_cnt and r[4] is not None:
                    try:
                        fc_eps = round(float(r[4]) / float(shares_cnt), 1)
                    except Exception:
                        pass
                fc_payout = None
                if dps is not None and fc_eps is not None and fc_eps > 0:
                    fc_payout = round(dps / fc_eps * 100, 1)
                result.append({
                    "label": lbl, "period_end": fend,
                    "revenue": rev, "op": op, "ord": ord_, "net": net,
                    "total_assets": None, "total_equity": None, "cf_op": None,
                    "op_mgn":  _safe_pct(r[2], r[1]),
                    "ord_mgn": _safe_pct(r[3], r[1]),
                    "net_mgn": _safe_pct(r[4], r[1]),
                    "dps": dps, "payout": fc_payout, "roe": None, "eps": fc_eps,
                    "is_forecast": True,
                })
        # 前年比（売上高成長率）を計算
        for i, row in enumerate(result):
            if i > 0:
                prev = result[i - 1]
                if prev.get("revenue") and row.get("revenue") and prev["revenue"] != 0:
                    row["yoy_rev"] = round((row["revenue"] - prev["revenue"]) / abs(prev["revenue"]) * 100, 1)
                else:
                    row["yoy_rev"] = None
            else:
                row["yoy_rev"] = None
        # 重複period_end排除（実績と予想が被る場合は実績優先）
        seen: set = set()
        deduped = []
        for row in result:
            if row["period_end"] not in seen:
                seen.add(row["period_end"])
                deduped.append(row)
        return deduped

    fin_annual_data    = _build_fin_rows(fin_annual_rows, fc_rows=forecast_rows, shares_cnt=shares)
    fin_quarterly_data = _build_fin_rows(fin_quarterly_rows, shares_cnt=shares)

    fin_annual_json    = _json.dumps(fin_annual_data,    ensure_ascii=False)
    fin_quarterly_json = _json.dumps(fin_quarterly_data, ensure_ascii=False)

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

    # ─ ローソク足チャート (Plotly.js 直接利用・期間切替・MA対応) ─
    prices_json = _json.dumps([{
        "date":   str(p[0]),
        "open":   float(p[1] or 0),
        "high":   float(p[2] or 0),
        "low":    float(p[3] or 0),
        "close":  float(p[4] or 0),
        "volume": int(p[5] or 0),
    } for p in prices]) if prices else "[]"

    chart_div = f"""
<div class="chart-controls">
  <div class="period-btns">
    <button class="period-btn" data-period="1M">1M</button>
    <button class="period-btn active" data-period="3M">3M</button>
    <button class="period-btn" data-period="6M">6M</button>
    <button class="period-btn" data-period="1Y">1Y</button>
    <button class="period-btn" data-period="3Y">3Y</button>
  </div>
  <div class="ma-btns">
    <span class="ma-label">MA</span>
    <button class="ma-btn" data-ma="5" style="--ma-color:#ffa657">5</button>
    <button class="ma-btn" data-ma="25" style="--ma-color:#58a6ff">25</button>
    <button class="ma-btn" data-ma="75" style="--ma-color:#a371f7">75</button>
  </div>
</div>
<div id="stock-chart" style="width:100%;height:340px"></div>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<script>
(function(){{
  var allPrices={prices_json};
  var activePeriod='3M';
  var activeMAs=new Set();
  var MA_COLORS={{5:'#ffa657',25:'#58a6ff',75:'#a371f7'}};
  function filterPrices(period){{
    if(!allPrices.length) return allPrices;
    var last=new Date(allPrices[allPrices.length-1].date);
    var from=new Date(last);
    var M={{'1M':1,'3M':3,'6M':6,'1Y':12,'3Y':36}};
    from.setMonth(from.getMonth()-(M[period]||3));
    var fs=from.toISOString().slice(0,10);
    return allPrices.filter(function(p){{return p.date>=fs;}});
  }}
  function renderChart(){{
    var prices=filterPrices(activePeriod);
    if(!prices.length){{ Plotly.purge('stock-chart'); return; }}
    var dates=prices.map(function(p){{return p.date;}});
    var traces=[{{
      type:'candlestick',x:dates,
      open:prices.map(function(p){{return p.open;}}),
      high:prices.map(function(p){{return p.high;}}),
      low:prices.map(function(p){{return p.low;}}),
      close:prices.map(function(p){{return p.close;}}),
      name:'株価',
      increasing:{{line:{{color:'#E84040'}},fillcolor:'rgba(232,64,64,0.3)'}},
      decreasing:{{line:{{color:'#3A9FE0'}},fillcolor:'rgba(58,159,224,0.3)'}},
    }}];
    var fromDate=prices[0].date;
    [5,25,75].forEach(function(n){{
      if(!activeMAs.has(n)) return;
      var allC=allPrices.map(function(p){{return p.close;}});
      var allD=allPrices.map(function(p){{return p.date;}});
      var xArr=[],yArr=[];
      allD.forEach(function(d,i){{
        if(i<n-1||d<fromDate) return;
        var sum=0; for(var j=i-n+1;j<=i;j++) sum+=allC[j];
        xArr.push(d); yArr.push(sum/n);
      }});
      traces.push({{type:'scatter',mode:'lines',x:xArr,y:yArr,
        name:'MA'+n,line:{{color:MA_COLORS[n],width:1.5}}}});
    }});
    var layout={{
      paper_bgcolor:'#161b22',plot_bgcolor:'#161b22',
      font:{{color:'#c9d1d9',size:11}},
      height:340,margin:{{l:55,r:10,t:10,b:30}},
      xaxis:{{rangeslider:{{visible:false}},gridcolor:'#21262d',color:'#8b949e',
              type:'category',nticks:8}},
      yaxis:{{gridcolor:'#21262d',color:'#8b949e',side:'right'}},
      showlegend:activeMAs.size>0,
      legend:{{x:0.01,y:0.99,bgcolor:'rgba(22,27,34,0.8)',font:{{size:10}},orientation:'h'}},
    }};
    Plotly.react('stock-chart',traces,layout,{{responsive:true}});
  }}
  document.querySelectorAll('.period-btn').forEach(function(btn){{
    btn.addEventListener('click',function(){{
      document.querySelectorAll('.period-btn').forEach(function(b){{b.classList.remove('active');}});
      btn.classList.add('active');
      activePeriod=btn.dataset.period;
      renderChart();
    }});
  }});
  document.querySelectorAll('.ma-btn').forEach(function(btn){{
    btn.addEventListener('click',function(){{
      var n=parseInt(btn.dataset.ma);
      if(activeMAs.has(n)){{activeMAs.delete(n);btn.classList.remove('active');}}
      else{{activeMAs.add(n);btn.classList.add('active');}}
      renderChart();
    }});
  }});
  renderChart();
}})();
</script>""" if prices else '<p class="muted" style="padding:40px;text-align:center">価格データなし</p>'

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

    # ─ 会社概要（改善版） ─
    # ファクトグリッド（構造化情報）
    co_facts = []
    if market:  co_facts.append(("上場市場",   market))
    if sector:  co_facts.append(("セクター",   sector))
    if mktcap:  co_facts.append(("時価総額",   _mktcap(mktcap)))
    if shares:  co_facts.append(("発行済株式", f"{int(shares/1e4):,}万株"))

    # 事業内容: EDINETバッチ取得済みならDBから表示、未取得ならkabutan fallback
    biz = s_biz_desc or ""
    biz_src_note = ""
    if biz:
        biz_updated_str = str(s_biz_updated)[:10] if s_biz_updated else ""
        biz_src_note = f'（有価証券報告書 {biz_updated_str}）' if biz_updated_str else ""
    else:
        co_info = _fetch_company_info(s_code)
        biz = co_info.get("business", "")
        for kab_key, label in [("設立","設立"), ("資本金","資本金"), ("従業員","従業員"), ("決算","決算月")]:
            v = co_info.get(kab_key, "")
            if v:
                co_facts.append((label, v))

    # ファクトグリッドHTML
    facts_html = "".join(
        f'<div class="co-fact"><span class="co-fact-lbl">{lbl}</span>'
        f'<span class="co-fact-val">{val}</span></div>'
        for lbl, val in co_facts
    ) if co_facts else ""

    # 事業内容: 最初の段落を要約として表示 + 残りを全文トグル
    biz_summary_html = ""
    biz_full_html    = ""
    if biz:
        import html as _html_mod
        biz_esc = _html_mod.escape(biz)
        paragraphs = [p.strip() for p in biz_esc.split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = [biz_esc[:500]]
        biz_summary_html = f'<p>{paragraphs[0]}</p>'
        if len(paragraphs) > 1:
            rest = "".join(f'<p style="margin-top:8px">{p}</p>' for p in paragraphs[1:])
            biz_full_html = f"""<details class="co-biz-toggle">
  <summary>全文を表示{biz_src_note}</summary>
  <div class="co-biz-full">{rest}</div>
</details>"""

    co_html = f"""<div class="co-box">
  <div class="co-section-title">会社概要</div>
  {f'<div class="co-facts-grid">{facts_html}</div>' if facts_html else ""}
  {f'<hr class="co-biz-divider"><div class="co-biz-summary">{biz_summary_html}</div>{biz_full_html}' if biz_summary_html else ""}
</div>"""

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

    # ─ 業績タブ JS（データ埋め込み部分はf-string、ロジック部分は生文字列） ─
    _fin_data_script = f"""<script>
var FIN_ANNUAL    = {fin_annual_json};
var FIN_QUARTERLY = {fin_quarterly_json};
</script>"""

    _fin_logic_script = """<script>
(function(){
var currentFinType='A';
var finRendered=false;
var isMob=window.innerWidth<768;
var CH=isMob?170:230;

document.querySelectorAll('.pg-tab').forEach(function(btn){
  btn.addEventListener('click',function(){
    document.querySelectorAll('.pg-tab').forEach(function(b){b.classList.remove('active');});
    document.querySelectorAll('.pg-panel').forEach(function(p){p.classList.remove('active');});
    btn.classList.add('active');
    document.getElementById('tab-'+btn.dataset.tab).classList.add('active');
    if(btn.dataset.tab==='financials'&&!finRendered){
      setTimeout(function(){renderFinAll();finRendered=true;},0);
    }
  });
});

document.querySelectorAll('.fin-type-btn').forEach(function(btn){
  btn.addEventListener('click',function(){
    document.querySelectorAll('.fin-type-btn').forEach(function(b){b.classList.remove('active');});
    btn.classList.add('active');
    currentFinType=btn.dataset.finType;
    renderFinAll();
  });
});

function getData(){return currentFinType==='A'?FIN_ANNUAL:FIN_QUARTERLY;}

function renderFinAll(){
  var d=getData();
  if(!d.length){
    ['fin-rev-chart','fin-op-chart','fin-net-chart','fin-eps-chart','fin-div-chart'].forEach(function(id){
      var el=document.getElementById(id);
      if(el)el.innerHTML='<div style="padding:40px;text-align:center;color:#484f58;font-size:13px">データなし</div>';
    });
    document.getElementById('fin-table').innerHTML='<tbody><tr><td colspan="9" style="text-align:center;color:#484f58;padding:20px">業績データなし</td></tr></tbody>';
    return;
  }
  renderRevChart(d);
  renderOpChart(d);
  renderNetChart(d);
  renderEpsChart(d);
  renderDivChart(d);
  renderFinTable(d);
}

var DLAYOUT={paper_bgcolor:'#161b22',plot_bgcolor:'#161b22',
  font:{color:'#c9d1d9',size:11},showlegend:false,hovermode:'x unified'};

function mkLayout(extra){
  var L=JSON.parse(JSON.stringify(DLAYOUT));
  L.height=extra.height||CH;
  L.margin={l:52,r:52,t:8,b:36};
  L.xaxis={color:'#8b949e',gridcolor:'#21262d',tickfont:{size:10}};
  L.yaxis=Object.assign({color:'#8b949e',gridcolor:'#21262d',tickfont:{size:10},side:'left'},extra.y1||{});
  L.yaxis2=Object.assign({color:'#888',overlaying:'y',side:'right',showgrid:false,zeroline:false,tickfont:{size:10}},extra.y2||{});
  if(extra.shapes)L.shapes=extra.shapes;
  return L;
}

function fcShapes(data){
  var idx=data.findIndex(function(d){return d.is_forecast;});
  if(idx<0)return[];
  return [{type:'line',xref:'x',yref:'paper',x0:data[idx].label,x1:data[idx].label,y0:0,y1:1,
    line:{color:'#484f58',width:1,dash:'dot'}}];
}

function mkBar(data,key,color,ttmpl){
  var acts=data.filter(function(d){return!d.is_forecast;});
  var fcs =data.filter(function(d){return d.is_forecast;});
  var traces=[{
    type:'bar',x:acts.map(function(d){return d.label;}),y:acts.map(function(d){return d[key];}),
    marker:{color:color,opacity:0.85},showlegend:false,
    hovertemplate:(ttmpl||'%{y:.1f}億')+'<extra></extra>',
  }];
  if(fcs.length){
    traces.push({type:'bar',x:fcs.map(function(d){return d.label;}),y:fcs.map(function(d){return d[key];}),
      marker:{color:color,opacity:0.35},showlegend:false,
      hovertemplate:(ttmpl||'%{y:.1f}億（予）')+'<extra></extra>',
    });
  }
  return traces;
}

function mkLine(data,key,color,sfx){
  return{type:'scatter',mode:'lines+markers',
    x:data.map(function(d){return d.label;}),y:data.map(function(d){return d[key];}),
    line:{color:color,width:2},marker:{size:5,color:color},yaxis:'y2',showlegend:false,
    hovertemplate:'%{y:.1f}'+(sfx||'%')+'<extra></extra>'};
}

function yoy(data,key){
  return data.map(function(d,i){
    if(i===0||data[i-1][key]==null||d[key]==null||data[i-1][key]===0)return null;
    return Math.round((d[key]-data[i-1][key])/Math.abs(data[i-1][key])*1000)/10;
  });
}

function renderRevChart(d){
  var t=mkBar(d,'revenue','#58a6ff','%{y:.1f}億');
  var hasYoy=d.some(function(x){return x.yoy_rev!=null;});
  if(hasYoy){t.push(mkLine(d,'yoy_rev','#ffa657','%'));}
  var L=mkLayout({y1:{ticksuffix:'億'},y2:{tickcolor:'#ffa657',tickfont:{color:'#ffa657',size:10},ticksuffix:'%'},shapes:fcShapes(d)});
  Plotly.react('fin-rev-chart',t,L,{responsive:true,displayModeBar:false});
}

function renderOpChart(d){
  var acts=d.filter(function(x){return!x.is_forecast;});
  var fcs =d.filter(function(x){return x.is_forecast;});
  var t=[
    {type:'bar',name:'営業利益',x:acts.map(function(x){return x.label;}),y:acts.map(function(x){return x.op;}),
     marker:{color:'#ffa657',opacity:0.85},showlegend:true,hovertemplate:'営業利益 %{y:.1f}億<extra></extra>'},
    {type:'bar',name:'経常利益',x:acts.map(function(x){return x.label;}),y:acts.map(function(x){return x.ord;}),
     marker:{color:'#58a6ff',opacity:0.65},showlegend:true,hovertemplate:'経常利益 %{y:.1f}億<extra></extra>'},
  ];
  if(fcs.length){
    t.push({type:'bar',name:'営業利益(P)',x:fcs.map(function(x){return x.label;}),y:fcs.map(function(x){return x.op;}),
      marker:{color:'#ffa657',opacity:0.35},showlegend:false});
    t.push({type:'bar',name:'経常利益(P)',x:fcs.map(function(x){return x.label;}),y:fcs.map(function(x){return x.ord;}),
      marker:{color:'#58a6ff',opacity:0.25},showlegend:false});
  }
  t.push(mkLine(d,'op_mgn','#E84040','%'));
  var L=mkLayout({barmode:'group',y1:{ticksuffix:'億'},y2:{tickcolor:'#E84040',tickfont:{color:'#E84040',size:10},ticksuffix:'%'},shapes:fcShapes(d),
    legend:{x:0,y:1.08,orientation:'h',font:{size:11,color:'#c9d1d9'},bgcolor:'rgba(0,0,0,0)'}});
  Plotly.react('fin-op-chart',t,L,{responsive:true,displayModeBar:false});
}

function renderNetChart(d){
  var t=mkBar(d,'net','#3fb950');
  t.push(mkLine(d,'net_mgn','#a371f7','%'));
  var L=mkLayout({y1:{ticksuffix:'億'},y2:{tickcolor:'#a371f7',tickfont:{color:'#a371f7',size:10},ticksuffix:'%'},shapes:fcShapes(d)});
  Plotly.react('fin-net-chart',t,L,{responsive:true,displayModeBar:false});
}

function renderEpsChart(d){
  var acts=d.filter(function(x){return!x.is_forecast;});
  var fcs =d.filter(function(x){return x.is_forecast;});
  var traces=[{
    type:'bar',x:acts.map(function(x){return x.label;}),y:acts.map(function(x){return x.eps;}),
    marker:{color:'#58a6ff',opacity:0.85},showlegend:false,
    hovertemplate:'%{y:.0f}円<extra></extra>',
  }];
  if(fcs.length){traces.push({type:'bar',x:fcs.map(function(x){return x.label;}),y:fcs.map(function(x){return x.eps;}),
    marker:{color:'#58a6ff',opacity:0.35},showlegend:false});}
  var roeVals=d.map(function(x){return x.roe;});
  if(roeVals.some(function(v){return v!=null;})){
    traces.push({type:'scatter',mode:'lines+markers',x:d.map(function(x){return x.label;}),y:roeVals,
      line:{color:'#E84040',width:2},marker:{size:5,color:'#E84040'},yaxis:'y2',showlegend:false,
      hovertemplate:'ROE %{y:.1f}%<extra></extra>'});
  }
  var L=mkLayout({y1:{ticksuffix:'円'},y2:{tickcolor:'#E84040',tickfont:{color:'#E84040',size:10},ticksuffix:'%',range:[0,40]},shapes:fcShapes(d)});
  Plotly.react('fin-eps-chart',traces,L,{responsive:true,displayModeBar:false});
}

function renderDivChart(d){
  var hasDps=d.some(function(x){return x.dps!=null;});
  if(!hasDps){
    document.getElementById('fin-div-chart').innerHTML='<div style="padding:40px;text-align:center;color:#484f58;font-size:13px">配当データなし</div>';return;
  }
  var withDps=d.filter(function(x){return x.dps!=null;});
  var acts=withDps.filter(function(x){return!x.is_forecast;});
  var fcs =withDps.filter(function(x){return x.is_forecast;});
  var traces=[{type:'bar',x:acts.map(function(x){return x.label;}),y:acts.map(function(x){return x.dps;}),
    marker:{color:'#a371f7',opacity:0.85},showlegend:false,hovertemplate:'%{y}円<extra>DPS</extra>'}];
  if(fcs.length){traces.push({type:'bar',x:fcs.map(function(x){return x.label;}),y:fcs.map(function(x){return x.dps;}),
    marker:{color:'#a371f7',opacity:0.35},showlegend:false});}
  var payVals=withDps.map(function(x){return x.payout;});
  if(payVals.some(function(v){return v!=null;})){
    traces.push({type:'scatter',mode:'lines+markers',x:withDps.map(function(x){return x.label;}),y:payVals,
      line:{color:'#ffa657',width:2},marker:{size:5,color:'#ffa657'},yaxis:'y2',showlegend:false,
      hovertemplate:'配当性向 %{y:.1f}%<extra></extra>'});
  }
  var L=mkLayout({height:200,y1:{ticksuffix:'円'},y2:{tickcolor:'#ffa657',tickfont:{color:'#ffa657',size:10},ticksuffix:'%'},shapes:fcShapes(withDps)});
  Plotly.react('fin-div-chart',traces,L,{responsive:true,displayModeBar:false});
}

function renderFinTable(d){
  var isA=currentFinType==='A';
  var cols=isA?[
    {k:'revenue',  l:'売上高',    s:'億'},
    {k:'op',       l:'営業利益',  s:'億'},
    {k:'op_mgn',   l:'営業利益率',s:'%'},
    {k:'ord',      l:'経常利益',  s:'億'},
    {k:'net',      l:'純利益',    s:'億'},
    {k:'net_mgn',  l:'純利益率',  s:'%'},
    {k:'eps',      l:'EPS',       s:'円'},
    {k:'dps',      l:'DPS',       s:'円'},
    {k:'roe',      l:'ROE',       s:'%'},
    {k:'cf_op',    l:'営業CF',    s:'億'},
  ]:[
    {k:'revenue',  l:'売上高',    s:'億'},
    {k:'op',       l:'営業利益',  s:'億'},
    {k:'op_mgn',   l:'営業利益率',s:'%'},
    {k:'ord',      l:'経常利益',  s:'億'},
    {k:'net',      l:'純利益',    s:'億'},
    {k:'net_mgn',  l:'純利益率',  s:'%'},
    {k:'eps',      l:'EPS',       s:'円'},
  ];
  var thead='<thead><tr><th>決算期</th>'+cols.map(function(c){return'<th>'+c.l+'（'+c.s+'）</th>';}).join('')+'</tr></thead>';
  var tbody='<tbody>'+d.map(function(r){
    var cls=r.is_forecast?' class="fin-forecast-row"':'';
    var cells=cols.map(function(c){
      var v=r[c.k];
      if(v==null)return'<td><span style="color:#484f58">—</span></td>';
      return'<td>'+parseFloat(v.toFixed(1)).toLocaleString('ja-JP')+'</td>';
    }).join('');
    return'<tr'+cls+'><td>'+r.label+'</td>'+cells+'</tr>';
  }).join('')+'</tbody>';
  document.getElementById('fin-table').innerHTML=thead+tbody;
}

})();
</script>"""

    s_name_esc = s_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body = f"""\
<style>{_STOCK_CSS}</style>

<div class="s-header">
  <div class="s-name">{s_name_esc}</div>
  <div class="s-meta">
    {s_code}
    {f"&nbsp;｜&nbsp;{market}" if market else ""}
    {f"&nbsp;｜&nbsp;{sector}" if sector else ""}
  </div>
</div>

<div class="s-price-row">
  <div>
    <span class="s-price">{price_str}</span>
    <span class="s-chg {chg_cls}" style="margin-left:10px">
      {chg_sign}{chg:.2f}%
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

<div class="pg-tabs">
  <button class="pg-tab active" data-tab="overview">概要</button>
  <button class="pg-tab" data-tab="financials">業績・財務</button>
</div>

<div id="tab-overview" class="pg-panel active">

{co_html}

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

{memos_html}

</div><!-- /tab-overview -->

<div id="tab-financials" class="pg-panel">

<div class="fin-ctrl-bar">
  <div class="fin-type-btns">
    <button class="fin-type-btn active" data-fin-type="A">通期</button>
    <button class="fin-type-btn" data-fin-type="Q">四半期</button>
  </div>
  <span style="font-size:11px;color:#484f58">薄色 = 予想値</span>
</div>

<div class="fin-charts-grid">
  <div class="fin-chart-box">
    <div class="fin-chart-hd">
      <span class="fin-chart-title">売上高 &amp; 前年比</span>
      <span class="fin-chart-unit">億円 ／ %</span>
    </div>
    <div id="fin-rev-chart" style="height:230px"></div>
  </div>
  <div class="fin-chart-box">
    <div class="fin-chart-hd">
      <span class="fin-chart-title">営業利益・経常利益 &amp; 営業利益率</span>
      <span class="fin-chart-unit">億円 ／ %</span>
    </div>
    <div id="fin-op-chart" style="height:230px"></div>
  </div>
  <div class="fin-chart-box">
    <div class="fin-chart-hd">
      <span class="fin-chart-title">純利益 &amp; 純利益率</span>
      <span class="fin-chart-unit">億円 ／ %</span>
    </div>
    <div id="fin-net-chart" style="height:230px"></div>
  </div>
  <div class="fin-chart-box">
    <div class="fin-chart-hd">
      <span class="fin-chart-title">EPS &amp; ROE</span>
      <span class="fin-chart-unit">円 ／ %</span>
    </div>
    <div id="fin-eps-chart" style="height:230px"></div>
  </div>
  <div class="fin-chart-box full">
    <div class="fin-chart-hd">
      <span class="fin-chart-title">配当金（DPS）&amp; 配当性向</span>
      <span class="fin-chart-unit">円 ／ %</span>
    </div>
    <div id="fin-div-chart" style="height:200px"></div>
  </div>
</div>

<p class="price-section-header" style="margin-top:8px">財務データ</p>
<div class="fin-table-wrap">
  <table class="fin-table" id="fin-table"></table>
</div>

{_fin_data_script}
{_fin_logic_script}

</div><!-- /tab-financials -->"""

    return _page_html(f"{s_name_esc}（{s_code}）", body, active="")


# ════════════════════════════════════════════════════════════════════════
#  Routes
# ════════════════════════════════════════════════════════════════════════

@app.route("/api/screen")
def api_screen():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT s.code, s.name, m.name AS market,
               f.per, f.pbr, f.roe, f.div_yield, f.market_cap,
               lp.close, lp.change_pct,
               ps.chg5d, ps.chg25d, ps.chg75d,
               ps.dev_ma25, ps.dev_ma75, ps.dev_high52w
        FROM stocks s
        LEFT JOIN markets m ON s.market_id = m.id
        LEFT JOIN stock_fundamentals f ON s.code = f.code
        LEFT JOIN (
            SELECT dp.code, dp.close, dp.change_pct
            FROM daily_prices dp
            JOIN (SELECT code, MAX(date) AS max_date FROM daily_prices GROUP BY code) mx
              ON dp.code = mx.code AND dp.date = mx.max_date
        ) lp ON s.code = lp.code
        LEFT JOIN price_stats ps ON s.code = ps.code
        WHERE s.is_active = TRUE
          AND (f.per IS NOT NULL OR f.pbr IS NOT NULL OR f.roe IS NOT NULL
               OR f.div_yield IS NOT NULL OR ps.chg25d IS NOT NULL)
        ORDER BY COALESCE(f.market_cap, 0) DESC
        LIMIT 4000
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    results = []
    for r in rows:
        results.append({
            "code":       r[0],
            "name":       r[1] or "",
            "market":     r[2] or "",
            "per":        float(r[3])  if r[3]  is not None else None,
            "pbr":        float(r[4])  if r[4]  is not None else None,
            "roe":        float(r[5]) * 100 if r[5] is not None else None,
            "div_yield":  float(r[6])  if r[6]  is not None else None,
            "market_cap": int(r[7])    if r[7]  else None,
            "close":      float(r[8])  if r[8]  else None,
            "change_pct": float(r[9])  if r[9]  is not None else None,
            "chg5d":      float(r[10]) if r[10] is not None else None,
            "chg25d":     float(r[11]) if r[11] is not None else None,
            "chg75d":     float(r[12]) if r[12] is not None else None,
            "dev_ma25":   float(r[13]) if r[13] is not None else None,
            "dev_ma75":   float(r[14]) if r[14] is not None else None,
            "dev_high52w":float(r[15]) if r[15] is not None else None,
        })
    return jsonify(results)


@app.route("/screen")
def screen():
    key  = "screen_page"
    html = _get(key)
    if not html:
        html = _build_screen_page()
        _set(key, html)
    return html


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT s.code, s.name, m.name AS market,
               lp.close, lp.change_pct
        FROM stocks s
        LEFT JOIN markets m ON s.market_id = m.id
        LEFT JOIN (
            SELECT dp.code, dp.close, dp.change_pct
            FROM daily_prices dp
            JOIN (SELECT code, MAX(date) AS max_date FROM daily_prices GROUP BY code) mx
              ON dp.code = mx.code AND dp.date = mx.max_date
        ) lp ON s.code = lp.code
        WHERE s.is_active = TRUE
          AND (s.code LIKE %s OR s.name LIKE %s)
        ORDER BY
          CASE WHEN s.code = %s THEN 0
               WHEN s.code LIKE %s THEN 1
               ELSE 2 END, s.code
        LIMIT 12
    """, (f"{q}%", f"%{q}%", q, f"{q}%"))
    rows = cur.fetchall()
    cur.close(); conn.close()
    results = []
    for r in rows:
        results.append({
            "code":       r[0],
            "name":       r[1] or "",
            "market":     r[2] or "",
            "close":      float(r[3]) if r[3] else None,
            "change_pct": float(r[4]) if r[4] else None,
        })
    return jsonify(results)


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


@app.route("/theme/<int:theme_id>")
def theme_page(theme_id: int):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT id, name, level FROM theme_categories WHERE id = %s", (theme_id,))
    theme = cur.fetchone()
    if not theme:
        cur.close(); conn.close()
        return "テーマが見つかりません", 404

    cur.execute("""
        SELECT st.code, s.name, st.relevance
        FROM stock_themes st
        JOIN stocks s ON st.code = s.code
        WHERE st.theme_id = %s
        ORDER BY st.relevance DESC, st.code
    """, (theme_id,))
    stocks_in_theme = cur.fetchall()
    cur.close()
    conn.close()

    theme_name = theme[1]
    codes = [r[0] for r in stocks_in_theme]
    codes_js = _json.dumps(codes)

    body = f"""<div class="page-header">
  <div class="page-title">{theme_name}</div>
  <div class="page-subtitle">テーマ銘柄 {len(codes)} 件</div>
</div>
{_chart_grid_toolbar(codes_js, show_added_sort=False)}
<div id="view-list">
  <div class="card">
    <div class="table-wrap">
      <table>
        <thead><tr><th class="left">銘柄</th><th>コード</th><th>関連度</th></tr></thead>
        <tbody>{''.join(
            f'<tr><td class="left"><a class="tbl-link" href="/stock/{c}">{n}</a></td>'
            f'<td class="muted">{c}</td>'
            f'<td>{"★"*r}{"☆"*(3-r)}</td></tr>'
            for c, n, r in stocks_in_theme
        )}</tbody>
      </table>
    </div>
  </div>
</div>
<div id="view-chart" style="display:none">
  <div class="cg-grid" id="cg-grid"><div class="cg-loading">読み込み中...</div></div>
</div>
{_chart_grid_script()}"""

    return _page_html(f"{theme_name} | テーマ", body)


@app.route("/api/chart_grid")
def api_chart_grid():
    codes_param = request.args.get("codes", "")
    codes = [c.strip() for c in codes_param.split(",") if c.strip()]
    if not codes or len(codes) > 200:
        return _json.dumps({"error": "invalid"}), 400

    from datetime import timedelta as _td
    date_from = (date.today() - _td(days=380)).strftime("%Y-%m-%d")

    conn = get_conn()
    cur  = conn.cursor()

    ph = ",".join(["%s"] * len(codes))
    cur.execute(f"""
        SELECT dp.code, s.name, dp.date, dp.open, dp.high, dp.low, dp.close
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        WHERE dp.code IN ({ph}) AND dp.date >= %s AND dp.close IS NOT NULL
        ORDER BY dp.code, dp.date
    """, (*codes, date_from))
    rows = cur.fetchall()

    cur.execute(f"""
        SELECT lp.code, lp.close, lp.change_pct, f.market_cap, f.per, f.pbr, f.div_yield
        FROM (
            SELECT dp.code, dp.close, dp.change_pct
            FROM daily_prices dp
            JOIN (SELECT code, MAX(date) AS mx FROM daily_prices GROUP BY code) t
              ON dp.code = t.code AND dp.date = t.mx
            WHERE dp.code IN ({ph})
        ) lp
        LEFT JOIN stock_fundamentals f ON lp.code = f.code
    """, (*codes,))
    latest = {r[0]: {"close": r[1], "change_pct": r[2], "market_cap": r[3],
                     "per": r[4], "pbr": r[5], "div_yield": r[6]}
              for r in cur.fetchall()}

    cur.close()
    conn.close()

    from collections import defaultdict as _dd
    price_map = _dd(list)
    name_map  = {}
    for code, name, dt, o, h, l, c in rows:
        name_map[code] = name
        price_map[code].append({
            "date":  str(dt),
            "open":  float(o or 0),
            "high":  float(h or 0),
            "low":   float(l or 0),
            "close": float(c or 0),
        })

    result = []
    for code in codes:
        lat = latest.get(code, {})
        result.append({
            "code":       code,
            "name":       name_map.get(code, code),
            "close":      float(lat["close"])      if lat.get("close")      else None,
            "change_pct": float(lat["change_pct"]) if lat.get("change_pct") is not None else None,
            "market_cap": float(lat["market_cap"]) if lat.get("market_cap") else None,
            "per":        float(lat["per"])        if lat.get("per")        else None,
            "pbr":        float(lat["pbr"])        if lat.get("pbr")        else None,
            "div_yield":  float(lat["div_yield"])  if lat.get("div_yield")  else None,
            "prices":     price_map.get(code, []),
        })

    return _json.dumps(result, ensure_ascii=False, default=str)


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
