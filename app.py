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

/* ─ Responsive ─ */
@media (max-width: 768px) {
  .page { padding: 12px 10px; }
  .grid-3 { grid-template-columns: 1fr; gap: 12px; }
  .grid-2 { grid-template-columns: 1fr; gap: 12px; }
  .grid-aside { grid-template-columns: 1fr; gap: 12px; }
  .page-title { font-size: 17px; }
  th, td { padding: 6px 8px; font-size: 12px; }
  .nav-logo { font-size: 13px; margin-right: 12px; }
}
"""


def _nav(active: str = "") -> str:
    links = [
        ("home",      "/",           "ホーム"),
        ("themes",    "#",           "テーマ分析"),
        ("rankings",  "/rankings",   "ランキング"),
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

    body = f"""\
<div class="page-header">
  <div class="page-title">マーケット ダッシュボード</div>
  <div class="page-subtitle">最終更新: {latest_date}</div>
</div>

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


def _fmt_mktcap(v) -> str:
    """時価総額を兆円/億円で表示"""
    if v is None:
        return "—"
    v = float(v)
    if v >= 1e12:
        return f"{v/1e12:.2f}兆円"
    return f"{v/1e8:.0f}億円"


def _metric_box(label: str, value: str, note: str = "", color: str = "") -> str:
    col = f"color:{color}" if color else "color:#e6edf3"
    return f"""<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
                         padding:14px 16px;text-align:center;min-width:0">
  <div style="font-size:11px;color:#8b949e;margin-bottom:6px">{label}</div>
  <div style="font-size:20px;font-weight:700;{col};line-height:1.2">{value}</div>
  {f'<div style="font-size:11px;color:#8b949e;margin-top:4px">{note}</div>' if note else ""}
</div>"""


