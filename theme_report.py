#!/usr/bin/env python3
from __future__ import annotations
"""
テーマ別資金フローレポート生成

生成物: theme_report_YYYY-MM-DD.html（単一HTMLファイル）

【レポート構成】
  1. 過熱スコアバーチャート（全テーマ横並び）
  2. テーマ指数推移ライングラフ（直近約1ヶ月）
  3. テーマサマリーテーブル（1日/5日/20日リターン、資金流入比等）
  4. テーマ別詳細カード（牽引銘柄を5日リターン上位で表示）

【使い方】
  python theme_report.py              # 直近取引日のレポート
  python theme_report.py 2026-06-26   # 指定日のレポート
"""

import os, sys, subprocess
from datetime import date, timedelta
from collections import defaultdict
import plotly.graph_objects as go
from config import get_conn

# ═══════════════════════════════════════════════════════════
#  設定
# ═══════════════════════════════════════════════════════════

TREND_DAYS   = 20   # 指数トレンドグラフの表示期間（営業日）
STOCK_BUFFER = 35   # 銘柄データ取得バッファ（カレンダー日数、20営業日分を確保）
TOP_N        = 5    # テーマごとに表示する銘柄数
RELEVANCE_LABEL = {3: "コア", 2: "関連", 1: "周辺"}

# テーマコード → 固有色（ダークテーマ上で視認しやすい配色）
_THEME_COLORS: dict[str, str] = {
    "AI_GEN":   "#FF6B6B",  # 赤橙    TECH
    "AI_INFRA": "#FFA040",  # オレンジ TECH
    "SEMI":     "#FFD700",  # ゴールド TECH
    "CYBER":    "#00CED1",  # ターコイズ TECH
    "ROBOT":    "#00E676",  # 緑      TECH
    "CLOUD":    "#40C4FF",  # 水色    TECH
    "PHYS_AI":  "#EA80FC",  # ピンク  TECH
    "DEF_EQ":   "#FF7043",  # 深オレ  DEFENSE
    "SPACE":    "#7C4DFF",  # 紫      DEFENSE
    "DRONE":    "#64B5F6",  # 薄青    DEFENSE
    "RENEW":    "#69F0AE",  # 薄緑    ENERGY
    "BATTERY":  "#F9A825",  # アンバー ENERGY
    "HYDROGEN": "#B39DDB",  # 薄紫    ENERGY
    "INBOUND":  "#F06292",  # ローズ  CONSUMER
    "HEALTH_DX":"#80CBC4",  # ティール CONSUMER
    "EV":       "#B2FF59",  # ライム  MOBILITY
}

# 親カテゴリ → 線種（補助的な判別手段）
_PARENT_DASH: dict[str, str] = {
    "TECH":     "solid",
    "DEFENSE":  "dash",
    "ENERGY":   "dot",
    "CONSUMER": "dashdot",
    "MOBILITY": "longdash",
}

# テーマコード → 親カテゴリ
_THEME_PARENT: dict[str, str] = {
    "AI_GEN": "TECH", "AI_INFRA": "TECH", "SEMI": "TECH",
    "CYBER": "TECH", "ROBOT": "TECH", "CLOUD": "TECH", "PHYS_AI": "TECH",
    "DEF_EQ": "DEFENSE", "SPACE": "DEFENSE", "DRONE": "DEFENSE",
    "RENEW": "ENERGY", "BATTERY": "ENERGY", "HYDROGEN": "ENERGY",
    "INBOUND": "CONSUMER", "HEALTH_DX": "CONSUMER",
    "EV": "MOBILITY",
}


# ═══════════════════════════════════════════════════════════
#  データロード
# ═══════════════════════════════════════════════════════════

