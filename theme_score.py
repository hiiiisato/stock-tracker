#!/usr/bin/env python3
from __future__ import annotations
"""
テーマ別日次統計・スコアの計算とDB保存

【計算内容】
  index_value   : 期間初日=100の累積テーマ指数（relevance加重平均リターンを毎日複利積み上げ）
  total_turnover: テーマ銘柄の合計売買代金
  net_turnover  : ネットフロー推計（上昇銘柄の売買代金 − 下落銘柄の売買代金）
  turnover_surge: 直近N日平均売買代金比（1.0=平常、2.0=2倍の資金流入）
  breadth_ratio : 上昇銘柄比率（0.0〜1.0）
  heat_score    : 過熱スコア（正=強気・資金流入、負=弱気・資金流出）

【使い方】
  python theme_score.py              # 直近取引日を計算
  python theme_score.py 2026-06-26   # 指定日を計算
  python theme_score.py --all        # 全期間を再計算（初回・再構築用）
  python theme_score.py --reindex    # index_value/surge/heat のみ再計算
"""

import sys
from collections import defaultdict
from datetime import date
from config import get_conn, bulk_upsert

# ═══════════════════════════════════════════════════════════
#  チューニングパラメータ（変更しやすいよう先頭にまとめる）
# ═══════════════════════════════════════════════════════════

# テーマ指数の銘柄加重: みんかぶ関連度(60-100)をそのまま重みに使う
# （コア80点は関連60点の1.33倍の影響。テーマの主役の動きが指数に強く出る）

# 資金流入比率（turnover_surge）の基準ウィンドウ（営業日）
SURGE_WINDOW = 20

# 過熱スコアの週間リターン計算ウィンドウ（営業日）
HEAT_WINDOW = 5

# 過熱スコアの重み（合計 1.0）
# ・return : 週間リターン(%) ← 上昇幅の大きさ
# ・flow   : (surge-1) × 10 ← 資金流入の勢い（surge=1.5 で +5 相当）
# ・breadth: (breadth-0.5) × 20 ← 騰落の広がり（全銘柄上昇で +10 相当）
HEAT_WEIGHTS: dict[str, float] = {
    "return":  0.4,
    "flow":    0.4,
    "breadth": 0.2,
}


# ═══════════════════════════════════════════════════════════
#  ヘルパー
# ═══════════════════════════════════════════════════════════

def _get_theme_stocks(conn) -> dict[int, list[dict]]:
    """テーマ別の銘柄リスト {theme_id: [{code, weight}]} を返す。
    ソースは統一テーママスタ(theme_master.py＝みんかぶ)。集計は tier>=2(関連度60+)のみ。
    旧: theme_categories/stock_themes(自前21テーマ) — 2026-07にみんかぶへ統一。"""
    cur = conn.cursor()
    cur.execute("""
        SELECT tm.theme_id, tm.code, tm.relevance
        FROM theme_members tm
        JOIN themes t ON t.id = tm.theme_id
        JOIN stocks s ON s.code = tm.code
        WHERE t.status = 'active' AND tm.tier >= 2 AND s.is_active = TRUE
    """)
    result: dict[int, list[dict]] = defaultdict(list)
    for theme_id, code, relevance in cur.fetchall():
        result[theme_id].append({
            "code": code,
            "weight": float(relevance or 60),
        })
    cur.close()
    return dict(result)


