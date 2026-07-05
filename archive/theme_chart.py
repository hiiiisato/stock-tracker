#!/usr/bin/env python3
from __future__ import annotations
"""
テーマ別資金フロー・過熱度チャート

【チャート種別】
  heat    : 全テーマの過熱スコアを横棒グラフ表示（デフォルト）
  index   : テーマ指数の時系列比較ライングラフ
  detail  : 1テーマの詳細（指数 + 資金流入比率 + 上昇銘柄比率）

【使い方】
  python theme_chart.py                         # 過熱スコアバーチャート
  python theme_chart.py heat                    # 同上
  python theme_chart.py index                   # 全テーマ指数比較（3ヶ月）
  python theme_chart.py index 6                 # 6ヶ月
  python theme_chart.py index SEMI AI_GEN       # 指定テーマのみ比較
  python theme_chart.py index SEMI AI_GEN 6     # 指定テーマ6ヶ月
  python theme_chart.py detail SEMI             # 半導体テーマ詳細（3ヶ月）
  python theme_chart.py detail SEMI 6           # 6ヶ月

【テーマ指定】
  コード（SEMI, AI_GEN, DEF_EQ …）または日本語名（半導体, 防衛装備 …）で指定可。
"""

import os
import sys
import subprocess
from datetime import date, timedelta
from config import get_conn
import plotly.graph_objects as go
from plotly.subplots import make_subplots

MONTHS_DEFAULT = 3

# テーマコード → 固有色（ダークテーマ上で視認しやすい配色）
_THEME_COLORS: dict[str, str] = {
    "AI_GEN":   "#FF6B6B",
    "AI_INFRA": "#FFA040",
    "SEMI":     "#FFD700",
    "CYBER":    "#00CED1",
    "ROBOT":    "#00E676",
    "CLOUD":    "#40C4FF",
    "PHYS_AI":  "#EA80FC",
    "DEF_EQ":   "#FF7043",
    "SPACE":    "#7C4DFF",
    "DRONE":    "#64B5F6",
    "RENEW":    "#69F0AE",
    "BATTERY":  "#F9A825",
    "HYDROGEN": "#B39DDB",
    "INBOUND":  "#F06292",
    "HEALTH_DX":"#80CBC4",
    "EV":       "#B2FF59",
}

# 親カテゴリ → 線種
_PARENT_DASH: dict[str, str] = {
    "TECH":     "solid",
    "DEFENSE":  "dash",
    "ENERGY":   "dot",
    "CONSUMER": "dashdot",
    "MOBILITY": "longdash",
}

_THEME_PARENT: dict[str, str] = {
    "AI_GEN": "TECH", "AI_INFRA": "TECH", "SEMI": "TECH",
    "CYBER": "TECH", "ROBOT": "TECH", "CLOUD": "TECH", "PHYS_AI": "TECH",
    "DEF_EQ": "DEFENSE", "SPACE": "DEFENSE", "DRONE": "DEFENSE",
    "RENEW": "ENERGY", "BATTERY": "ENERGY", "HYDROGEN": "ENERGY",
    "INBOUND": "CONSUMER", "HEALTH_DX": "CONSUMER",
    "EV": "MOBILITY",
}


# ═══════════════════════════════════════════════════════════
#  DB ヘルパー
# ═══════════════════════════════════════════════════════════