def _build_stock_page(code: str) -> str:
    conn = get_conn()
    cur  = conn.cursor()

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

    # ファンダメンタルズ（なければ後でオンデマンド取得）
    cur.execute("SELECT * FROM stock_fundamentals WHERE code = %s", (code,))
    fund_row = cur.fetchone()

    # 直近1年の配当金合計（dividendsテーブルから）
    from_div = date.today() - timedelta(days=365)
    cur.execute("""
        SELECT SUM(amount) FROM dividends
        WHERE code = %s AND ex_date >= %s
    """, (code, from_div))
    div_sum_row = cur.fetchone()
    div_ttm = float(div_sum_row[0]) if div_sum_row and div_sum_row[0] else None

    cur.close()
    conn.close()

    # ─ ファンダメンタルズ オンデマンド取得 ─
    # DB になし、または 7 日以上古い場合は即時取得（1〜2秒程度）
    _FUND_TTL_DAYS = 7
    need_fetch = fund_row is None
    if not need_fetch and fund_row[15]:
        updated = fund_row[15] if isinstance(fund_row[15], datetime) else datetime.combine(fund_row[15], datetime.min.time())
        need_fetch = (datetime.now() - updated).days >= _FUND_TTL_DAYS
    if need_fetch:
        try:
            from fundamentals import fetch_one_on_demand
            print(f"[app] ファンダメンタルズ取得: {code}")
            if fetch_one_on_demand(code):
                # 取得成功 → 再クエリ
                conn2 = get_conn()
                cur2  = conn2.cursor()
                cur2.execute("SELECT * FROM stock_fundamentals WHERE code = %s", (code,))
                fund_row = cur2.fetchone()
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
    else:
        cur_price, price_str, chg, vol = 0.0, "-", 0.0, 0

    chg_cls = "up" if chg > 0 else ("dn" if chg < 0 else "muted")

    # ─ ファンダメンタルズ解析 ─
    def _fd(idx):
        if fund_row and fund_row[idx] is not None:
            return float(fund_row[idx])
        return None

    shares   = _fd(1)   # shares_outstanding
    eps_ttm  = _fd(2)   # EPS実績
    eps_fwd  = _fd(3)   # EPS予想
    bps      = _fd(4)   # BPS
    div_rate = _fd(5)   # 年間配当（予想）
    ann_dps  = _fd(6)   # 年間配当（直近実績）- Yahoo
    payout   = _fd(7)   # 配当性向
    roe      = _fd(8)
    roa      = _fd(9)
    dte      = _fd(10)  # D/E比率
    op_mgn   = _fd(11)  # 営業利益率
    pr_mgn   = _fd(12)  # 純利益率
    beta     = _fd(13)
    mktcap_s = _fd(14)  # market_cap from Yahoo（API取得時の価格ベース）

    # 価格から再計算（常に最新価格ベース）
    mktcap = cur_price * shares if cur_price and shares else mktcap_s
    per_ttm = cur_price / eps_ttm if cur_price and eps_ttm and eps_ttm > 0 else None
    per_fwd = cur_price / eps_fwd if cur_price and eps_fwd and eps_fwd > 0 else None
    pbr     = cur_price / bps     if cur_price and bps     and bps     > 0 else None
    # 配当利回り: dividendsテーブル合計を優先、なければ Yahoo の値
    dps_use = div_ttm if div_ttm else ann_dps
    div_yld = (dps_use / cur_price * 100) if dps_use and cur_price else None

    def _f(v, fmt="{:.1f}", suffix="", fallback="—"):
        return f"{fmt.format(v)}{suffix}" if v is not None else fallback

    def _pct(v, fallback="—"):
        return f"{v*100:.1f}%" if v is not None else fallback

    def _signed_color(v):
        if v is None: return "#e6edf3"
        return "#E84040" if float(v) > 0 else ("#3A9FE0" if float(v) < 0 else "#e6edf3")

    no_data = fund_row is None

    # ─ 投資指標グリッド ─
    metrics_html = ""
    if not no_data:
        # 行1: 時価総額・PER・PBR
        # 行2: 配当利回り・配当金・EPS（実績/予想）
        # 行3: ROE・ROA・営業利益率
        # 行4: D/E比率・配当性向・ベータ
        grid_css = """display:grid;grid-template-columns:repeat(3,1fr);
                      gap:10px;margin-bottom:24px"""

        row1 = (
            _metric_box("時価総額", _fmt_mktcap(mktcap), "最新株価×発行済株数") +
            _metric_box("PER（実績）",
                        _f(per_ttm, "{:.1f}", "倍") if per_ttm and 0 < per_ttm < 500 else "—",
                        f"EPS {_f(eps_ttm, '{:.2f}', '円')}") +
            _metric_box("PBR",
                        _f(pbr, "{:.2f}", "倍") if pbr and 0 < pbr < 100 else "—",
                        f"BPS {_f(bps, '{:,.0f}', '円')}")
        )
        row2 = (
            _metric_box("配当利回り",
                        _f(div_yld, "{:.2f}", "%") if div_yld else "—",
                        f"年間配当 {_f(dps_use, '{:.0f}', '円')}",
                        color="#ffa657" if div_yld and div_yld >= 3 else "") +
            _metric_box("配当性向",
                        _pct(payout),
                        "内部留保との配分") +
            _metric_box("PER（予想）",
                        _f(per_fwd, "{:.1f}", "倍") if per_fwd and 0 < per_fwd < 500 else "—",
                        f"予想EPS {_f(eps_fwd, '{:.2f}', '円')}")
        )
        row3 = (
            _metric_box("ROE",
                        _pct(roe),
                        "自己資本利益率",
                        color=_signed_color(roe)) +
            _metric_box("ROA",
                        _pct(roa),
                        "総資産利益率",
                        color=_signed_color(roa)) +
            _metric_box("営業利益率",
                        _pct(op_mgn),
                        "本業の収益性",
                        color=_signed_color(op_mgn))
        )
        row4 = (
            _metric_box("純利益率",
                        _pct(pr_mgn),
                        "最終的な収益率",
                        color=_signed_color(pr_mgn)) +
            _metric_box("D/E比率",
                        _f(dte, "{:.1f}", "倍") if dte is not None else "—",
                        "有利子負債÷自己資本",
                        color="#E84040" if dte and dte > 150 else "#e6edf3") +
            _metric_box("ベータ",
                        _f(beta, "{:.2f}"),
                        "市場との連動性（1=同等）")
        )

        fund_updated = str(fund_row[15])[:10] if fund_row and fund_row[15] else "—"
        metrics_html = f"""
<h2 class="sect-h2">投資指標</h2>
<div style="{grid_css}">{row1}</div>
<div style="{grid_css}">{row2}</div>
<div style="{grid_css}">{row3}</div>
<div style="{grid_css}">{row4}</div>
<p style="font-size:11px;color:#484f58;text-align:right;margin-bottom:20px">
  ※ 指標データ更新: {fund_updated}　PER/PBR/時価総額は当日終値で再計算
</p>"""
    else:
        metrics_html = """
<h2 class="sect-h2">投資指標</h2>
<div class="alert" style="margin-bottom:20px">
  この銘柄の指標データはまだ取得されていません。
  テーマ登録銘柄は週次で自動更新されます。
</div>"""

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
            template="plotly_dark", height=360,
            margin=dict(l=50, r=10, t=10, b=30),
            xaxis_rangeslider_visible=False,
            font=dict(size=12),
        )
        chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn",
                                 config={"responsive": True})
    else:
        chart_html = '<p class="muted" style="padding:20px">価格データなし</p>'

    # ─ テーマバッジ ─
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
    theme_html = "".join(badges) if badges else '<span class="muted">なし</span>'

    # ─ 直近20日テーブル ─
    recent20 = list(reversed(prices[-20:]))
    trows = ""
    for p in recent20:
        c = float(p[6] or 0)
        cls = "up" if c > 0 else ("dn" if c < 0 else "")
        trows += (
            f'<tr>'
            f'<td class="left">{p[0]}</td>'
            f'<td>{float(p[4] or 0):,.0f}</td>'
            f'<td class="{cls}">{c:+.1f}%</td>'
            f'<td class="muted">{int(p[5] or 0):,}</td>'
            f'</tr>'
        )

    body = f"""\
<style>
.sect-h2 {{
  font-size:14px;font-weight:600;color:#e6edf3;
  border-bottom:1px solid #30363d;padding-bottom:8px;margin:20px 0 12px;
}}
@media(max-width:768px){{
  .metrics-grid {{ grid-template-columns: repeat(2,1fr) !important; }}
}}
</style>

<h1 style="font-size:22px;font-weight:700;color:#e6edf3;margin-bottom:4px">{s_name}</h1>
<p class="muted" style="font-size:13px;margin-bottom:16px">
  {s_code} &nbsp;|&nbsp; {market or "—"} &nbsp;|&nbsp; {sector or "—"}
</p>

<div style="display:flex;align-items:center;gap:14px;margin-bottom:24px;flex-wrap:wrap">
  <div>
    <span style="font-size:36px;font-weight:700;color:#e6edf3">{price_str}</span>
    <span class="{chg_cls}" style="font-size:20px;font-weight:600;margin-left:8px">{chg:+.1f}%</span>
    <span class="muted" style="font-size:12px;margin-left:6px">前日比</span>
  </div>
  <div class="muted" style="font-size:13px">
    出来高 {vol:,}株
  </div>
  <form method="POST" action="/watchlist/add" style="margin-left:auto">
    <input type="hidden" name="code" value="{s_code}">
    <input type="hidden" name="next" value="/stock/{s_code}">
    <button type="submit" class="btn-sm">⭐ ウォッチリストへ</button>
  </form>
</div>

{metrics_html}

<h2 class="sect-h2">所属テーマ</h2>
<div style="margin-bottom:20px">{theme_html}</div>

<h2 class="sect-h2">株価チャート（直近3ヶ月）</h2>
{chart_html}

<h2 class="sect-h2">直近20営業日</h2>
<div class="table-wrap">
  <div class="card">
    <table>
      <thead><tr>
        <th class="left">日付</th><th>終値</th><th>前日比</th><th>出来高</th>
      </tr></thead>
      <tbody>{trows}</tbody>
    </table>
  </div>
</div>"""

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