def _compute_raw_day(conn, target_date: date, theme_stocks: dict) -> int:
    """
    指定日の生統計（avg_change_pct / total_turnover / net_turnover / breadth_ratio）を
    計算して UPSERT する。
    index_value / turnover_surge / heat_score は _update_derived で後処理。
    Returns: 更新したテーマ数
    """
    cur = conn.cursor()

    # 当日の全銘柄価格を一括取得
    # turnoverはJ-Quants無料プランでは未提供のため、volume×closeで代替
    cur.execute("""
        SELECT code, change_pct,
               COALESCE(turnover, volume * close) AS effective_turnover
        FROM daily_prices
        WHERE date = %s AND close IS NOT NULL
    """, (target_date,))
    price_map: dict[str, tuple] = {r[0]: r for r in cur.fetchall()}

    if not price_map:
        cur.close()
        return 0

    rows = []
    for theme_id, stocks in theme_stocks.items():
        hits = [(s, price_map[s["code"]]) for s in stocks if s["code"] in price_map]
        if not hits:
            continue

        total_weight = sum(s["weight"] for s, _ in hits)
        # 個別リターンは±25%にクリップ（ウィンズライズ）。新規上場初日や特殊イベントの
        # 数百%リターンが1銘柄でテーマ指数を吹き飛ばすのを防ぐ（指数の頑健化）
        avg_chg = sum(
            s["weight"] * max(-25.0, min(25.0, float(p[1] or 0))) for s, p in hits
        ) / total_weight

        total_t = sum(int(p[2] or 0) for _, p in hits)
        net_t = sum(
            int(p[2] or 0) * (
                1 if float(p[1] or 0) > 0 else
               -1 if float(p[1] or 0) < 0 else 0
            )
            for _, p in hits
        )
        breadth = sum(1 for _, p in hits if float(p[1] or 0) > 0) / len(hits)

        rows.append((
            target_date, theme_id,
            len(hits),
            round(avg_chg, 4),
            total_t, net_t,
            round(breadth, 4),
        ))

    if rows:
        # index_value / turnover_surge / heat_score は update_cols に含めない
        # → ON DUPLICATE KEY UPDATE でも既存の派生値を上書きしない
        bulk_upsert(
            cur, "theme_daily_stats",
            ["date", "theme_id", "stock_count", "avg_change_pct",
             "total_turnover", "net_turnover", "breadth_ratio"],
            rows,
            update_cols=["stock_count", "avg_change_pct",
                         "total_turnover", "net_turnover", "breadth_ratio"],
        )
        conn.commit()

    cur.close()
    return len(rows)


def _update_derived(conn, since: date | None = None) -> int:
    """
    index_value / turnover_surge / heat_score を再計算して upsert する。
    since=None : 全期間の完全再計算（初回・--all・--reindex用）
    since=日付 : その日以降の行だけ更新する増分モード（日次用）。
                 みんかぶ統一で1,100超テーマ×全期間=数十万行の毎日全更新は重いため、
                 過去70日分だけロードし、先頭行の保存済みindex_valueを起点(アンカー)に
                 前進計算する（70日 ≒ 営業日47日 > SURGE_WINDOW+HEAT_WINDOW で窓は充足）。
    """
    from datetime import timedelta
    cur = conn.cursor()
    if since is None:
        cur.execute("""
            SELECT date, theme_id, avg_change_pct, total_turnover, breadth_ratio, index_value
            FROM theme_daily_stats ORDER BY theme_id, date
        """)
    else:
        cur.execute("""
            SELECT date, theme_id, avg_change_pct, total_turnover, breadth_ratio, index_value
            FROM theme_daily_stats WHERE date >= %s ORDER BY theme_id, date
        """, (since - timedelta(days=70),))
    all_rows = cur.fetchall()

    theme_data: dict[int, list[dict]] = defaultdict(list)
    for dt, theme_id, avg_chg, total_t, breadth, idx_stored in all_rows:
        theme_data[int(theme_id)].append({
            "date": dt,
            "avg_change_pct": float(avg_chg or 0),
            "total_turnover": int(total_t or 0),
            "breadth_ratio":  float(breadth or 0.5),
            "index_stored":   float(idx_stored) if idx_stored is not None else None,
        })

    out_rows = []
    for theme_id, data in theme_data.items():
        turnovers = [d["total_turnover"] for d in data]

        # Pass 1: index_value と turnover_surge
        for i, d in enumerate(data):
            if i == 0:
                # 全再計算なら100起点。増分ならアンカー（保存済みindexを信頼して継続）
                d["index_value"] = 100.0 if since is None else (d["index_stored"] or 100.0)
            else:
                d["index_value"] = round(
                    data[i - 1]["index_value"] * (1 + d["avg_change_pct"] / 100), 4
                )
            window = turnovers[max(0, i - SURGE_WINDOW):i]
            avg_t = sum(window) / len(window) if window else turnovers[i]
            d["turnover_surge"] = round(turnovers[i] / avg_t if avg_t > 0 else 1.0, 4)

        # Pass 2: heat_score
        for i, d in enumerate(data):
            if since is not None and d["date"] < since:
                continue   # 増分モードでは対象日以降のみ書き込む
            ref_idx = data[max(0, i - HEAT_WINDOW)]["index_value"]
            ret_5d  = (d["index_value"] / ref_idx - 1) * 100 if ref_idx else 0
            heat = (
                HEAT_WEIGHTS["return"]  * ret_5d
              + HEAT_WEIGHTS["flow"]    * (d["turnover_surge"] - 1) * 10
              + HEAT_WEIGHTS["breadth"] * (d["breadth_ratio"]  - 0.5) * 20
            )
            # 列型のレンジにクリップ（index DECIMAL(12,4) / surge・heat DECIMAL(8,4)）。
            # 売買代金ほぼゼロのテーマのsurge爆発等で書込みエラーにならないよう防御
            out_rows.append([
                d["date"], theme_id,
                min(d["index_value"], 99999999.0),
                min(d["turnover_surge"], 9999.0),
                max(-9999.0, min(9999.0, round(heat, 4))),
            ])

    # (date, theme_id) ユニークキーで既存行の派生3列だけ更新（batched・数十万行でも実用速度）
    bulk_upsert(
        cur, "theme_daily_stats",
        ["date", "theme_id", "index_value", "turnover_surge", "heat_score"],
        out_rows,
        update_cols=["index_value", "turnover_surge", "heat_score"],
    )
    conn.commit()
    cur.close()
    return len(out_rows)


