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
        SELECT id, code, side, budget, shares, reason, thesis, decided_date
        FROM ai_fund_orders WHERE status = 'pending' ORDER BY side DESC, id
    """)  # side DESC → sell を先に処理して現金を作ってから buy
    orders = cur.fetchall()
    filled = 0

    for oid, code, side, budget, shares, reason, thesis, decided in orders:
        if decided >= today:
            continue  # 決定日当日の価格では絶対に約定させない
        cur.execute("SELECT open, close FROM daily_prices WHERE code = %s AND date = %s", (code, today))
        row = cur.fetchone()
        if not row or not row[0] or float(row[0]) <= 0:
            continue  # 当日値なし → 持ち越し
        open_px = float(row[0])

        if side == "sell":
            cur.execute("SELECT shares, avg_cost, buy_date, buy_reason FROM ai_fund_positions WHERE code = %s", (code,))
            pos = cur.fetchone()
            if not pos:
                cur.execute("UPDATE ai_fund_orders SET status='expired', note='ポジションなし' WHERE id=%s", (oid,))
                continue
            p_shares, avg_cost, buy_date, buy_reason = int(pos[0]), float(pos[1]), pos[2], pos[3]
            proceeds = p_shares * open_px
            fee = proceeds * COST_RATE
            pnl = proceeds - fee - p_shares * avg_cost
            pnl_pct = (open_px * (1 - COST_RATE) / avg_cost - 1) * 100
            hold_days = (today - buy_date).days if buy_date else None
            cur.execute("""
                INSERT INTO ai_fund_trades (code, side, shares, price, fee, trade_date, decided_date,
                                            reason, buy_reason, pnl, pnl_pct, hold_days, created_at)
                VALUES (%s,'sell',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            """, (code, p_shares, open_px, round(fee), today, decided, reason, buy_reason,
                  round(pnl), round(pnl_pct, 2), hold_days))
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
                INSERT INTO ai_fund_positions (code, shares, avg_cost, buy_date, buy_reason, thesis, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,NOW())
                ON DUPLICATE KEY UPDATE
                    avg_cost = (avg_cost*shares + VALUES(avg_cost)*VALUES(shares)) / (shares+VALUES(shares)),
                    shares = shares + VALUES(shares)
            """, (code, n, round(avg_cost, 2), today, reason, thesis))
            cur.execute("""
                INSERT INTO ai_fund_trades (code, side, shares, price, fee, trade_date, decided_date, reason, created_at)
                VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,NOW())
            """, (code, n, open_px, round(fee), today, decided, reason))
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

def _candidates(cur, exclude: set[str]) -> list[dict]:
    """観点別に候補を集めて重複排除。exclude（保有中・再購入クールダウン中）は除外。"""
    base_from = """
        FROM price_stats p
        JOIN stocks s ON s.code = p.code AND s.is_active = 1
        LEFT JOIN stock_fundamentals f ON f.code = p.code
        LEFT JOIN theoretical_values t ON t.code = p.code
        WHERE p.turnover_20d >= 3 AND p.close BETWEEN 300 AND 24000
          AND f.market_cap >= 15e9
    """
    sel = """
        SELECT p.code, s.name, p.close, p.chg5d, p.chg25d, p.chg75d, p.rsi14,
               p.dev_ma25, p.dev_high52w, p.vol20_ratio, p.turnover_20d,
               p.ma200_slope, p.break_65d, f.per, f.roe, f.market_cap,
               t.theo_ratio, t.upside_3y_pct, p.rev_growth, p.op_growth
    """
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
    from decimal import Decimal

    def _conv(x):
        return float(x) if isinstance(x, Decimal) else x

    cands: dict[str, dict] = {}
    cols = ["code", "name", "close", "chg5d", "chg25d", "chg75d", "rsi14", "dev_ma25",
            "dev_high52w", "vol20_ratio", "turnover_20d", "ma200_slope", "break_65d",
            "per", "roe", "market_cap", "theo_ratio", "upside_3y_pct", "rev_growth", "op_growth"]
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
    return list(cands.values())[:28]


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