def _all_themes() -> list[dict]:
    """小分類テーマ（level=2）を全件取得"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, code, name
        FROM theme_categories
        WHERE level = 2 AND is_active = TRUE
        ORDER BY sort_order, id
    """)
    result = [{"id": r[0], "code": r[1], "name": r[2]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return result


def _find_themes(queries: list[str]) -> list[dict]:
    """
    コード（SEMI）または日本語名（半導体）のリストからテーマを検索。
    見つからない場合は警告を出してスキップ。
    """
    conn = get_conn()
    cur = conn.cursor()
    results = []
    seen_ids = set()
    for q in queries:
        cur.execute("""
            SELECT id, code, name FROM theme_categories
            WHERE level = 2 AND is_active = TRUE
              AND (code = %s OR name LIKE %s)
            LIMIT 1
        """, (q.upper(), f"%{q}%"))
        row = cur.fetchone()
        if row and row[0] not in seen_ids:
            results.append({"id": row[0], "code": row[1], "name": row[2]})
            seen_ids.add(row[0])
        elif not row:
            print(f"  [警告] テーマが見つかりません: {q}")
    cur.close()
    conn.close()
    return results


def _load_stats(theme_ids: list[int], months: int) -> dict[int, list[dict]]:
    """
    指定テーマの直近N ヶ月分の theme_daily_stats を取得。
    Returns: {theme_id: [{date, index_value, avg_change_pct, total_turnover,
                          net_turnover, turnover_surge, breadth_ratio, heat_score}]}
    """
    from_date = date.today() - timedelta(days=months * 31)
    conn = get_conn()
    cur = conn.cursor()

    placeholders = ",".join(["%s"] * len(theme_ids))
    cur.execute(f"""
        SELECT theme_id, date, index_value, avg_change_pct,
               total_turnover, net_turnover, turnover_surge, breadth_ratio, heat_score
        FROM theme_daily_stats
        WHERE theme_id IN ({placeholders}) AND date >= %s
        ORDER BY theme_id, date
    """, (*theme_ids, from_date))

    result: dict[int, list[dict]] = {tid: [] for tid in theme_ids}
    for row in cur.fetchall():
        tid, dt, idx, chg, tot_t, net_t, surge, breadth, heat = row
        result[int(tid)].append({
            "date":          dt,
            "index_value":   float(idx   or 100),
            "avg_change_pct":float(chg   or 0),
            "total_turnover":int(tot_t   or 0),
            "net_turnover":  int(net_t   or 0),
            "turnover_surge":float(surge or 1),
            "breadth_ratio": float(breadth or 0.5),
            "heat_score":    float(heat  or 0),
        })
    cur.close()
    conn.close()
    return result


def _latest_stats() -> list[dict]:
    """全テーマの最新日統計を取得（heat チャート用）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT tds.theme_id, tc.name, tc.code,
               tds.index_value, tds.heat_score,
               tds.turnover_surge, tds.breadth_ratio, tds.date
        FROM theme_daily_stats tds
        JOIN theme_categories tc ON tds.theme_id = tc.id
        WHERE tds.date = (SELECT MAX(date) FROM theme_daily_stats)
          AND tc.level = 2
        ORDER BY tds.heat_score DESC
    """)
    result = []
    for row in cur.fetchall():
        tid, name, code, idx, heat, surge, breadth, dt = row
        result.append({
            "theme_id":      int(tid),
            "name":          name,
            "code":          code,
            "index_value":   float(idx    or 100),
            "heat_score":    float(heat   or 0),
            "turnover_surge":float(surge  or 1),
            "breadth_ratio": float(breadth or 0.5),
            "date":          dt,
        })
    cur.close()
    conn.close()
    return result


# ═══════════════════════════════════════════════════════════
#  チャート生成
# ═══════════════════════════════════════════════════════════

def heat_chart(out_path: str) -> None:
    """
    全テーマの過熱スコアを横棒グラフで表示。
    スコアが高いほど強気・資金流入、低いほど弱気。
    """
    stats = _latest_stats()
    if not stats:
        print("  データがありません")
        return

    # 過熱スコア順（下から高いほうへ表示されるよう逆順）
    stats_rev = list(reversed(stats))
    names  = [s["name"] for s in stats_rev]
    heats  = [s["heat_score"] for s in stats_rev]
    idxs   = [s["index_value"] for s in stats_rev]
    surges = [s["turnover_surge"] for s in stats_rev]
    breadths = [s["breadth_ratio"] * 100 for s in stats_rev]

    colors = ["#E84040" if h >= 0 else "#2CA02C" for h in heats]

    # カスタムテキスト（ホバー詳細）
    hover = [
        f"<b>{s['name']}</b><br>"
        f"過熱スコア: {s['heat_score']:+.2f}<br>"
        f"テーマ指数: {s['index_value']:.1f}<br>"
        f"資金流入比率: {s['turnover_surge']:.2f}x<br>"
        f"上昇銘柄比率: {s['breadth_ratio']*100:.0f}%"
        for s in stats_rev
    ]

    fig = go.Figure(go.Bar(
        x=heats,
        y=names,
        orientation="h",
        marker_color=colors,
        text=[f"{h:+.2f}" for h in heats],
        textposition="outside",
        hovertext=hover,
        hoverinfo="text",
    ))

    latest_date = stats[0]["date"] if stats else ""
    fig.update_layout(
        title=f"テーマ別 過熱スコア（{latest_date}）<br>"
              f"<sub>正=強気・資金流入 / 負=弱気・資金流出　"
              f"構成: 週間リターン40% + 資金流入比率40% + 騰落広がり20%</sub>",
        xaxis_title="過熱スコア",
        xaxis=dict(zeroline=True, zerolinewidth=2, zerolinecolor="white"),
        template="plotly_dark",
        height=max(400, len(stats) * 45),
        margin=dict(l=160, r=80, t=100, b=60),
        font=dict(size=13),
        showlegend=False,
    )
    fig.write_html(out_path)
    print(f"  過熱スコアチャート: {out_path}")


def index_chart(themes: list[dict], months: int, out_path: str) -> None:
    """
    複数テーマの累積指数を比較ラインチャートで表示。
    初日=100 として各テーマの相対パフォーマンスを比較できる。
    """
    theme_ids = [t["id"] for t in themes]
    stats_map = _load_stats(theme_ids, months)

    fig = go.Figure()
    for t in themes:
        data = stats_map.get(t["id"], [])
        if not data:
            continue
        # 表示期間の最初を100に再基準化
        base = data[0]["index_value"]
        dates  = [d["date"] for d in data]
        values = [d["index_value"] / base * 100 for d in data]
        hover  = [
            f"<b>{t['name']}</b><br>"
            f"指数: {d['index_value']:.1f}<br>"
            f"期間基準比: {d['index_value']/base*100:.1f}<br>"
            f"過熱スコア: {d['heat_score']:+.2f}<br>"
            f"資金流入比: {d['turnover_surge']:.2f}x"
            for d in data
        ]
        code  = t.get("code", "")
        color = _THEME_COLORS.get(code, "#AAAAAA")
        dash  = _PARENT_DASH.get(_THEME_PARENT.get(code, ""), "solid")
        fig.add_trace(go.Scatter(
            x=dates, y=values,
            mode="lines",
            name=t["name"],
            hovertext=hover,
            hoverinfo="text",
            line=dict(color=color, width=2, dash=dash),
        ))

    fig.add_hline(y=100, line_dash="dot", line_color="gray", opacity=0.5)
    fig.update_layout(
        title=f"テーマ指数 比較チャート（期間初=100）　直近{months}ヶ月",
        xaxis_title="日付",
        yaxis_title="相対指数（期間初=100）",
        template="plotly_dark",
        height=550,
        font=dict(size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
    )
    fig.write_html(out_path)
    print(f"  テーマ指数チャート: {out_path}")


def detail_chart(theme: dict, months: int, out_path: str) -> None:
    """
    1テーマの詳細チャート。
    Panel 1: テーマ指数（累積パフォーマンス）
    Panel 2: 資金流入比率（直近20日平均比）
    Panel 3: 上昇銘柄比率（Breadth）
    """
    data_map = _load_stats([theme["id"]], months)
    data = data_map.get(theme["id"], [])
    if not data:
        print(f"  データがありません: {theme['name']}")
        return

    dates   = [d["date"]            for d in data]
    indexes = [d["index_value"]     for d in data]
    surges  = [d["turnover_surge"]  for d in data]
    breadths= [d["breadth_ratio"]*100 for d in data]
    heats   = [d["heat_score"]      for d in data]

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.50, 0.25, 0.25],
        subplot_titles=(
            "テーマ指数（初日=100）",
            f"資金流入比率（直近{20}日平均比）",
            "上昇銘柄比率（%）",
        ),
    )

    # Panel 1: 指数ライン + 過熱スコアを背景色で表現
    fig.add_trace(go.Scatter(
        x=dates, y=indexes,
        mode="lines",
        name="テーマ指数",
        line=dict(color="#5B9BD5", width=2.5),
        hovertemplate="%{x}<br>指数: %{y:.1f}<extra></extra>",
    ), row=1, col=1)

    # Panel 2: 資金流入比率（1.0ラインに水平線）
    surge_colors = ["#E84040" if s >= 1 else "#2CA02C" for s in surges]
    fig.add_trace(go.Bar(
        x=dates, y=[s - 1 for s in surges],  # 0基準で表示
        name="資金流入比率",
        marker_color=surge_colors,
        opacity=0.75,
        hovertemplate="%{x}<br>対平均比: %{customdata:.2f}x<extra></extra>",
        customdata=surges,
    ), row=2, col=1)

    # Panel 3: Breadth（50%ラインで中立）
    breadth_colors = ["#E84040" if b >= 50 else "#2CA02C" for b in breadths]
    fig.add_trace(go.Bar(
        x=dates, y=breadths,
        name="上昇銘柄比率",
        marker_color=breadth_colors,
        opacity=0.75,
        hovertemplate="%{x}<br>上昇比率: %{y:.0f}%<extra></extra>",
    ), row=3, col=1)

    # 参照ライン
    fig.add_hline(y=0,   line_dash="dot", line_color="gray", opacity=0.4, row=2, col=1)
    fig.add_hline(y=50,  line_dash="dot", line_color="gray", opacity=0.4, row=3, col=1)

    latest = data[-1]
    fig.update_layout(
        title=(
            f"{theme['name']}（{theme['code']}）　詳細チャート　直近{months}ヶ月<br>"
            f"<sub>最新: 指数={latest['index_value']:.1f} / "
            f"資金比={latest['turnover_surge']:.2f}x / "
            f"上昇率={latest['breadth_ratio']*100:.0f}% / "
            f"過熱スコア={latest['heat_score']:+.2f}</sub>"
        ),
        template="plotly_dark",
        height=700,
        font=dict(size=12),
        showlegend=False,
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="指数",  row=1, col=1)
    fig.update_yaxes(title_text="対平均比-1", row=2, col=1)
    fig.update_yaxes(title_text="%", ticksuffix="%", row=3, col=1)

    fig.write_html(out_path)
    print(f"  詳細チャート: {out_path}")


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

