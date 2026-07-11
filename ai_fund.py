"""
AIファンドマネージャー
======================
AIが銘柄を発掘・保有・売却する模擬運用ファンド。サイトの /aifund タブに表示する。

運用ルール（オーナー指定）:
  - 資金1000万円スタート。常に8銘柄を保有（市況を問わず）
  - 投資期間の目安は数日〜半年。キャピタルゲインの最大化が目的
  - 売買には必ず理由を付け、購入理由〜売却理由まで通しで記録する
  - **意思決定した当日の株価では売買しない**（先読み防止）。
    夜に意思決定 → 翌営業日の寄付（始値）で約定
  - 100株単位。銘柄ごとに予算の強弱をつけてよい
  - 取引コスト: 約定代金の0.1%/片道（手数料+スリッページの模擬）

アーキテクチャ（ハイブリッド）:
  1. 定量スクリーニングで候補を数十銘柄に絞る（モメンタム/押し目/ブレイク/
     業績イベント/割安成長 の5観点。price_stats・theoretical_values・
     forecast_revisions を利用）
  2. Gemini が現ポジションと候補を見て売り/買いを決定し、理由とシナリオ
     （どうなったら売るか）を日本語で書く
  3. コード側ガードレールが強制執行:
     常時8銘柄・予算60万〜250万/銘柄・1日の入替最大3銘柄（初回除く）・
     売却後5営業日は同一銘柄の再購入禁止・含み損-20%で強制ロスカット

日次フロー（daily_run.py に組込み）:
  - メイン便（夕方）: execute_orders() 当日寄付で約定 → record_nav() 終値で評価
  - イブニング便（20:30・開示回収後）: decide() 意思決定 → 翌日分の注文登録

実行例:
  python3 ai_fund.py --execute   # pending注文を当日寄付で約定
  python3 ai_fund.py --nav       # 当日NAVを記録
  python3 ai_fund.py --decide    # 意思決定（Gemini・注文登録）
  python3 ai_fund.py --status    # 現状表示
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from config import get_conn

load_dotenv()

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"

INITIAL_CASH   = 10_000_000
N_POSITIONS    = 8            # 常時保有する銘柄数
COST_RATE      = 0.001        # 片道0.1%（往復0.2%）
BUDGET_MIN     = 600_000      # 1銘柄の予算下限
BUDGET_MAX     = 2_500_000    # 1銘柄の予算上限
MAX_SWAPS      = 3            # 1日の入替上限（初回構築を除く）
REBUY_COOLDOWN = 7            # 売却後の再購入禁止（カレンダー日）
LOSSCUT_PCT    = -20.0        # 強制ロスカット閾値（含み損%）
BENCH_CODE     = "1306"       # ベンチマーク: TOPIX連動ETF
N_BENCH        = 8            # 控え（次点候補）銘柄数
MAX_ANTICIPATE = 2            # 「先回り（予測）」スタイルの保有上限（ポートフォリオの一部に留める）

# ファンドの運営方針（憲章）。固定ルールの一元記述。サイトの /aifund に常時表示する。
# 変更時はこの定数を更新する（コードのガードレールと必ず一致させること）。
CHARTER = f"""1. 目的: キャピタルゲインの最大化。投資期間の目安は数日〜半年（デイトレはしない）
2. 常時{N_POSITIONS}銘柄を保有し、市況を問わずフルポジションを維持する
3. 全ての買いに「カタリスト」を明文化する: 自分が買った後に、誰が・いつ・なぜ買い上げてくるのか
   （例: 決算がコンセンサスを上回る見込み／統計的エッジ／業界拡大に必須のパーツだが未注目）。
   カタリストは毎晩再検証し、崩れたら売却する
4. 意思決定した当日の価格では売買しない。夜に判断し、翌営業日の寄付で約定（先読みの排除）
5. 売買単位は100株。1銘柄の予算は{BUDGET_MIN//10000}万〜{BUDGET_MAX//10000}万円で確信度に応じて強弱
6. 取引コスト{COST_RATE*100:.1f}%/片道を控除。無駄な回転を抑えるため入替は1日最大{MAX_SWAPS}銘柄、
   売却後{REBUY_COOLDOWN}日間は同一銘柄を再購入しない
7. 含み損{LOSSCUT_PCT:.0f}%で機械的にロスカット（AIの判断より規律を優先）
8. 「先回り（予測）」スタイルは最大{MAX_ANTICIPATE}銘柄まで。予測であることを明示する
9. 保有に次ぐ控え{N_BENCH}銘柄を常に選定・計測し、機動的に昇格させる
10. 投資基準は毎晩、相場環境と自らの成績を検証して明文化・更新し、履歴を蓄積する
11. 決算発表日を必ず把握し、無自覚に決算を跨がない（跨ぐならそれ自体をカタリストとして明文化する）
12. 分散を守る: 同一業種・同一テーマは最大3銘柄。カタリストの種類（イベント/トレンド/割安見直し/先回り）も分散"""


# ─────────────────────────────────────────────────────────────────────────────
# テーブル
# ─────────────────────────────────────────────────────────────────────────────

def ensure_tables():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_state (
            id             TINYINT PRIMARY KEY,
            cash           DOUBLE NOT NULL,
            inception_date DATE,
            last_decided   DATE,
            updated_at     DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_positions (
            code       VARCHAR(10) PRIMARY KEY,
            shares     INT NOT NULL,
            avg_cost   DOUBLE NOT NULL COMMENT '取得単価（片道コスト込み）',
            buy_date   DATE COMMENT '約定日',
            buy_reason TEXT,
            thesis     TEXT COMMENT '想定シナリオ・売却条件',
            created_at DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_orders (
            id           BIGINT AUTO_INCREMENT PRIMARY KEY,
            code         VARCHAR(10) NOT NULL,
            side         VARCHAR(4)  NOT NULL COMMENT 'buy/sell',
            budget       DOUBLE COMMENT 'buy: 予算円（株数は約定時の寄付値で決定）',
            shares       INT    COMMENT 'sell: 株数',
            reason       TEXT,
            thesis       TEXT,
            decided_date DATE NOT NULL COMMENT '意思決定日（この日の価格は使わない）',
            status       VARCHAR(10) DEFAULT 'pending' COMMENT 'pending/filled/expired',
            note         VARCHAR(200),
            created_at   DATETIME,
            INDEX idx_status (status)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_trades (
            id           BIGINT AUTO_INCREMENT PRIMARY KEY,
            code         VARCHAR(10) NOT NULL,
            side         VARCHAR(4)  NOT NULL,
            shares       INT NOT NULL,
            price        DOUBLE NOT NULL COMMENT '約定値（当日寄付）',
            fee          DOUBLE NOT NULL,
            trade_date   DATE NOT NULL,
            decided_date DATE,
            reason       TEXT,
            buy_reason   TEXT COMMENT 'sell時: 対応する購入理由（通しで読めるように）',
            pnl          DOUBLE COMMENT 'sell時: 実現損益（コスト込み）',
            pnl_pct      DOUBLE,
            hold_days    INT,
            created_at   DATETIME,
            INDEX idx_code (code), INDEX idx_date (trade_date)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_nav (
            date       DATE PRIMARY KEY,
            nav        DOUBLE NOT NULL COMMENT '現金+時価評価',
            cash       DOUBLE NOT NULL,
            n_pos      INT,
            bench      DOUBLE COMMENT 'ベンチマーク(1306 adj_close)',
            market_view TEXT COMMENT 'AIの市況見解（decide時に更新）',
            created_at DATETIME
        )
    """)
    # 投資基準（毎晩AIが相場環境・成績を踏まえて更新。日次で蓄積し最新をサイト表示）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_policy (
            policy_date DATE PRIMARY KEY,
            statement   TEXT COMMENT 'その日時点の投資基準（明文）',
            created_at  DATETIME
        )
    """)
    # 控え（ベンチ）銘柄: 保有8に次ぐ候補8。日次で入替・蓄積
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_bench (
            bench_date DATE NOT NULL,
            rank_no    TINYINT NOT NULL,
            code       VARCHAR(10) NOT NULL,
            style      VARCHAR(10) DEFAULT '通常' COMMENT '通常/先回り',
            reason     TEXT,
            close_at   DOUBLE COMMENT '選定日の終値（以後のパフォーマンス計測用）',
            created_at DATETIME,
            PRIMARY KEY (bench_date, rank_no)
        )
    """)
    # 観点タグ別の実測エッジ（週次スナップショットで20営業日後リターンを実測。週1再計算）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_fund_edge (
            view_tag     VARCHAR(20) PRIMARY KEY,
            horizon_days INT,
            n            INT,
            avg_ret      DOUBLE,
            med_ret      DOUBLE,
            win_rate     DOUBLE,
            computed_at  DATETIME
        )
    """)
    # 決算発表予定日（kabutanのfinanceページから取得・キャッシュ。決算跨ぎのリスク管理用）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS earnings_schedule (
            code          VARCHAR(10) PRIMARY KEY,
            announce_date DATE,
            fetched_at    DATETIME
        )
    """)
    # 既存テーブルへのカラム追加（初回マイグレーション）:
    #   style    = 投資スタイル（通常/先回り）
    #   catalyst = カタリスト（誰が・いつ・なぜ後から買ってくるかの明文化。買いの必須項目）
    for tbl in ("ai_fund_orders", "ai_fund_positions", "ai_fund_trades"):
        for coldef in ("style VARCHAR(10) DEFAULT '通常'", "catalyst TEXT"):
            try:
                cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {coldef}")
            except Exception:
                pass
    conn.commit(); cur.close(); conn.close()