def _load_theme_series(report_date: date):
    """
    テーマ時系列データを取得。
    Returns: (meta, series)
      meta   : {theme_id: {id, name, code}}
      series : {theme_id: [{date, index_value, heat_score, ...}]}  ← 日付昇順
    """
    from_dt = report_date - timedelta(days=TREND_DAYS * 3)  # 営業日を確保するため余裕を持つ
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT tds.theme_id, tc.name, tc.code, tds.date,
               tds.index_value, tds.heat_score, tds.turnover_surge,
               tds.breadth_ratio, tds.avg_change_pct, tds.total_turnover
        FROM theme_daily_stats tds
        JOIN theme_categories tc ON tds.theme_id = tc.id
        WHERE tc.level = 2 AND tds.date BETWEEN %s AND %s
        ORDER BY tds.theme_id, tds.date
    """, (from_dt, report_date))

    meta   = {}
    series = defaultdict(list)
    for tid, name, code, dt, idx, heat, surge, breadth, chg, t_t in cur.fetchall():
        if tid not in meta:
            meta[tid] = {"id": int(tid), "name": name, "code": code}
        series[tid].append({
            "date":          dt,
            "index_value":   float(idx    or 100),
            "heat_score":    float(heat   or 0),
            "turnover_surge":float(surge  or 1),
            "breadth_ratio": float(breadth or 0.5),
            "avg_change_pct":float(chg    or 0),
            "total_turnover":int(t_t      or 0),
        })
    cur.close()
    conn.close()
    return meta, dict(series)


def _load_stock_data(report_date: date):
    """
    テーマ銘柄の過去N日分の価格データを一括取得。
    Returns: (stock_meta, stock_series)
      stock_meta   : {theme_id: {code: {name, relevance}}}
      stock_series : {theme_id: {code: [{date, close, change_pct, turnover}]}}
    """
    from_dt = report_date - timedelta(days=STOCK_BUFFER)
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT st.theme_id, st.code, s.name, st.relevance,
               dp.date, dp.close, dp.change_pct,
               COALESCE(dp.turnover, dp.volume * dp.close) AS t_val
        FROM stock_themes st
        JOIN stocks s ON s.code = st.code
        JOIN theme_categories tc ON st.theme_id = tc.id
        JOIN daily_prices dp ON dp.code = st.code
        WHERE tc.level = 2 AND s.is_active = TRUE
          AND dp.date BETWEEN %s AND %s
          AND dp.close IS NOT NULL
        ORDER BY st.theme_id, st.code, dp.date
    """, (from_dt, report_date))

    stock_meta   = defaultdict(dict)
    stock_series = defaultdict(lambda: defaultdict(list))
    for tid, code, name, rel, dt, close, chg, t_val in cur.fetchall():
        stock_meta[tid][code]             = {"name": name, "relevance": int(rel)}
        stock_series[tid][code].append({
            "date":       dt,
            "close":      float(close),
            "change_pct": float(chg   or 0),
            "turnover":   int(t_val   or 0),
        })
    cur.close()
    conn.close()
    return dict(stock_meta), dict(stock_series)


# ═══════════════════════════════════════════════════════════
#  統計計算
# ═══════════════════════════════════════════════════════════

def _idx_n_ago(series: list, n: int) -> float:
    """時系列リスト(日付昇順)の n 営業日前の index_value（不足時は最古値）"""
    return series[max(0, len(series) - 1 - n)]["index_value"]


def _compute_theme_stats(meta: dict, series_dict: dict) -> list:
    """テーマ別集計統計を計算して heat_score 降順で返す"""
    result = []
    for tid, m in meta.items():
        s = series_dict.get(tid, [])
        if not s:
            continue
        latest  = s[-1]
        idx_now = latest["index_value"]
        result.append({
            **m,
            "index_value":    idx_now,
            "heat_score":     latest["heat_score"],
            "turnover_surge": latest["turnover_surge"],
            "breadth_ratio":  latest["breadth_ratio"],
            "total_turnover": latest["total_turnover"],
            "ret_1d":  latest["avg_change_pct"],
            "ret_5d":  (idx_now / _idx_n_ago(s, 5)  - 1) * 100,
            "ret_20d": (idx_now / _idx_n_ago(s, 20) - 1) * 100,
            "series":  s,
        })
    result.sort(key=lambda x: x["heat_score"], reverse=True)
    return result


def _compute_leading_stocks(
    theme_stats: list,
    stock_meta:  dict,
    stock_series: dict,
    report_date: date,
) -> dict:
    """テーマごとの牽引銘柄（5日リターン降順 TOP_N）を返す"""
    result = {}
    for t in theme_stats:
        tid    = t["id"]
        stocks = []
        for code, ss in stock_series.get(tid, {}).items():
            if not ss:
                continue
            today = next((d for d in reversed(ss) if d["date"] == report_date), None)
            if not today:
                continue
            n       = len(ss) - 1
            c5      = ss[max(0, n - 5)]["close"]
            c20     = ss[max(0, n - 20)]["close"]
            ret_5d  = (today["close"] / c5  - 1) * 100 if c5  else 0
            ret_20d = (today["close"] / c20 - 1) * 100 if c20 else 0
            smeta   = stock_meta.get(tid, {}).get(code, {})
            stocks.append({
                "code":      code,
                "name":      smeta.get("name", code),
                "relevance": smeta.get("relevance", 1),
                "close":     today["close"],
                "ret_1d":    today["change_pct"],
                "ret_5d":    ret_5d,
                "ret_20d":   ret_20d,
                "turnover":  today["turnover"],
            })
        stocks.sort(key=lambda x: x["ret_5d"], reverse=True)
        result[tid] = stocks[:TOP_N]
    return result