# ═══════════════════════════════════════════════════════════
#  公開 API
# ═══════════════════════════════════════════════════════════

def compute_day(target_date: date = None) -> None:
    """指定日（省略時: daily_prices の最新日）の統計を計算し DB に保存する"""
    conn = get_conn()

    if target_date is None:
        cur = conn.cursor()
        cur.execute("SELECT MAX(date) FROM daily_prices")
        target_date = cur.fetchone()[0]
        cur.close()

    print(f"  対象日: {target_date}")
    theme_stocks = _get_theme_stocks(conn)
    n_raw = _compute_raw_day(conn, target_date, theme_stocks)
    print(f"  生統計: {n_raw} テーマ更新")
    n_der = _update_derived(conn, since=target_date)   # 増分（対象日以降のみ）
    print(f"  派生値: {n_der} 行更新")
    conn.close()


def compute_all() -> None:
    """
    daily_prices の全日付を対象に再計算する。
    初回構築・テーマ銘柄の大幅変更後・パラメータ変更後に実行。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM daily_prices ORDER BY date")
    dates = [r[0] for r in cur.fetchall()]
    cur.close()

    print(f"  対象期間: {dates[0]} 〜 {dates[-1]}  ({len(dates)} 営業日)")
    theme_stocks = _get_theme_stocks(conn)

    total_raw = 0
    for i, d in enumerate(dates, 1):
        total_raw += _compute_raw_day(conn, d, theme_stocks)
        if i % 20 == 0 or i == len(dates):
            print(f"  [{i:3d}/{len(dates)}] {d} 処理完了")

    print(f"  生統計計: {total_raw} 行")
    print("  派生値を再計算中...")
    n_der = _update_derived(conn)
    print(f"  派生値: {n_der} 行更新完了")
    conn.close()


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]

    if not args:
        print("直近取引日を計算します...")
        compute_day()
        return

    if args[0] == "--all":
        print("全期間を再計算します（時間がかかります）...")
        compute_all()

    elif args[0] == "--reindex":
        print("派生値のみ再計算します...")
        conn = get_conn()
        n = _update_derived(conn)
        conn.close()
        print(f"  {n} 行更新完了")

    else:
        try:
            d = date.fromisoformat(args[0])
            compute_day(d)
        except ValueError:
            print(f"引数が不正です: {args[0]}")
            print("  使い方: python theme_score.py [YYYY-MM-DD | --all | --reindex]")
            sys.exit(1)


if __name__ == "__main__":
    main()