def _build_prompt(state, positions, cands, market_ctx, n_slots, est_cash) -> str:
    pos_lines = []
    for p in positions:
        pos_lines.append(
            f"- {p['code']} {p['name']}: 取得{p['avg_cost']:,.0f}円({str(p['buy_date'])}) 現在{p['close']:,.0f}円 "
            f"損益{p['pnl_pct']:+.1f}% 保有{p['hold_days']}日 RSI{_fnum(p['rsi14'],0)} 5日{_fnum(p['chg5d'])}% 25日{_fnum(p['chg25d'])}%\n"
            f"  購入理由: {p['buy_reason']}\n  シナリオ: {p['thesis']}"
        )
    cand_lines = []
    for c in cands:
        extra = f" イベント:{c['event']}" if c.get("event") else ""
        cand_lines.append(
            f"- {c['code']} {c['name']} [{'/'.join(c['tags'])}] 株価{c['close']:,.0f}円 "
            f"5日{_fnum(c['chg5d'])}% 25日{_fnum(c['chg25d'])}% 75日{_fnum(c['chg75d'])}% RSI{_fnum(c['rsi14'],0)} "
            f"52週高値比{_fnum(c['dev_high52w'])}% 出来高比{_fnum(c['vol20_ratio'])}x 売買代金{_fnum(c['turnover_20d'],0)}億 "
            f"PER{_fnum(c['per'])} ROE{_fnum(c['roe'])}% 理論株価比{_fnum(c['theo_ratio'],2)} 営業益成長{_fnum(c['op_growth'],0)}%{extra}"
        )
    return f"""あなたは日本株のファンドマネージャーです。模擬ファンドを運用しています。

# 運用ルール（厳守）
- 目的: キャピタルゲインの最大化。投資期間の目安は数日〜半年
- 常に{N_POSITIONS}銘柄を保有する。今回は売りと買いをセットで考え、決定後の保有数が{N_POSITIONS}になるようにする
- 今回の買い枠: {n_slots}銘柄（売却を指示すればその分増える。1日の入替は最大{MAX_SWAPS}銘柄まで）
- 予算: 1銘柄 {BUDGET_MIN//10000}万〜{BUDGET_MAX//10000}万円。買い予算の合計は約{est_cash/10000:,.0f}万円以内
- 約定は明日の寄付（成行）。今日の終値からは乖離しうる
- 売買理由は具体的に（何を根拠に・何を期待して・どうなったら降りるか）。理由の水増しや創作は禁止

# 市況
{market_ctx}

# 現在のポートフォリオ（現金 {state['cash']/10000:,.0f}万円）
{chr(10).join(pos_lines) if pos_lines else '（なし・初回構築）'}

# 買い候補（定量スクリーニング済み。この中からのみ選ぶこと）
[タグ] momentum=上昇モメンタム / dip=上昇トレンド中の押し目 / breakout=65日高値ブレイク / value_growth=理論株価比で割安×成長 / event=直近の上方修正・増配
{chr(10).join(cand_lines)}

# 指示
1. 保有銘柄それぞれについてシナリオ通りか点検し、崩れたもの・目標達成したもの・より良い候補に劣後するものを売る（無理に売る必要はない）
2. 買いは候補から選ぶ。分散（同一業種・同一テーマに偏らない）と、モメンタム・押し目・イベント等の組み合わせを意識する
3. 確信度に応じて予算に強弱をつける

以下のJSONのみを出力（説明文・コードブロック不要）:
{{"market_view": "市況の見立て(2文以内)",
 "sells": [{{"code": "XXXX", "reason": "売却理由(シナリオとの照合を含め具体的に)"}}],
 "buys": [{{"code": "XXXX", "budget": 1200000, "reason": "購入理由(根拠を具体的に)", "thesis": "想定シナリオと売却条件(例: 〜を期待。MA25割れか+25%で売却)"}}]}}"""


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
    cur.execute("SELECT code, shares, avg_cost, buy_date, buy_reason, thesis FROM ai_fund_positions")
    positions = []
    for code, shares, avg_cost, buy_date, buy_reason, thesis in cur.fetchall():
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
            "thesis": thesis, "close": close,
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

    # ── 市況コンテキスト ──
    cur.execute("SELECT ai_commentary FROM market_summary ORDER BY summary_date DESC LIMIT 1")
    r = cur.fetchone()
    market_ctx = (r[0][:400] if r and r[0] else "（市況コメントなし）")

    n_slots = N_POSITIONS - len(positions) + len(forced_sells)
    # 買い予算の目安: 現金 + 売り見込み（強制ロスカット分は今日終値の98%で概算）
    est_cash = state["cash"] + sum(p["close"] * p["shares"] * 0.98 for p in forced_sells)

    prompt = _build_prompt(state, positions, cands, market_ctx, n_slots, est_cash)
    out = _call_gemini(prompt)

    sells, buys, market_view = [], [], ""
    if out:
        market_view = str(out.get("market_view", ""))[:500]
        sells = out.get("sells", []) or []
        buys = out.get("buys", []) or []

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

    valid_buys, budget_sum, seen = [], 0.0, set()
    for b0 in buys:
        c = str(b0.get("code", "")).strip()
        if c not in cand_codes or c in seen or len(valid_buys) >= n_buy_slots:
            continue
        try:
            budget = float(b0.get("budget", 0))
        except (TypeError, ValueError):
            budget = 0
        budget = max(BUDGET_MIN, min(BUDGET_MAX, budget or BUDGET_MIN))
        if budget_sum + budget > est_cash:
            budget = est_cash - budget_sum
            if budget < BUDGET_MIN * 0.8:
                continue
        valid_buys.append({"code": c, "budget": round(budget),
                           "reason": str(b0.get("reason", ""))[:1000],
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
            valid_buys.append({"code": c["code"], "budget": round(budget),
                               "reason": f"[定量補完] {reasons}の条件に合致（25日騰落{_fnum(c['chg25d'])}%・RSI{_fnum(c['rsi14'],0)}）。AI出力不足分を規律的に補充。",
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
            INSERT INTO ai_fund_orders (code, side, budget, reason, thesis, decided_date, created_at)
            VALUES (%s,'buy',%s,%s,%s,%s,NOW())
        """, (b0["code"], b0["budget"], b0["reason"], b0["thesis"], today))
        n_orders += 1

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