# ═══════════════════════════════════════════════════════════
#  plotly チャート（HTML fragment）
# ═══════════════════════════════════════════════════════════

def _heat_chart_html(theme_stats: list) -> str:
    rev    = list(reversed(theme_stats))
    names  = [t["name"]       for t in rev]
    heats  = [t["heat_score"] for t in rev]
    colors = ["#E84040" if h >= 0 else "#3A9FE0" for h in heats]
    hover  = [
        (f"<b>{t['name']}</b><br>"
         f"過熱スコア: {t['heat_score']:+.2f}<br>"
         f"テーマ指数: {t['index_value']:.1f}（期初=100）<br>"
         f"1日: {t['ret_1d']:+.1f}%　5日: {t['ret_5d']:+.1f}%　20日: {t['ret_20d']:+.1f}%<br>"
         f"資金流入比: {t['turnover_surge']:.2f}x　上昇銘柄率: {t['breadth_ratio']*100:.0f}%")
        for t in rev
    ]
    fig = go.Figure(go.Bar(
        x=heats, y=names, orientation="h",
        marker_color=colors,
        text=[f"{h:+.2f}" for h in heats],
        textposition="outside",
        hovertext=hover, hoverinfo="text",
    ))
    fig.add_vline(x=0, line_width=1.5, line_color="#555", opacity=0.8)
    fig.update_layout(
        title="テーマ過熱スコア",
        xaxis_title="スコア（正=強気・資金流入 / 負=弱気・資金流出）",
        template="plotly_dark",
        height=500,
        margin=dict(l=150, r=90, t=50, b=40),
        font=dict(size=12),
        showlegend=False,
    )
    return fig.to_html(full_html=False, include_plotlyjs=False,
                       config={"responsive": True})


def _trend_chart_html(theme_stats: list) -> str:
    fig = go.Figure()
    for t in theme_stats:
        s      = t.get("series", [])
        recent = s[-TREND_DAYS:] if len(s) >= TREND_DAYS else s
        if not recent:
            continue
        base   = recent[0]["index_value"]
        dates  = [d["date"] for d in recent]
        values = [d["index_value"] / base * 100 for d in recent]
        code   = t.get("code", "")
        color  = _THEME_COLORS.get(code, "#AAAAAA")
        dash   = _PARENT_DASH.get(_THEME_PARENT.get(code, ""), "solid")
        fig.add_trace(go.Scatter(
            x=dates, y=values, mode="lines",
            name=t["name"],
            line=dict(color=color, width=2, dash=dash),
            hovertemplate=(
                f"<b>{t['name']}</b><br>%{{x}}<br>"
                f"期間基準比: %{{y:.1f}}<extra></extra>"
            ),
        ))
    fig.add_hline(y=100, line_dash="dot", line_color="#888", opacity=0.4)
    fig.update_layout(
        title=f"テーマ指数推移（直近{TREND_DAYS}営業日、期間初=100）",
        yaxis_title="相対指数",
        template="plotly_dark",
        height=500,
        margin=dict(l=55, r=20, t=50, b=40),
        font=dict(size=11),
        hovermode="x unified",
        legend=dict(orientation="v", x=1.01, y=1, font=dict(size=9)),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False,
                       config={"responsive": True})


# ═══════════════════════════════════════════════════════════
#  HTML 部品（f-string 内のネストクォートを避けるため _c() を使用）
# ═══════════════════════════════════════════════════════════

def _c(val: float) -> str:
    """数値に対する CSS クラス名（日本株: 上昇=赤/pos, 下落=青/neg）"""
    return "pos" if val > 0 else ("neg" if val < 0 else "")


def _pct(val: float) -> str:
    """騰落率を色付きセルで返す"""
    cls = _c(val)
    return f'<td class="num {cls}">{val:+.1f}%</td>'