def _get_state(cur) -> dict | None:
    cur.execute("SELECT cash, inception_date, last_decided FROM ai_fund_state WHERE id = 1")
    r = cur.fetchone()
    return {"cash": float(r[0]), "inception": r[1], "last_decided": r[2]} if r else None


def _init_state(cur):
    cur.execute("""
        INSERT IGNORE INTO ai_fund_state (id, cash, inception_date, updated_at)
        VALUES (1, %s, CURDATE(), NOW())
    """, (INITIAL_CASH,))


def _latest_trading_date(cur) -> date | None:
    cur.execute("SELECT MAX(date) FROM daily_prices")
    return cur.fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# 約定処理（当日の寄付＝始値で pending 注文を執行）
# ─────────────────────────────────────────────────────────────────────────────

def execute_orders() -> int:
    """pending注文を「最新取引日の始値」で約定する。
    決定日より後の取引日にのみ約定させる（先読み防止の要）。
    始値が無い銘柄（売買停止等）は pending のまま持ち越し。"""
    ensure_tables()
    conn = get_conn(); cur = conn.cursor()
    today = _latest_trading_date(cur)
    if today is None:
        cur.close(); conn.close(); return 0
    state = _get_state(cur)
    if state is None:
        cur.close(); conn.close(); return 0

    cur.execute("""
        SELECT id, code, side, budget, shares, reason, thesis, decided_date, style, catalyst
        FROM ai_fund_orders WHERE status = 'pending' ORDER BY side DESC, id
    """)  # side DESC → sell を先に処理して現金を作ってから buy
    orders = cur.fetchall()
    filled = 0

    for oid, code, side, budget, shares, reason, thesis, decided, style, catalyst in orders:
        if decided >= today:
            continue  # 決定日当日の価格では絶対に約定させない
        cur.execute("SELECT open, close FROM daily_prices WHERE code = %s AND date = %s", (code, today))
        row = cur.fetchone()
        if not row or not row[0] or float(row[0]) <= 0:
            continue  # 当日値なし → 持ち越し
        open_px = float(row[0])

        if side == "sell":
            cur.execute("SELECT shares, avg_cost, buy_date, buy_reason, style FROM ai_fund_positions WHERE code = %s", (code,))
            pos = cur.fetchone()
            if not pos:
                cur.execute("UPDATE ai_fund_orders SET status='expired', note='ポジションなし' WHERE id=%s", (oid,))
                continue
            p_shares, avg_cost, buy_date, buy_reason, p_style = int(pos[0]), float(pos[1]), pos[2], pos[3], pos[4]
            proceeds = p_shares * open_px
            fee = proceeds * COST_RATE
            pnl = proceeds - fee - p_shares * avg_cost
            pnl_pct = (open_px * (1 - COST_RATE) / avg_cost - 1) * 100
            hold_days = (today - buy_date).days if buy_date else None
            cur.execute("""
                INSERT INTO ai_fund_trades (code, side, shares, price, fee, trade_date, decided_date,
                                            reason, buy_reason, pnl, pnl_pct, hold_days, style, created_at)
                VALUES (%s,'sell',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            """, (code, p_shares, open_px, round(fee), today, decided, reason, buy_reason,
                  round(pnl), round(pnl_pct, 2), hold_days, p_style or "通常"))
            cur.execute("DELETE FROM ai_fund_positions WHERE code = %s", (code,))
            state["cash"] += proceeds - fee
            cur.execute("UPDATE ai_fund_orders SET status='filled' WHERE id=%s", (oid,))
            filled += 1
            print(f"  [約定] 売 {code} {p_shares}株 @{open_px:,.0f} 損益{pnl:+,.0f}円 ({pnl_pct:+.1f}%)")

        else:  # buy
            budget = float(budget or 0)
            n = int(budget // (open_px * 100)) * 100
            max_afford = int((state["cash"] / (1 + COST_RATE)) // (open_px * 100)) * 100
            n = min(n, max_afford)
            if n <= 0:
                cur.execute("UPDATE ai_fund_orders SET status='expired', note='予算/現金内で100株単位が買えず' WHERE id=%s", (oid,))
                print(f"  [失効] 買 {code}: 寄付{open_px:,.0f}円が予算内で買えず")
                continue
            amount = n * open_px
            fee = amount * COST_RATE
            avg_cost = (amount + fee) / n
            cur.execute("""
                INSERT INTO ai_fund_positions (code, shares, avg_cost, buy_date, buy_reason, thesis, style, catalyst, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON DUPLICATE KEY UPDATE
                    avg_cost = (avg_cost*shares + VALUES(avg_cost)*VALUES(shares)) / (shares+VALUES(shares)),
                    shares = shares + VALUES(shares)
            """, (code, n, round(avg_cost, 2), today, reason, thesis, style or "通常", catalyst))
            cur.execute("""
                INSERT INTO ai_fund_trades (code, side, shares, price, fee, trade_date, decided_date, reason, style, catalyst, created_at)
                VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            """, (code, n, open_px, round(fee), today, decided, reason, style or "通常", catalyst))
            state["cash"] -= amount + fee
            cur.execute("UPDATE ai_fund_orders SET status='filled' WHERE id=%s", (oid,))
            filled += 1
            print(f"  [約定] 買 {code} {n}株 @{open_px:,.0f} ({amount/1e4:,.0f}万円)")

    cur.execute("UPDATE ai_fund_state SET cash=%s, updated_at=NOW() WHERE id=1", (state["cash"],))
    conn.commit(); cur.close(); conn.close()
    if filled:
        print(f"  [AIファンド] {filled}件約定 / 現金残 {state['cash']/1e4:,.0f}万円")
    return filled


# ─────────────────────────────────────────────────────────────────────────────
# NAV記録（終値で時価評価）
# ─────────────────────────────────────────────────────────────────────────────

def record_nav() -> float | None:
    ensure_tables()
    conn = get_conn(); cur = conn.cursor()
    today = _latest_trading_date(cur)
    state = _get_state(cur)
    if today is None or state is None:
        cur.close(); conn.close(); return None
    cur.execute("SELECT code, shares FROM ai_fund_positions")
    pos = cur.fetchall()
    mv = 0.0
    for code, shares in pos:
        cur.execute("SELECT close FROM daily_prices WHERE code=%s AND date=%s", (code, today))
        r = cur.fetchone()
        if r and r[0]:
            mv += int(shares) * float(r[0])
        else:  # 当日値なし→直近値
            cur.execute("SELECT close FROM daily_prices WHERE code=%s AND date<=%s ORDER BY date DESC LIMIT 1", (code, today))
            r2 = cur.fetchone()
            if r2 and r2[0]:
                mv += int(shares) * float(r2[0])
    nav = state["cash"] + mv
    cur.execute("SELECT COALESCE(adj_close, close) FROM daily_prices WHERE code=%s AND date=%s", (BENCH_CODE, today))
    b = cur.fetchone()
    bench = float(b[0]) if b and b[0] else None
    cur.execute("""
        INSERT INTO ai_fund_nav (date, nav, cash, n_pos, bench, created_at)
        VALUES (%s,%s,%s,%s,%s,NOW())
        ON DUPLICATE KEY UPDATE nav=VALUES(nav), cash=VALUES(cash), n_pos=VALUES(n_pos), bench=VALUES(bench)
    """, (today, round(nav), round(state["cash"]), len(pos), bench))
    conn.commit(); cur.close(); conn.close()
    print(f"  [AIファンド] NAV {nav/1e4:,.0f}万円（現金{state['cash']/1e4:,.0f}万・{len(pos)}銘柄）")
    return nav


# ─────────────────────────────────────────────────────────────────────────────
# 候補の定量スクリーニング（5観点）
# ─────────────────────────────────────────────────────────────────────────────

_CAND_FROM = """
    FROM price_stats p
    JOIN stocks s ON s.code = p.code AND s.is_active = 1
    LEFT JOIN sectors sec ON sec.id = s.sector_id
    LEFT JOIN stock_fundamentals f ON f.code = p.code
    LEFT JOIN theoretical_values t ON t.code = p.code
    WHERE p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
      AND f.market_cap >= 15e9
"""
_CAND_SEL = """
    SELECT p.code, s.name, p.close, p.chg5d, p.chg25d, p.chg75d, p.rsi14,
           p.dev_ma25, p.dev_high52w, p.vol20_ratio, p.turnover_20d,
           p.ma200_slope, p.break_65d, f.per, f.roe, f.market_cap,
           t.theo_ratio, t.upside_3y_pct, p.rev_growth, p.op_growth, sec.name
"""
_CAND_COLS = ["code", "name", "close", "chg5d", "chg25d", "chg75d", "rsi14", "dev_ma25",
              "dev_high52w", "vol20_ratio", "turnover_20d", "ma200_slope", "break_65d",
              "per", "roe", "market_cap", "theo_ratio", "upside_3y_pct", "rev_growth", "op_growth",
              "sector"]


def _cconv(x):
    from decimal import Decimal
    return float(x) if isinstance(x, Decimal) else x


def _candidates_one(cur, code: str) -> dict | None:
    """1銘柄分の候補メトリクスを取得（控え銘柄の合流用）。流動性等の足切りも適用される。"""
    cur.execute(_CAND_SEL + _CAND_FROM.replace("WHERE", "WHERE p.code = %s AND", 1), (code,))
    r = cur.fetchone()
    if not r:
        return None
    d = dict(zip(_CAND_COLS, [_cconv(x) for x in r]))
    d["event"] = None
    return d


def _candidates(cur, exclude: set[str]) -> list[dict]:
    """観点別に候補を集めて重複排除。exclude（保有中・再購入クールダウン中）は除外。"""
    base_from = _CAND_FROM
    sel = _CAND_SEL
    views = [
        ("momentum", sel + base_from + """
            AND p.chg25d >= 10 AND p.rsi14 < 78 AND p.close > p.ma25 AND p.chg5d > -4
            ORDER BY p.chg25d DESC LIMIT 8"""),
        ("dip", sel + base_from + """
            AND p.ma200_slope > 0 AND p.close > p.ma200 AND p.rsi14 < 42 AND p.chg25d > -12
            ORDER BY p.rsi14 ASC LIMIT 6"""),
        ("breakout", sel + base_from + """
            AND p.break_65d = 1 AND p.vol20_ratio >= 1.4 AND p.rsi14 < 80
            ORDER BY p.vol20_ratio DESC LIMIT 6"""),
        ("value_growth", sel + base_from + """
            AND t.theo_ratio >= 1.25 AND p.op_growth > 5 AND p.chg25d > -5 AND p.rsi14 < 70
            ORDER BY t.upside_3y_pct DESC LIMIT 6"""),
    ]
    _conv = _cconv
    cands: dict[str, dict] = {}
    cols = _CAND_COLS
    for tag, q in views:
        cur.execute(q)
        for r in cur.fetchall():
            d = dict(zip(cols, [_conv(x) for x in r]))
            code = d["code"]
            if code in exclude:
                continue
            if code in cands:
                if tag not in cands[code]["tags"]:
                    cands[code]["tags"].append(tag)
            else:
                d["tags"] = [tag]
                d["event"] = None
                cands[code] = d

    # 観点5: 直近の上方修正・増配イベント（発表済み・reaction_date以降のみ＝先読みなし）
    cur.execute("""
        SELECT r.code, s.name, r.direction, r.op_chg_pct, r.dps_old, r.dps_new, r.announced_at
        FROM forecast_revisions r
        JOIN stocks s ON s.code = r.code AND s.is_active = 1
        JOIN price_stats p ON p.code = r.code
        LEFT JOIN stock_fundamentals f ON f.code = r.code
        WHERE r.announced_at >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
          AND r.reaction_date <= (SELECT MAX(date) FROM daily_prices)
          AND (r.direction = 'up' OR (r.dps_new > r.dps_old))
          AND p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
          AND COALESCE(f.market_cap, 0) >= 15e9
        ORDER BY r.announced_at DESC LIMIT 8
    """)
    ev_rows = cur.fetchall()
    for code, name, direction, op_chg, dps_old, dps_new, ann in ev_rows:
        if code in exclude:
            continue
        label = []
        if direction == "up":
            label.append(f"上方修正(営業益{f'{float(op_chg):+.0f}%' if op_chg is not None else ''})")
        if dps_new and dps_old and float(dps_new) > float(dps_old):
            label.append(f"増配{float(dps_old):.0f}→{float(dps_new):.0f}円")
        ev = f"{str(ann)[:10]} {'・'.join(label)}"
        if code in cands:
            if "event" not in cands[code]["tags"]:
                cands[code]["tags"].append("event")
            cands[code]["event"] = ev
        else:
            cur.execute(sel + base_from.replace("WHERE", "WHERE p.code = %s AND", 1), (code,))
            r = cur.fetchone()
            if r:
                d = dict(zip(cols, [_conv(x) for x in r]))
                d["tags"] = ["event"]; d["event"] = ev
                cands[code] = d

    # 観点6: 資金流入テーマ内の「出遅れ」（先回り投資の材料）。
    # 「テーマ内のAが買われた→出遅れているBに資金が波及する」という一歩先の予測用。
    # テーマは規模足切り（売買代金100億+・8銘柄+）でノイズを除外（/flowsと同じ基準）
    cur.execute("""
        SELECT group_key, zscore FROM money_flow_weekly
        WHERE group_type = 'theme' AND flow_class = 'inflow'
          AND week_end = (SELECT MAX(week_end) FROM money_flow_weekly)
          AND turnover >= 100 AND n_stocks >= 8
        ORDER BY zscore DESC LIMIT 5
    """)
    for theme, z in cur.fetchall():
        cur.execute(
            sel + base_from.replace(
                "WHERE", "WHERE p.code IN (SELECT code FROM kabutan_themes WHERE theme = %s) AND", 1) + """
            AND p.chg25d < 12 AND p.chg5d > -5 AND p.ma200_slope >= 0 AND p.rsi14 < 65
            ORDER BY p.turnover_20d DESC LIMIT 3
        """, (theme,))
        for r in cur.fetchall():
            d = dict(zip(cols, [_conv(x) for x in r]))
            code = d["code"]
            if code in exclude:
                continue
            note = f"資金流入テーマ「{theme}」(Z={float(z):.1f})内でまだ上がっていない出遅れ"
            if code in cands:
                if "laggard" not in cands[code]["tags"]:
                    cands[code]["tags"].append("laggard")
                cands[code]["event"] = cands[code].get("event") or note
            else:
                d["tags"] = ["laggard"]; d["event"] = note
                cands[code] = d
    return list(cands.values())[:36]


# 観点タグ→過去スナップショットでの再現条件（price_stats_historyの列で表現できるもののみ。
# 「AIの語るエッジ」を実測値で裏付ける／反証するための検証基盤）
EDGE_VIEWS = {
    "momentum": "chg25d >= 10 AND rsi14 < 78 AND close > ma25 AND chg5d > -4",
    "dip":      "ma200_slope > 0 AND close > ma200 AND rsi14 < 42 AND chg25d > -12",
    "breakout": "break_65d = 1 AND vol20_ratio >= 1.4 AND rsi14 < 80",
    "baseline": "1 = 1",  # 全銘柄平均（比較基準）
}
EDGE_HORIZON = 20  # 営業日


def _refresh_edge_stats(cur, conn, force: bool = False) -> None:
    """観点タグ別の実測エッジを週次スナップショット×20営業日後リターンで計測し
    ai_fund_edge に保存する（週1回再計算）。流動性・価格帯は候補生成と同じ足切り。"""
    cur.execute("SELECT MAX(computed_at) FROM ai_fund_edge")
    r = cur.fetchone()
    if not force and r and r[0] and (datetime.now() - r[0]).days < 7:
        return

    print("  [AIファンド] 観点タグ別エッジを再計測中（週次）...")
    cur.execute("SELECT DISTINCT date FROM daily_prices WHERE date >= '2024-10-01' ORDER BY date")
    tdays = [row[0] for row in cur.fetchall()]
    tidx = {d: i for i, d in enumerate(tdays)}
    cur.execute("SELECT DISTINCT snapshot_date FROM price_stats_history ORDER BY snapshot_date")
    snaps = [row[0] for row in cur.fetchall()]

    for tag, cond in EDGE_VIEWS.items():
        rets: list[float] = []
        for d0 in snaps:
            i = tidx.get(d0)
            if i is None or i + EDGE_HORIZON >= len(tdays):
                continue
            d2 = tdays[i + EDGE_HORIZON]
            cur.execute(f"""
                SELECT h.code, h.close FROM price_stats_history h
                WHERE h.snapshot_date = %s AND h.turnover_20d >= 3
                  AND h.close BETWEEN 300 AND 24000 AND {cond}
            """, (d0,))
            rows = cur.fetchall()
            if not rows:
                continue
            codes = [row[0] for row in rows]
            px0 = {row[0]: float(row[1]) for row in rows}
            fmt = ",".join(["%s"] * len(codes))
            cur.execute(f"""
                SELECT code, COALESCE(adj_close, close) FROM daily_prices
                WHERE date = %s AND code IN ({fmt})
            """, [d2] + codes)
            for code, px2 in cur.fetchall():
                if px2 and px0.get(code, 0) > 0:
                    rets.append((float(px2) / px0[code] - 1) * 100)
        if not rets:
            continue
        rets.sort()
        n = len(rets)
        cur.execute("""
            INSERT INTO ai_fund_edge (view_tag, horizon_days, n, avg_ret, med_ret, win_rate, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
            ON DUPLICATE KEY UPDATE horizon_days=VALUES(horizon_days), n=VALUES(n),
                avg_ret=VALUES(avg_ret), med_ret=VALUES(med_ret),
                win_rate=VALUES(win_rate), computed_at=NOW()
        """, (tag, EDGE_HORIZON, n, round(sum(rets) / n, 2), round(rets[n // 2], 2),
              round(sum(1 for x in rets if x > 0) / n * 100, 1)))
        print(f"    {tag}: 平均{sum(rets)/n:+.2f}% 中央値{rets[n//2]:+.2f}% 勝率{sum(1 for x in rets if x>0)/n*100:.0f}% (n={n})")
    conn.commit()


def _edge_summary(cur) -> str:
    """プロンプト注入用のエッジ実測サマリー。"""
    cur.execute("SELECT view_tag, horizon_days, n, avg_ret, med_ret, win_rate FROM ai_fund_edge")
    rows = cur.fetchall()
    if not rows:
        return ""
    parts = []
    for tag, hz, n, avg, med, wr in sorted(rows, key=lambda r: r[0] != "baseline"):
        label = "全銘柄平均" if tag == "baseline" else tag
        parts.append(f"{label}: 平均{float(avg):+.1f}%/中央値{float(med):+.1f}%/勝率{float(wr):.0f}%(n={n})")
    return f"当サイトの週次スナップショットで実測した{rows[0][1]}営業日後リターン（過去約1年半） — " + " ／ ".join(parts)


def _progress_note(cur, code: str) -> str:
    """通期会社予想に対する営業益の進捗率（コンセンサス不在の代替指標）。
    例: ' 進捗率:営業益54%(Q2終了・単純按分50%)' — 按分超なら上振れ気配。"""
    try:
        cur.execute("""
            SELECT period_end, operating_income FROM financials
            WHERE code = %s AND period_type = 'A' AND period_end > CURDATE()
            ORDER BY period_end LIMIT 1
        """, (code,))
        fc = cur.fetchone()
        if not fc or not fc[1] or float(fc[1]) <= 0:
            return ""
        fy_end, op_fc = fc[0], float(fc[1])
        cur.execute("""
            SELECT COUNT(*), SUM(operating_income) FROM financials
            WHERE code = %s AND period_type = 'Q'
              AND period_end > DATE_SUB(%s, INTERVAL 1 YEAR) AND period_end <= CURDATE()
              AND operating_income IS NOT NULL
        """, (code, fy_end))
        n_q, op_sum = cur.fetchone()
        if not n_q or n_q == 0 or n_q >= 4 or op_sum is None:
            return ""
        progress = float(op_sum) / op_fc * 100
        return f" 進捗率:営業益{progress:.0f}%(Q{n_q}終了・単純按分{n_q*25}%)"
    except Exception:
        return ""


def _earnings_dates(cur, conn, codes: list[str]) -> dict:
    """各銘柄の次回決算発表予定日を返す。kabutanのfinanceページから取得し
    earnings_schedule にキャッシュ（3日で再取得・過ぎた日付も再取得）。
    決算跨ぎはイベントリスク/カタリストの両面で判断材料になるため、意思決定に必須。"""
    if not codes:
        return {}
    fmt = ",".join(["%s"] * len(codes))
    cur.execute(f"""
        SELECT code, announce_date FROM earnings_schedule
        WHERE code IN ({fmt}) AND fetched_at >= NOW() - INTERVAL 3 DAY
          AND (announce_date IS NULL OR announce_date >= CURDATE())
    """, codes)
    result = {r[0]: r[1] for r in cur.fetchall()}

    missing = [c for c in codes if c not in result]
    if missing:
        from kabutan_client import get as kabutan_get
        for code in missing[:50]:  # 1回の実行での取得上限（夜間バッチの時間保護）
            try:
                time.sleep(0.4)
                status, text = kabutan_get(f"stock/finance?code={code}")
                m = re.search(r'決算発表予定日.{0,120}?datetime="(\d{4}-\d{2}-\d{2})', text, re.S) if status == 200 else None
                ann = m.group(1) if m else None
                cur.execute("""
                    INSERT INTO earnings_schedule (code, announce_date, fetched_at)
                    VALUES (%s, %s, NOW())
                    ON DUPLICATE KEY UPDATE announce_date = VALUES(announce_date), fetched_at = NOW()
                """, (code, ann))
                if ann:
                    result[code] = datetime.strptime(ann, "%Y-%m-%d").date()
            except Exception:
                continue
        conn.commit()
    return result


def _earn_note(earn_map: dict, code: str) -> str:
    """プロンプト用の決算予定表記（45日以内のみ）。例: ' 決算発表:07/15(3日後)'"""
    d = earn_map.get(code)
    if not d:
        return ""
    days = (d - date.today()).days
    if days < 0 or days > 45:
        return ""
    return f" 決算発表:{d.month:02d}/{d.day:02d}({days}日後)"


def _cooldown_codes(cur) -> set[str]:
    cur.execute("""
        SELECT DISTINCT code FROM ai_fund_trades
        WHERE side='sell' AND trade_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
    """, (REBUY_COOLDOWN,))
    return {r[0] for r in cur.fetchall()}


# ─────────────────────────────────────────────────────────────────────────────
# 意思決定（Gemini + ガードレール）
# ─────────────────────────────────────────────────────────────────────────────

def _fnum(v, nd=1):
    return "-" if v is None else f"{float(v):.{nd}f}"


def _build_prompt(state, positions, cands, market_ctx, n_slots, est_cash,
                  policy_prev, feedback, inflow_themes, movers, earn_map=None,
                  edge_line="", prog_map=None) -> str:
    earn_map = earn_map or {}
    prog_map = prog_map or {}
    pos_lines = []
    for p in positions:
        style_tag = f"[{p.get('style') or '通常'}] " if p.get("style") == "先回り" else ""
        pos_lines.append(
            f"- {style_tag}{p['code']} {p['name']}: 取得{p['avg_cost']:,.0f}円({str(p['buy_date'])}) 現在{p['close']:,.0f}円 "
            f"損益{p['pnl_pct']:+.1f}% 保有{p['hold_days']}日 RSI{_fnum(p['rsi14'],0)} 5日{_fnum(p['chg5d'])}% 25日{_fnum(p['chg25d'])}%"
            f"{_earn_note(earn_map, p['code'])}\n"
            f"  購入理由: {p['buy_reason']}\n  カタリスト: {p.get('catalyst') or '（未記録）'}\n  シナリオ: {p['thesis']}"
        )
    cand_lines = []
    for c in cands:
        extra = f" 補足:{c['event']}" if c.get("event") else ""
        sec = f" 業種:{c['sector']}" if c.get("sector") else ""
        cand_lines.append(
            f"- {c['code']} {c['name']} [{'/'.join(c['tags'])}]{sec} 株価{c['close']:,.0f}円 "
            f"5日{_fnum(c['chg5d'])}% 25日{_fnum(c['chg25d'])}% 75日{_fnum(c['chg75d'])}% RSI{_fnum(c['rsi14'],0)} "
            f"52週高値比{_fnum(c['dev_high52w'])}% 出来高比{_fnum(c['vol20_ratio'])}x 売買代金{_fnum(c['turnover_20d'],0)}億 "
            f"PER{_fnum(c['per'])} ROE{_fnum(c['roe'])}% 理論株価比{_fnum(c['theo_ratio'],2)} 営業益成長{_fnum(c['op_growth'],0)}%"
            f"{_earn_note(earn_map, c['code'])}{prog_map.get(c['code'], '')}{extra}"
        )
    return f"""あなたは日本株のファンドマネージャーです。模擬ファンドを運用しています。

# 運用ルール（厳守）
- 目的: キャピタルゲインの最大化。投資期間の目安は数日〜半年
- 常に{N_POSITIONS}銘柄を保有する。今回は売りと買いをセットで考え、決定後の保有数が{N_POSITIONS}になるようにする
- 今回の買い枠: {n_slots}銘柄（売却を指示すればその分増える。1日の入替は最大{MAX_SWAPS}銘柄まで）
- 予算: 1銘柄 {BUDGET_MIN//10000}万〜{BUDGET_MAX//10000}万円。買い予算の合計は約{est_cash/10000:,.0f}万円以内
- 約定は明日の寄付（成行）。今日の終値からは乖離しうる
- 売買理由は具体的に（何を根拠に・何を期待して・どうなったら降りるか）。理由の水増しや創作は禁止
- **全ての買いに catalyst（カタリスト）を必ず書く**: 自分が買った後に「誰が・いつ・なぜ買い上げてくるのか」。
  例: 「直近の上方修正で機関投資家の見直し買いが入る局面」「65日高値ブレイク銘柄はトレンド追随の買いを
  呼びやすい」「テーマXに資金流入中だが本銘柄はそのXに不可欠な部材でまだ物色が及んでいない」。
  提供データから言えることだけを書き、無いイベントを創作しない。カタリストが書けない銘柄は買わない
- 各買いに style を付ける: "通常" または "先回り"（=まだ上がっていないが、資金波及・技術トレンドの読みで
  次に買われると**予測**する銘柄。laggardタグ等）。先回りは予測であることを理由に明記し、保有は最大{MAX_ANTICIPATE}銘柄まで
- **決算発表日（表記がある銘柄）を必ず考慮する**: 決算を跨ぐなら「決算が上振れするとみて跨ぐ（＝それがカタリスト）」か
  「決算前に手仕舞う/買わない」かを理由・シナリオに明記。無自覚に決算を跨ぐことを禁止する
- 分散: 同一業種・同一テーマの保有は最大3銘柄まで。カタリストの種類（イベント/トレンド/割安見直し/先回り）も分散させる

# 前回までの投資基準（あなた自身が書いたもの。継続性を保ちつつ、環境変化の根拠があれば更新する）
{policy_prev or '（初回のためまだ無い。今日の環境から初版を書くこと）'}

# 直近の成績フィードバック（何が効いて何が外れたか。基準の更新材料にする）
{feedback or '（まだ売買実績なし）'}

# 今週の相場環境
{market_ctx}
- 資金流入テーマ: {inflow_themes or '—'}
- 直近1週間の上昇上位: {movers or '—'}

# 観点タグ別の実測エッジ（重要: カタリストや投資基準で統計を語るときは、この実測値を引用する。
# 実測が「全銘柄平均」を上回らない観点を過信しない。数値の創作は厳禁）
{edge_line or '（計測データなし）'}
※候補行の「進捗率」= 通期会社予想の営業益に対する四半期累計の消化率。単純按分を大きく超えていれば上方修正の素地

# 現在のポートフォリオ（現金 {state['cash']/10000:,.0f}万円）
{chr(10).join(pos_lines) if pos_lines else '（なし・初回構築）'}

# 買い候補（定量スクリーニング済み。この中からのみ選ぶこと）
[タグ] momentum=上昇モメンタム / dip=上昇トレンド中の押し目 / breakout=65日高値ブレイク / value_growth=割安×成長 / event=上方修正・増配 / laggard=資金流入テーマ内の出遅れ(先回り向き) / bench=昨日までの控え銘柄
{chr(10).join(cand_lines)}

# 指示
1. 保有銘柄それぞれについて**カタリストとシナリオが生きているか**を点検し、崩れたもの・実現して出尽くしたもの・
   より良い候補に劣後するものを売る（無理に売る必要はない。売却理由にはカタリストの検証結果を書く）
2. 買いは候補から選ぶ。分散（同一業種・同一テーマに偏らない）と、観点の組み合わせを意識する。確信度に応じて予算に強弱
3. 保有8銘柄に次ぐ「控え」を{N_BENCH}銘柄選ぶ（保有・買い予定と重複しないこと。次に昇格させたい順）
4. 投資基準(policy)を更新する: 今の相場で「何が効いているか」を踏まえ、銘柄選定・利確/損切り・保有期間の方針を
   箇条書き4〜7行で明文化。前回から変えた点があれば末尾に「【更新】…」として1行で理由を書く

以下のJSONのみを出力:
{{"market_view": "市況の見立て(2文以内)",
 "policy": "今日時点の投資基準（箇条書き。改行は\\n）",
 "sells": [{{"code": "XXXX", "reason": "売却理由(カタリスト・シナリオとの照合を含め具体的に)"}}],
 "buys": [{{"code": "XXXX", "budget": 1200000, "style": "通常", "reason": "購入理由",
           "catalyst": "誰が・いつ・なぜ買い上げてくるか（必須）", "thesis": "想定シナリオと売却条件"}}],
 "bench": [{{"code": "XXXX", "style": "通常", "reason": "控えに置く理由(1文)"}}]}}"""


def _call_gemini(prompt: str) -> dict | None:
    from google import genai
    client = genai.Client(api_key=GEMINI_KEY)
    # JSONモード（構造化出力）で呼ぶ。理由文に引用符等が入ってもJSONが壊れない
    try:
        from google.genai import types
        config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4)
    except Exception:
        config = None
    for attempt in range(3):
        try:
            kwargs = {"model": GEMINI_MODEL, "contents": prompt}
            if config is not None:
                kwargs["config"] = config
            resp = client.models.generate_content(**kwargs)
            raw = (resp.text or "").strip()
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError as e:
                    print(f"  [AIファンド] JSONパース失敗({attempt+1}回目): {str(e)[:80]} → リトライ")
                    continue
        except Exception as e:
            if ("429" in str(e) or "503" in str(e)) and attempt < 2:
                print("  [AIファンド] レート制限 → 65秒待機してリトライ")
                time.sleep(65)
            else:
                print(f"  [AIファンド] Geminiエラー: {str(e)[:120]}")
                break
    return None


def decide() -> int:
    """夜間の意思決定。売り/買いを決めて ai_fund_orders に登録する（執行は翌営業日の寄付）。
    戻り値: 登録した注文数。"""
    ensure_tables()
    if not GEMINI_KEY:
        print("  [AIファンド] GEMINI_API_KEY未設定のためスキップ")
        return 0
    conn = get_conn(); cur = conn.cursor()
    _init_state(cur); conn.commit()
    state = _get_state(cur)
    today = date.today()

    if state["last_decided"] == today:
        print("  [AIファンド] 本日の意思決定は完了済み")
        cur.close(); conn.close(); return 0
    # 未約定注文が残っている間は新たな決定をしない（二重注文防止・休場明けに自然と解消）
    cur.execute("SELECT COUNT(*) FROM ai_fund_orders WHERE status='pending'")
    if cur.fetchone()[0] > 0:
        print("  [AIファンド] 未約定注文が残っているため今日は見送り")
        cur.close(); conn.close(); return 0

    latest = _latest_trading_date(cur)

    # ── 現ポジション状況 ──
    cur.execute("SELECT code, shares, avg_cost, buy_date, buy_reason, thesis, style, catalyst FROM ai_fund_positions")
    positions = []
    for code, shares, avg_cost, buy_date, buy_reason, thesis, style, catalyst in cur.fetchall():
        cur.execute("""
            SELECT s.name, d.close, p.rsi14, p.chg5d, p.chg25d
            FROM stocks s
            LEFT JOIN daily_prices d ON d.code = s.code AND d.date = %s
            LEFT JOIN price_stats p ON p.code = s.code
            WHERE s.code = %s
        """, (latest, code))
        r = cur.fetchone()
        close = float(r[1]) if r and r[1] else float(avg_cost)
        positions.append({
            "code": code, "name": r[0] if r else code, "shares": int(shares),
            "avg_cost": float(avg_cost), "buy_date": buy_date, "buy_reason": buy_reason,
            "thesis": thesis, "style": style or "通常", "catalyst": catalyst, "close": close,
            "pnl_pct": (close / float(avg_cost) - 1) * 100,
            "hold_days": (today - buy_date).days if buy_date else 0,
            "rsi14": r[2] if r else None, "chg5d": r[3] if r else None, "chg25d": r[4] if r else None,
        })

    # ── 強制ロスカット（AI判断に先立つ規律） ──
    forced_sells = [p for p in positions if p["pnl_pct"] <= LOSSCUT_PCT]

    # ── 候補 ──
    held = {p["code"] for p in positions}
    exclude = held | _cooldown_codes(cur)
    cands = _candidates(cur, exclude)
    if not cands:
        print("  [AIファンド] 候補なし（データ未整備？）→ 見送り")
        cur.close(); conn.close(); return 0

    # ── 昨日までの控え銘柄を候補プールに合流（昇格の道を確保） ──
    cur.execute("""
        SELECT code FROM ai_fund_bench
        WHERE bench_date = (SELECT MAX(bench_date) FROM ai_fund_bench)
    """)
    prev_bench = [r[0] for r in cur.fetchall()]
    cand_codes_now = {c["code"] for c in cands}
    for bcode in prev_bench:
        if bcode in exclude or bcode in cand_codes_now:
            if bcode in cand_codes_now and "bench" not in {t for c in cands if c["code"] == bcode for t in c["tags"]}:
                next(c for c in cands if c["code"] == bcode)["tags"].append("bench")
            continue
        cands_extra = _candidates_one(cur, bcode)
        if cands_extra:
            cands_extra["tags"] = ["bench"]
            cands.append(cands_extra)

    # ── 決算発表予定日（保有＋候補。決算跨ぎの判断材料） ──
    all_codes = sorted(held | {c["code"] for c in cands})
    earn_map = _earnings_dates(cur, conn, all_codes)

    # ── 実測エッジ（週1再計測）と通期予想への進捗率 ──
    _refresh_edge_stats(cur, conn)
    edge_line = _edge_summary(cur)
    prog_map = {c2: _progress_note(cur, c2) for c2 in all_codes}

    # ── 市況コンテキスト（AI考察 + TOPIXレジーム） ──
    cur.execute("SELECT ai_commentary FROM market_summary ORDER BY summary_date DESC LIMIT 1")
    r = cur.fetchone()
    market_ctx = (r[0][:400] if r and r[0] else "（市況コメントなし）")
    cur.execute("""
        SELECT COALESCE(adj_close, close) FROM daily_prices
        WHERE code = %s ORDER BY date DESC LIMIT 200
    """, (BENCH_CODE,))
    bench_px = [float(r2[0]) for r2 in cur.fetchall()]
    if len(bench_px) >= 200:
        ma200 = sum(bench_px) / len(bench_px)
        chg25 = (bench_px[0] / bench_px[25] - 1) * 100 if len(bench_px) > 25 else 0
        regime = "上" if bench_px[0] > ma200 else "下"
        market_ctx += f"\n- TOPIXレジーム: 200日線より{regime}（乖離{(bench_px[0]/ma200-1)*100:+.1f}%）・直近25日{chg25:+.1f}%"

    # ── 投資基準（前回分）・成績フィードバック・環境データ ──
    cur.execute("SELECT statement FROM ai_fund_policy ORDER BY policy_date DESC LIMIT 1")
    r = cur.fetchone()
    policy_prev = r[0] if r else None

    cur.execute("""
        SELECT t.code, s.name, t.pnl_pct, t.hold_days, t.style
        FROM ai_fund_trades t JOIN stocks s ON s.code = t.code
        WHERE t.side = 'sell' ORDER BY t.trade_date DESC LIMIT 12
    """)
    fb_rows = cur.fetchall()
    feedback = None
    if fb_rows:
        wins = sum(1 for r2 in fb_rows if float(r2[2] or 0) > 0)
        lines = [f"- {r2[0]} {r2[1]}: {float(r2[2]):+.1f}% ({r2[3]}日保有・{r2[4] or '通常'})" for r2 in fb_rows]
        feedback = f"直近{len(fb_rows)}トレードの勝率 {wins}/{len(fb_rows)}\n" + "\n".join(lines)

    cur.execute("""
        SELECT group_label, zscore FROM money_flow_weekly
        WHERE group_type='theme' AND flow_class='inflow'
          AND week_end = (SELECT MAX(week_end) FROM money_flow_weekly)
          AND turnover >= 100 AND n_stocks >= 8
        ORDER BY zscore DESC LIMIT 6
    """)
    inflow_themes = "、".join(f"{r2[0]}(Z{float(r2[1]):.1f})" for r2 in cur.fetchall())

    cur.execute("""
        SELECT p.code, s.name, p.chg5d FROM price_stats p
        JOIN stocks s ON s.code = p.code AND s.is_active = 1
        WHERE p.turnover_20d >= 5 ORDER BY p.chg5d DESC LIMIT 10
    """)
    movers = "、".join(f"{r2[1]}({float(r2[2]):+.0f}%)" for r2 in cur.fetchall())

    n_slots = N_POSITIONS - len(positions) + len(forced_sells)
    # 買い予算の目安: 現金 + 売り見込み（強制ロスカット分は今日終値の98%で概算）
    est_cash = state["cash"] + sum(p["close"] * p["shares"] * 0.98 for p in forced_sells)

    prompt = _build_prompt(state, positions, cands, market_ctx, n_slots, est_cash,
                           policy_prev, feedback, inflow_themes, movers, earn_map,
                           edge_line, prog_map)
    out = _call_gemini(prompt)

    sells, buys, market_view, policy_new, bench_out = [], [], "", "", []
    if out:
        market_view = str(out.get("market_view", ""))[:500]
        pol = out.get("policy", "")
        if isinstance(pol, list):  # 箇条書きを配列で返してくるケースに対応
            pol = "\n".join(str(x) for x in pol)
        policy_new = str(pol)[:3000]
        sells = [s0 for s0 in (out.get("sells") or []) if isinstance(s0, dict)]
        buys = [b0 for b0 in (out.get("buys") or []) if isinstance(b0, dict)]
        bench_out = [b0 for b0 in (out.get("bench") or []) if isinstance(b0, dict)]

    # ── ガードレール ──
    cand_codes = {c["code"] for c in cands}
    cand_by_code = {c["code"]: c for c in cands}
    forced_codes = {p["code"] for p in forced_sells}

    valid_sells = []
    for s0 in sells:
        c = str(s0.get("code", "")).strip()
        if c in held and c not in forced_codes and len(valid_sells) < MAX_SWAPS:
            valid_sells.append({"code": c, "reason": str(s0.get("reason", ""))[:1000]})
    is_initial = len(positions) == 0
    if not is_initial:
        valid_sells = valid_sells[:MAX_SWAPS]

    n_buy_slots = N_POSITIONS - len(positions) + len(valid_sells) + len(forced_sells)
    est_cash = state["cash"] + sum(p["close"] * p["shares"] * 0.98
                                   for p in positions
                                   if p["code"] in forced_codes | {s["code"] for s in valid_sells})

    # 先回り（予測）スタイルはポートフォリオの一部に留める:
    # 売却されずに残る先回り保有 + 新規の先回り買い ≤ MAX_ANTICIPATE
    leaving = forced_codes | {s["code"] for s in valid_sells}
    n_antic = sum(1 for p in positions if p["style"] == "先回り" and p["code"] not in leaving)

    valid_buys, budget_sum, seen = [], 0.0, set()
    for b0 in buys:
        c = str(b0.get("code", "")).strip()
        if c not in cand_codes or c in seen or len(valid_buys) >= n_buy_slots:
            continue
        style = "先回り" if str(b0.get("style", "")).strip() == "先回り" else "通常"
        if style == "先回り":
            if n_antic >= MAX_ANTICIPATE:
                print(f"    [ガード] {c}: 先回り枠({MAX_ANTICIPATE})超過のため見送り")
                continue
            n_antic += 1
        try:
            budget = float(b0.get("budget", 0))
        except (TypeError, ValueError):
            budget = 0
        budget = max(BUDGET_MIN, min(BUDGET_MAX, budget or BUDGET_MIN))
        if budget_sum + budget > est_cash:
            budget = est_cash - budget_sum
            if budget < BUDGET_MIN * 0.8:
                continue
        catalyst = str(b0.get("catalyst", "")).strip()[:800]
        if not catalyst:
            # 運営方針3: カタリストが書けない銘柄は買わない
            print(f"    [ガード] {c}: カタリスト未記載のため見送り")
            if style == "先回り":
                n_antic -= 1
            continue
        valid_buys.append({"code": c, "budget": round(budget), "style": style,
                           "reason": str(b0.get("reason", ""))[:1000],
                           "catalyst": catalyst,
                           "thesis": str(b0.get("thesis", ""))[:600]})
        budget_sum += budget
        seen.add(c)

    # 8銘柄維持の充足: AI出力が足りなければ定量上位（タグ数→25日騰落順）で補完
    if len(valid_buys) < n_buy_slots and est_cash - budget_sum >= BUDGET_MIN:
        ranked = sorted((c for c in cands if c["code"] not in seen),
                        key=lambda c: (-len(c["tags"]), -(c["chg25d"] or 0)))
        for c in ranked:
            if len(valid_buys) >= n_buy_slots or est_cash - budget_sum < BUDGET_MIN:
                break
            budget = min(BUDGET_MAX, max(BUDGET_MIN, (est_cash - budget_sum) / max(1, n_buy_slots - len(valid_buys))))
            tag_jp = {"momentum": "上昇モメンタム", "dip": "押し目", "breakout": "高値ブレイク",
                      "value_growth": "割安×成長", "event": "好業績イベント"}
            reasons = "・".join(tag_jp.get(t, t) for t in c["tags"])
            valid_buys.append({"code": c["code"], "budget": round(budget), "style": "通常",
                               "reason": f"[定量補完] {reasons}の条件に合致（25日騰落{_fnum(c['chg25d'])}%・RSI{_fnum(c['rsi14'],0)}）。AI出力不足分を規律的に補充。",
                               "catalyst": f"{reasons}の統計的エッジ（強いトレンド・出来高を伴う銘柄は追随買いを集めやすい）。",
                               "thesis": "購入根拠のトレンドが崩れたら（MA25明確割れ or -12%）撤退。+20%超で利益確定を検討。"})
            budget_sum += budget
            seen.add(c["code"])

    # ── 注文登録 ──
    n_orders = 0
    for p in forced_sells:
        cur.execute("""
            INSERT INTO ai_fund_orders (code, side, shares, reason, decided_date, created_at)
            VALUES (%s,'sell',%s,%s,%s,NOW())
        """, (p["code"], p["shares"],
              f"[ロスカット規律] 含み損{p['pnl_pct']:.1f}%が閾値{LOSSCUT_PCT}%に到達。ルールに従い機械的に撤退。",
              today))
        n_orders += 1
    for s0 in valid_sells:
        pos = next(p for p in positions if p["code"] == s0["code"])
        cur.execute("""
            INSERT INTO ai_fund_orders (code, side, shares, reason, decided_date, created_at)
            VALUES (%s,'sell',%s,%s,%s,NOW())
        """, (s0["code"], pos["shares"], s0["reason"], today))
        n_orders += 1
    for b0 in valid_buys:
        cur.execute("""
            INSERT INTO ai_fund_orders (code, side, budget, reason, catalyst, thesis, style, decided_date, created_at)
            VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,NOW())
        """, (b0["code"], b0["budget"], b0["reason"], b0.get("catalyst", ""), b0["thesis"],
              b0.get("style", "通常"), today))
        n_orders += 1

    # ── 投資基準の保存（日次で蓄積。最新をサイト表示） ──
    if policy_new:
        cur.execute("""
            INSERT INTO ai_fund_policy (policy_date, statement, created_at)
            VALUES (%s, %s, NOW())
            ON DUPLICATE KEY UPDATE statement = VALUES(statement)
        """, (today, policy_new))

    # ── 控え（ベンチ）銘柄の保存: 保有・買い予定と重複しない8銘柄。不足は定量上位で補完 ──
    held_after = (held - {s["code"] for s in valid_sells} - forced_codes) | {b["code"] for b in valid_buys}
    bench_rows, bseen = [], set()
    for b0 in bench_out:
        c = str(b0.get("code", "")).strip()
        if len(bench_rows) >= N_BENCH:
            break
        if c in cand_codes and c not in held_after and c not in bseen:
            style = "先回り" if str(b0.get("style", "")).strip() == "先回り" else "通常"
            bench_rows.append((c, style, str(b0.get("reason", ""))[:500]))
            bseen.add(c)
    if len(bench_rows) < N_BENCH:
        ranked = sorted((c for c in cands if c["code"] not in bseen and c["code"] not in held_after),
                        key=lambda c: (-len(c["tags"]), -(c["chg25d"] or 0)))
        for c in ranked:
            if len(bench_rows) >= N_BENCH:
                break
            bench_rows.append((c["code"], "通常", f"[定量補完] {'/'.join(c['tags'])}の上位候補"))
            bseen.add(c["code"])
    cur.execute("DELETE FROM ai_fund_bench WHERE bench_date = %s", (today,))
    for i, (c, style, reason) in enumerate(bench_rows, 1):
        cur.execute("SELECT close FROM daily_prices WHERE code=%s AND date=%s", (c, latest))
        r = cur.fetchone()
        close_at = float(r[0]) if r and r[0] else None
        cur.execute("""
            INSERT INTO ai_fund_bench (bench_date, rank_no, code, style, reason, close_at, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
        """, (today, i, c, style, reason, close_at))

    cur.execute("UPDATE ai_fund_state SET last_decided=%s, updated_at=NOW() WHERE id=1", (today,))
    if market_view and latest:
        # NAV行が既にある日だけ市況見解を付記する（無ければ捨てる。nav=0のゴミ行を作らない）
        cur.execute("UPDATE ai_fund_nav SET market_view=%s WHERE date=%s", (market_view, latest))
    conn.commit(); cur.close(); conn.close()

    print(f"  [AIファンド] 意思決定: 売{len(forced_sells)+len(valid_sells)} 買{len(valid_buys)}（翌営業日の寄付で約定）")
    for b0 in valid_buys:
        print(f"    買 {b0['code']} {b0['budget']/1e4:.0f}万円: {b0['reason'][:60]}…")
    return n_orders


def status():
    conn = get_conn(); cur = conn.cursor()
    st = _get_state(cur)
    if not st:
        print("未初期化"); return
    print(f"現金: {st['cash']/1e4:,.1f}万円 / 設定日: {st['inception']} / 最終判断: {st['last_decided']}")
    cur.execute("SELECT code, shares, avg_cost, buy_date FROM ai_fund_positions ORDER BY code")
    for r in cur.fetchall():
        print(f"  保有 {r[0]} {r[1]}株 @{float(r[2]):,.1f} ({r[3]})")
    cur.execute("SELECT code, side, budget, shares, status, decided_date FROM ai_fund_orders WHERE status='pending'")
    for r in cur.fetchall():
        print(f"  注文 {r[1]} {r[0]} {'予算' + format(r[2]/1e4, '.0f') + '万' if r[2] else str(r[3]) + '株'} 決定{r[5]}")
    cur.close(); conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--execute" in args:
        execute_orders()
    elif "--nav" in args:
        record_nav()
    elif "--decide" in args:
        decide()
    elif "--status" in args:
        status()
    else:
        print("使い方: python3 ai_fund.py [--execute | --nav | --decide | --status]")