def _open_browser(path: str) -> None:
    subprocess.run(["open", path], check=False)


def main():
    args = sys.argv[1:]
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # 末尾が 1〜24 の数値なら months 指定
    months = MONTHS_DEFAULT
    if args and args[-1].isdigit() and 1 <= int(args[-1]) <= 24:
        months = int(args[-1])
        args = args[:-1]

    # コマンド判定（先頭引数が heat/index/detail のいずれか）
    cmd = "heat"
    if args and args[0].lower() in ("heat", "index", "detail"):
        cmd = args[0].lower()
        args = args[1:]

    # ── heat ──────────────────────────────────────────
    if cmd == "heat":
        out = os.path.join(base_dir, "theme_heat.html")
        heat_chart(out)
        _open_browser(out)

    # ── index ─────────────────────────────────────────
    elif cmd == "index":
        if args:
            themes = _find_themes(args)
        else:
            themes = _all_themes()

        if not themes:
            print("有効なテーマが見つかりませんでした")
            sys.exit(1)

        codes = "_".join(t["code"] for t in themes[:5])
        out = os.path.join(base_dir, f"theme_index_{codes}.html")
        index_chart(themes, months, out)
        _open_browser(out)

    # ── detail ────────────────────────────────────────
    elif cmd == "detail":
        if not args:
            print("テーマコードまたはテーマ名を指定してください")
            print("  例: python theme_chart.py detail SEMI")
            sys.exit(1)

        themes = _find_themes(args[:1])
        if not themes:
            print(f"テーマが見つかりません: {args[0]}")
            sys.exit(1)

        t = themes[0]
        out = os.path.join(base_dir, f"theme_detail_{t['code']}.html")
        detail_chart(t, months, out)
        _open_browser(out)


if __name__ == "__main__":
    main()