def _stock_row(s: dict) -> str:
    rel   = s["relevance"]
    label = RELEVANCE_LABEL.get(rel, "")
    t_val = s["turnover"]
    t_str = (f"{t_val/1e8:.1f}億" if t_val >= 1e8 else
             f"{t_val/1e4:.0f}万"  if t_val >= 1e4 else
             f"{t_val:,}")
    return (
        f'<tr>'
        f'<td class="code"><a href="/stock/{s["code"]}" style="color:#79c0ff;text-decoration:none">{s["code"]}</a></td>'
        f'<td class="sname">{s["name"]}</td>'
        f'<td><span class="rel rel{rel}">{label}</span></td>'
        f'<td class="num price">{s["close"]:,.0f}</td>'
        f'{_pct(s["ret_1d"])}'
        f'{_pct(s["ret_5d"])}'
        f'{_pct(s["ret_20d"])}'
        f'<td class="num tval">{t_str}</td>'
        f'</tr>'
    )


def _theme_card(t: dict, stocks: list) -> str:
    heat      = t["heat_score"]
    badge     = "🔥" if heat > 3 else "↗" if heat > 0 else "↘" if heat > -3 else "❄"
    heat_cls  = _c(heat)
    r1_cls    = _c(t["ret_1d"])
    r5_cls    = _c(t["ret_5d"])
    r20_cls   = _c(t["ret_20d"])
    t_str     = (f"{t['total_turnover']/1e8:.0f}億円"
                 if t["total_turnover"] >= 1e8 else
                 f"{t['total_turnover']/1e4:.0f}万円")
    rows_html = "\n".join(_stock_row(s) for s in stocks)
    surge_cls = "pos" if t["turnover_surge"] >= 1.2 else ("neg" if t["turnover_surge"] < 0.8 else "")
    return f"""
<div class="theme-card">
  <div class="theme-header">
    <span class="badge">{badge}</span>
    <span class="theme-name">{t['name']}</span>
    <span class="heat-val {heat_cls}">{heat:+.2f}</span>
  </div>
  <div class="theme-stats">
    <span>指数 <b>{t['index_value']:.1f}</b></span>
    <span>1日 <b class="{r1_cls}">{t['ret_1d']:+.1f}%</b></span>
    <span>5日 <b class="{r5_cls}">{t['ret_5d']:+.1f}%</b></span>
    <span>20日 <b class="{r20_cls}">{t['ret_20d']:+.1f}%</b></span>
    <span>資金比 <b class="{surge_cls}">{t['turnover_surge']:.2f}x</b></span>
    <span>上昇率 <b>{t['breadth_ratio']*100:.0f}%</b></span>
    <span>売買代金 <b>{t_str}</b></span>
  </div>
  <table class="stock-table">
    <thead>
      <tr>
        <th>コード</th><th class="left">銘柄名</th><th>分類</th>
        <th class="num">株価</th><th class="num">1日</th>
        <th class="num">5日</th><th class="num">20日</th>
        <th class="num">売買代金</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</div>"""


def _summary_row(t: dict) -> str:
    hcls = _c(t["heat_score"])
    return (
        f'<tr>'
        f'<td class="left">{t["name"]}</td>'
        f'<td class="num {hcls}">{t["heat_score"]:+.2f}</td>'
        f'{_pct(t["ret_1d"])}'
        f'{_pct(t["ret_5d"])}'
        f'{_pct(t["ret_20d"])}'
        f'<td class="num">{t["turnover_surge"]:.2f}x</td>'
        f'<td class="num">{t["breadth_ratio"]*100:.0f}%</td>'
        f'<td class="num">{t["index_value"]:.1f}</td>'
        f'</tr>'
    )


# ═══════════════════════════════════════════════════════════
#  CSS
# ═══════════════════════════════════════════════════════════

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #c9d1d9; font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 13px; line-height: 1.5; }
.container { max-width: 1500px; margin: 0 auto; padding: 20px 16px; }
h1 { color: #e6edf3; font-size: 22px; margin-bottom: 4px; }
.subtitle { color: #8b949e; font-size: 12px; margin-bottom: 24px; }
h2 { color: #e6edf3; font-size: 16px; margin: 28px 0 12px;
     border-bottom: 1px solid #30363d; padding-bottom: 8px; }

/* チャート横並び */
.charts-row { display: flex; gap: 16px; margin-bottom: 24px; }
.chart-box  { flex: 1; min-width: 0; background: #161b22; border: 1px solid #30363d;
              border-radius: 8px; padding: 8px; overflow: hidden; }

/* サマリーテーブル */
.summary-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.summary-table { width: 100%; border-collapse: collapse;
                 background: #161b22; border-radius: 8px; overflow: hidden; }
.summary-table th { background: #21262d; padding: 8px 12px; color: #8b949e;
                    font-size: 11px; font-weight: 600; text-align: right; white-space: nowrap; }
.summary-table th.left { text-align: left; }
.summary-table td { padding: 7px 12px; border-bottom: 1px solid #1c2128; white-space: nowrap; }
.summary-table td.left { text-align: left; font-weight: 500; }
.summary-table tr:last-child td { border-bottom: none; }
.summary-table tr:hover { background: #1c2128; }

/* テーマカードグリッド */
.theme-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(660px, 1fr)); gap: 16px; }
.theme-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
.theme-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.badge { font-size: 20px; }
.theme-name { font-size: 15px; font-weight: 600; color: #e6edf3; }
.heat-val { font-size: 20px; font-weight: 700; margin-left: auto; }
.theme-stats { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px;
               color: #8b949e; background: #0d1117; border-radius: 6px; padding: 8px 12px;
               margin-bottom: 10px; }
.theme-stats b { color: #c9d1d9; }

/* 銘柄テーブル */
.stock-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.stock-table th { background: #21262d; padding: 5px 8px; color: #8b949e;
                  font-size: 11px; font-weight: 600; white-space: nowrap; text-align: right; }
.stock-table th.left { text-align: left; }
.stock-table td { padding: 5px 8px; border-bottom: 1px solid #1c2128; }
.stock-table td.code  { color: #79c0ff; font-family: monospace; text-align: left; }
.stock-table td.sname { text-align: left; max-width: 200px; white-space: nowrap;
                        overflow: hidden; text-overflow: ellipsis; }
.stock-table td.price { color: #8b949e; text-align: right; }
.stock-table td.tval  { color: #8b949e; text-align: right; }
.stock-table tr:last-child td { border-bottom: none; }
.stock-table tr:hover { background: #1c2128; }

/* 分類バッジ */
.rel { font-size: 10px; border-radius: 3px; padding: 1px 5px; font-weight: 600; }
.rel3 { background: #3d1f00; color: #ffa657; }
.rel2 { background: #122012; color: #56d364; }
.rel1 { background: #1c2128; color: #6e7681; }

/* 共通 */
.num  { text-align: right; }
.left { text-align: left; }
.pos  { color: #E84040; }
.neg  { color: #3A9FE0; }

/* ── アプリ共通ナビバー ─────────────────────────── */
.app-nav {
  background: #161b22; border-bottom: 1px solid #30363d;
  padding: 0 20px; position: sticky; top: 0; z-index: 200;
  display: flex; align-items: center; height: 48px; gap: 0;
}
.app-nav-logo {
  font-weight: 700; font-size: 15px; color: #e6edf3;
  text-decoration: none; margin-right: 20px; white-space: nowrap;
}
.app-nav-logo:hover { text-decoration: none; color: #e6edf3; }
.app-nav-links { display: flex; align-items: center; gap: 4px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.app-nav-links::-webkit-scrollbar { display: none; }
.app-nav-link {
  color: #8b949e; font-size: 13px; text-decoration: none;
  padding: 0 10px; height: 48px; display: flex; align-items: center;
  border-bottom: 2px solid transparent; white-space: nowrap;
}
.app-nav-link:hover { color: #e6edf3; text-decoration: none; }
.app-nav-link.active { color: #e6edf3; border-bottom-color: #1f6feb; }

/* ── モバイル対応 (〜768px) ─────────────────────── */
@media (max-width: 768px) {
  .container { padding: 12px 10px; }
  h1 { font-size: 17px; }
  h2 { font-size: 14px; margin: 18px 0 10px; }
  .subtitle { font-size: 11px; margin-bottom: 14px; }

  /* チャートを縦積みに */
  .charts-row { flex-direction: column; gap: 10px; }

  /* テーマカードを1列に */
  .theme-grid { grid-template-columns: 1fr; gap: 10px; }
  .theme-card { padding: 10px; }
  .theme-name { font-size: 13px; }
  .heat-val   { font-size: 16px; }
  .theme-stats { gap: 8px; font-size: 11px; padding: 6px 10px; }
  .badge { font-size: 16px; }

  /* 銘柄テーブル: モバイルで売買代金列を非表示 */
  .stock-table .tval, .stock-table th:last-child { display: none; }
  .stock-table td { padding: 4px 6px; }
  .stock-table { font-size: 11px; }
  .stock-table td.sname { max-width: 120px; }
}
"""


# ═══════════════════════════════════════════════════════════
#  レポート組み立て・出力
# ═══════════════════════════════════════════════════════════

def generate_report(report_date: date, out_path: str = None) -> str:
    """
    テーマ別資金フローレポートを生成してHTMLを返す。
    out_path を指定した場合はファイルにも書き出す。
    """
    print("  テーマ時系列データを取得中...")
    meta, series_dict = _load_theme_series(report_date)

    print("  銘柄データを取得中...")
    stock_meta, stock_series = _load_stock_data(report_date)

    print("  統計を計算中...")
    theme_stats = _compute_theme_stats(meta, series_dict)
    leading     = _compute_leading_stocks(theme_stats, stock_meta, stock_series, report_date)

    print("  チャートを生成中...")
    heat_html  = _heat_chart_html(theme_stats)
    trend_html = _trend_chart_html(theme_stats)

    print("  HTMLを組み立て中...")
    summary_rows   = "\n".join(_summary_row(t) for t in theme_stats)
    theme_cards    = "\n".join(_theme_card(t, leading.get(t["id"], [])) for t in theme_stats)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>テーマ別資金フローレポート {report_date}</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>{CSS}</style>
</head>
<body>
<nav class="app-nav">
  <a class="app-nav-logo" href="/">📈 株式分析</a>
  <div class="app-nav-links">
    <a class="app-nav-link" href="/">ホーム</a>
    <a class="app-nav-link" href="/screen">スクリーニング</a>
    <a class="app-nav-link" href="/valuation">理論株価</a>
    <a class="app-nav-link active" href="/themes">テーマ</a>
    <a class="app-nav-link" href="/rankings">ランキング</a>
    <a class="app-nav-link" href="/events">イベント</a>
    <a class="app-nav-link" href="/disclosures">適時開示</a>
    <a class="app-nav-link" href="/funds">ファンドウォッチ</a>
    <a class="app-nav-link" href="/swing">スイング</a>
    <a class="app-nav-link" href="/watchlist">ウォッチリスト</a>
  </div>
</nav>
<div class="container">

  <h1>テーマ別資金フローレポート</h1>
  <p class="subtitle">
    {report_date} 時点 &nbsp;|&nbsp;
    過熱スコア = 週間リターン×40% + 資金流入比率×40% + 騰落広がり×20% &nbsp;|&nbsp;
    <span class="pos">■ 上昇（赤）</span> / <span class="neg">■ 下落（青）</span>（日本株表示）
  </p>

  <!-- ① チャート2本 -->
  <div class="charts-row">
    <div class="chart-box">{heat_html}</div>
    <div class="chart-box">{trend_html}</div>
  </div>

  <!-- ② サマリーテーブル -->
  <h2>テーマサマリー（過熱スコア順）</h2>
  <div class="summary-wrap">
    <table class="summary-table">
      <thead>
        <tr>
          <th class="left">テーマ</th>
          <th class="num">過熱スコア</th>
          <th class="num">1日リターン</th>
          <th class="num">5日リターン</th>
          <th class="num">20日リターン</th>
          <th class="num">資金流入比</th>
          <th class="num">上昇銘柄率</th>
          <th class="num">テーマ指数</th>
        </tr>
      </thead>
      <tbody>
        {summary_rows}
      </tbody>
    </table>
  </div>

  <!-- ③ テーマ別詳細カード（牽引銘柄 5日リターン上位） -->
  <h2>テーマ別詳細 ／ 牽引銘柄（5日リターン上位）</h2>
  <div class="theme-grid">
    {theme_cards}
  </div>

</div>
</body>
</html>"""

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        size_kb = len(html.encode()) // 1024
        print(f"  完了: {out_path} ({size_kb} KB)")

    return html


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

def main():
    if len(sys.argv) > 1:
        try:
            report_date = date.fromisoformat(sys.argv[1])
        except ValueError:
            print(f"日付形式エラー: {sys.argv[1]}（例: 2026-06-26）")
            sys.exit(1)
    else:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT MAX(date) FROM theme_daily_stats")
        report_date = cur.fetchone()[0]
        cur.close()
        conn.close()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(base_dir, f"theme_report_{report_date}.html")

    print(f"\n=== テーマ別資金フローレポート 生成 ===")
    print(f"  対象日: {report_date}")
    generate_report(report_date, out_path)
    subprocess.run(["open", out_path], check=False)


if __name__ == "__main__":
    main()
