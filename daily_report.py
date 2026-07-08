"""
日次相場レポート — 「サイトを見なくても数分で今日の相場がわかる」1枚のHTMLを生成する。

構成（逆ピラミッド: 上から重要順、途中離脱しても概要が掴める）:
  1. 今日の結論   — 指数バッジ + 市場ムード機械判定 + AI市況考察（market_summary）
  2. 相場の数字   — 指数グリッド / 騰落レシオバー / 売買代金
  3. 資金フロー   — 今週 vs 前週で流入が「加速」したテーマ / 「減速」したテーマ（money_flow_weekly）
  4. 値上がり・値下がりTOP5 — 理由（price_events AI調査）+ 30日スパークライン。マイクロ株はフィルタ
  5. トリガー銘柄 — v1基準（新高値×出来高 / 20日ブレイク初動 / 好材料開示×株価反応）※基準は調整前提
  6. 好材料開示   — 上方修正・増配等の当日ピックアップ
  7. ウォッチリスト — 登録銘柄の当日動向（±3%はハイライト）

出力は自己完結HTML（外部JS/CSSなし・チャートはインラインSVG）なので、
Webページ(/daily)としてもメール/LINE添付用としてもそのまま使える。

使い方:
  python3 daily_report.py             # 最新営業日のレポートHTMLを標準出力
  python3 daily_report.py 2026-07-08  # 日付指定
"""
import html as _html
import json
import sys
from datetime import date, datetime, timedelta
from config import get_conn

try:
    from disclosures import CATEGORY_LABELS as _CAT_LABELS
except Exception:
    _CAT_LABELS = {}

# ─── トリガー基準（v1・要調整。変更はここだけ編集する） ─────────────────────
TRIGGER_DEFS = {
    "new_high":  {"label": "🚀 新高値ブレイク",
                  "desc": "52週高値圏(-0.5%以内)×出来高1.5倍以上×時価総額300億+"},
    "breakout":  {"label": "📈 出来高急増の初動",
                  "desc": "20日高値更新×出来高2倍以上×25日騰落率25%未満(過熱前)×時価総額100億+"},
    "good_news": {"label": "📰 好材料×株価反応",
                  "desc": "当日の好材料開示(上方修正・増配等)×株価+5%以上"},
}
WATCH_ALERT_PCT = 3.0   # ウォッチリスト銘柄のハイライト閾値（±%）


# ═══════════════════════════════════════════════════════════════════════════
#  データ収集
# ═══════════════════════════════════════════════════════════════════════════

def _latest_trading_date(cur, d: date | None) -> date | None:
    if d:
        cur.execute("SELECT MAX(date) FROM daily_prices WHERE date <= %s", (d,))
    else:
        cur.execute("SELECT MAX(date) FROM daily_prices")
    r = cur.fetchone()
    return r[0] if r else None


def _fetch_indices(cur, target: date) -> list[dict]:
    """主要指数の当日値と前日比。日本→米→為替の順。"""
    order = ["^N225", "1306.T", "2516.T", "^DJI", "^GSPC", "^IXIC", "USDJPY=X"]
    names = {"^N225": "日経平均", "1306.T": "TOPIX (ETF)", "2516.T": "グロース250 (ETF)",
             "^DJI": "NYダウ", "^GSPC": "S&P 500", "^IXIC": "NASDAQ", "USDJPY=X": "ドル円"}
    decimals = {"USDJPY=X": 2, "1306.T": 1, "2516.T": 1}
    out = []
    for sym in order:
        cur.execute("""
            SELECT date, close FROM market_index_prices
            WHERE symbol = %s AND close IS NOT NULL AND date <= %s
            ORDER BY date DESC LIMIT 30
        """, (sym, target))
        rows = cur.fetchall()
        if not rows:
            continue
        series = [float(r[1]) for r in reversed(rows)]   # 古→新（30日ミニチャート用）
        close = series[-1]
        chg = (close / series[-2] - 1) * 100 if len(series) > 1 and series[-2] else None
        out.append({"name": names[sym], "sym": sym, "close": close, "chg": chg,
                    "series": series,
                    "decimals": decimals.get(sym, 0), "as_of": rows[0][0]})
    return out


def _fetch_breadth(cur, target: date) -> dict:
    cur.execute("""
        SELECT SUM(change_pct > 0), SUM(change_pct < 0), SUM(change_pct = 0),
               SUM(COALESCE(turnover, volume * close)) / 1e12
        FROM daily_prices dp
        JOIN stocks s ON s.code = dp.code AND s.market_id IN (2, 3, 4)
        WHERE dp.date = %s
    """, (target,))
    up, down, flat, tv = cur.fetchone()
    cur.execute("""
        SELECT SUM(COALESCE(turnover, volume * close)) / 1e12
        FROM daily_prices dp
        JOIN stocks s ON s.code = dp.code AND s.market_id IN (2, 3, 4)
        WHERE dp.date = (SELECT MAX(date) FROM daily_prices WHERE date < %s)
    """, (target,))
    tv_prev = cur.fetchone()[0]
    return {"up": int(up or 0), "down": int(down or 0), "flat": int(flat or 0),
            "turnover_t": float(tv or 0), "turnover_prev_t": float(tv_prev or 0)}


def _fetch_ai_commentary(cur, target: date) -> tuple[str, date] | None:
    cur.execute("""
        SELECT ai_commentary, summary_date FROM market_summary
        WHERE summary_date <= %s AND ai_commentary IS NOT NULL
        ORDER BY summary_date DESC LIMIT 1
    """, (target,))
    r = cur.fetchone()
    return (r[0], r[1]) if r else None


def _fetch_flow_changes(cur, target: date) -> dict:
    """資金フロー: 直近週 vs 前週のテーマ別 flow_ratio 変化。"""
    cur.execute("SELECT DISTINCT week_end FROM money_flow_weekly ORDER BY week_end DESC LIMIT 2")
    wks = [r[0] for r in cur.fetchall()]
    if len(wks) < 2:
        return {"accel": [], "decel": [], "size_note": "", "weeks": wks}
    w_now, w_prev = wks[0], wks[1]

    cur.execute("""
        SELECT a.group_label, a.flow_ratio, b.flow_ratio, a.ret_median, a.turnover, a.group_key
        FROM money_flow_weekly a
        JOIN money_flow_weekly b
          ON b.week_end = %s AND b.group_type = a.group_type AND b.group_key = a.group_key
        WHERE a.week_end = %s AND a.group_type = 'theme'
          AND a.flow_ratio IS NOT NULL AND b.flow_ratio IS NOT NULL
          AND a.turnover >= 100 AND a.n_stocks >= 5
    """, (w_prev, w_now))
    rows = [{"label": r[0], "now": float(r[1]), "prev": float(r[2]),
             "ret": float(r[3] or 0), "tv": float(r[4] or 0), "delta": float(r[1]) - float(r[2]),
             "key": r[5]}
            for r in cur.fetchall()]
    accel = sorted([r for r in rows if r["now"] >= 1.05 and r["delta"] > 0.05],
                   key=lambda r: -r["delta"])[:5]
    decel = sorted([r for r in rows if r["prev"] >= 1.05 and r["delta"] < -0.05],
                   key=lambda r: r["delta"])[:3]

    # 規模ローテーション一言（大型 vs 小型+超小型）
    size_note = ""
    cur.execute("""
        SELECT group_key, flow_ratio FROM money_flow_weekly
        WHERE week_end = %s AND group_type = 'size'
    """, (w_now,))
    sz = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
    big, small = sz.get("mega", 0), max(sz.get("small", 0), sz.get("micro", 0))
    if big >= 1.03 and small < 0.97:
        size_note = "大型株に資金集中（指数主導の相場）"
    elif small >= 1.03 and big < 0.97:
        size_note = "小型株に資金が波及（物色相場・個人優位）"
    elif big < 0.97 and small < 0.97:
        size_note = "大型・小型とも売買代金シェア低下（方向感の乏しい相場）"
    return {"accel": accel, "decel": decel, "size_note": size_note, "weeks": wks}


def _extract_reason(ai_summary: str | None) -> str | None:
    """price_events.ai_summary から【変動理由】の1段落だけを取り出す（レポートは簡潔に）。"""
    if not ai_summary:
        return None
    text = ai_summary.strip()
    if "【変動理由】" in text:
        text = text.split("【変動理由】", 1)[1]
        text = text.split("【", 1)[0]
    return text.strip()[:180] or None


def _fetch_movers(cur, target: date) -> dict:
    """値上がり/値下がりTOP5（時価総額50億+でノイズ除去）+ 理由 + 30日終値系列。"""
    out = {"gainers": [], "losers": []}
    for direction, key in [("DESC", "gainers"), ("ASC", "losers")]:
        cur.execute(f"""
            SELECT dp.code, s.name, dp.change_pct, f.market_cap,
                   pe.ai_summary
            FROM daily_prices dp
            JOIN stocks s ON s.code = dp.code AND s.is_active = 1 AND s.market_id IN (2,3,4)
            JOIN stock_fundamentals f ON f.code = dp.code AND f.market_cap >= 5e9
            LEFT JOIN price_events pe
              ON pe.code = dp.code AND pe.event_date = %s AND pe.period = 'daily'
            WHERE dp.date = %s AND dp.change_pct IS NOT NULL
            ORDER BY dp.change_pct {direction}
            LIMIT 5
        """, (target, target))
        for code, name, chg, mcap, reason in cur.fetchall():
            out[key].append({
                "code": str(code), "name": name, "chg": float(chg or 0),
                "mcap_oku": float(mcap) / 1e8 if mcap else None,
                "reason": _extract_reason(reason),
            })
    # スパークライン用の30日終値
    codes = [m["code"] for m in out["gainers"] + out["losers"]]
    if codes:
        ph = ",".join(["%s"] * len(codes))
        cur.execute(f"""
            SELECT code, date, COALESCE(adj_close, close)
            FROM daily_prices
            WHERE code IN ({ph}) AND date > %s AND date <= %s AND close > 0
            ORDER BY code, date
        """, (*codes, target - timedelta(days=45), target))
        series: dict = {}
        for c, d, cl in cur.fetchall():
            series.setdefault(str(c), []).append(float(cl))
        for m in out["gainers"] + out["losers"]:
            m["series"] = series.get(m["code"], [])
    return out


def _fetch_triggers(cur, target: date) -> dict:
    """トリガー銘柄（v1基準）。各基準最大5件。"""
    res = {k: [] for k in TRIGGER_DEFS}

    # 当日騰落
    cur.execute("""
        SELECT ps.code, s.name, dp.change_pct, ps.vol20_ratio, ps.dev_high52w,
               ps.break_20d, ps.chg25d, f.market_cap
        FROM price_stats ps
        JOIN stocks s ON s.code = ps.code AND s.is_active = 1 AND s.market_id IN (2,3,4)
        JOIN daily_prices dp ON dp.code = ps.code AND dp.date = %s
        LEFT JOIN stock_fundamentals f ON f.code = ps.code
        WHERE ps.vol20_ratio >= 1.5
    """, (target,))
    for code, name, chg, volr, dev52, brk20, chg25, mcap in cur.fetchall():
        chg  = float(chg or 0); volr = float(volr or 0)
        dev52 = float(dev52) if dev52 is not None else None
        chg25 = float(chg25) if chg25 is not None else None
        mcap  = float(mcap or 0)
        item = {"code": str(code), "name": name, "chg": chg, "volr": volr}
        if dev52 is not None and dev52 >= -0.5 and mcap >= 3e10 and chg > 0:
            res["new_high"].append(item)
        elif brk20 and volr >= 2.0 and (chg25 is None or chg25 < 25) and mcap >= 1e10 and chg > 0:
            res["breakout"].append(item)
    res["new_high"] = sorted(res["new_high"], key=lambda x: -x["volr"])[:5]
    res["breakout"] = sorted(res["breakout"], key=lambda x: -x["volr"])[:5]

    # 好材料開示 × 株価反応
    cur.execute("""
        SELECT d.code, s.name, dp.change_pct, d.category, d.title
        FROM disclosures d
        JOIN stocks s ON s.code = d.code
        JOIN daily_prices dp ON dp.code = d.code AND dp.date = %s
        WHERE DATE(d.disclosed_at) = %s AND d.sentiment = 1
          AND dp.change_pct >= 5
        ORDER BY dp.change_pct DESC
        LIMIT 5
    """, (target, target))
    for code, name, chg, cat, title in cur.fetchall():
        res["good_news"].append({"code": str(code), "name": name, "chg": float(chg or 0),
                                 "note": cat or (title or "")[:24]})
    return res


def _fetch_disclosures(cur, target: date) -> dict:
    """好材料開示のサマリー。当日分が未取得なら取得済みの直近日にフォールバック。"""
    cur.execute("SELECT MAX(DATE(disclosed_at)) FROM disclosures WHERE DATE(disclosed_at) <= %s", (target,))
    r = cur.fetchone()
    disc_date = r[0] if r else None
    if not disc_date:
        return {"counts": [], "picks": [], "date": None}
    cur.execute("""
        SELECT category, COUNT(*) FROM disclosures
        WHERE DATE(disclosed_at) = %s AND sentiment = 1
        GROUP BY category ORDER BY COUNT(*) DESC
    """, (disc_date,))
    counts = cur.fetchall()
    cur.execute("""
        SELECT d.code, s.name, d.category, LEFT(COALESCE(d.ai_summary, d.title), 90)
        FROM disclosures d
        JOIN stocks s ON s.code = d.code
        WHERE DATE(d.disclosed_at) = %s AND d.sentiment = 1
        ORDER BY (d.ai_summary IS NULL), d.disclosed_at DESC LIMIT 3
    """, (disc_date,))
    picks = [{"code": str(r[0]), "name": r[1], "cat": r[2], "summary": r[3]}
             for r in cur.fetchall()]
    return {"counts": counts, "picks": picks, "date": disc_date}


def _fetch_watchlist(cur, target: date) -> list[dict]:
    cur.execute("""
        SELECT w.code, s.name, dp.change_pct, dp.close, ps.chg5d
        FROM watchlist w
        JOIN stocks s ON s.code = w.code
        LEFT JOIN daily_prices dp ON dp.code = w.code AND dp.date = %s
        LEFT JOIN price_stats ps ON ps.code = w.code
        ORDER BY ABS(COALESCE(dp.change_pct, 0)) DESC
    """, (target,))
    return [{"code": str(r[0]), "name": r[1],
             "chg": float(r[2]) if r[2] is not None else None,
             "close": float(r[3]) if r[3] is not None else None,
             "chg5d": float(r[4]) if r[4] is not None else None}
            for r in cur.fetchall()]


# ═══════════════════════════════════════════════════════════════════════════
#  レンダリング
# ═══════════════════════════════════════════════════════════════════════════

_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0d1117; color: #c9d1d9;
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif;
  font-size: 14px; line-height: 1.6; -webkit-text-size-adjust: 100%; }
.rp { max-width: 680px; margin: 0 auto; padding: 16px 12px 60px; }
.rp a { color: #58a6ff; text-decoration: none; }
.rp-date { font-size: 12px; color: #8b949e; }
.rp-title { font-size: 20px; font-weight: 800; margin: 2px 0 14px; }
.rp-card { background: #161b22; border: 1px solid #21262d; border-radius: 10px;
  padding: 14px; margin-bottom: 14px; }
.rp-h { font-size: 14px; font-weight: 700; color: #c9d1d9; margin-bottom: 10px;
  display: flex; align-items: baseline; gap: 8px; }
.rp-h small { font-size: 11px; color: #484f58; font-weight: 400; }
.rp-mood { display: inline-block; font-size: 12px; font-weight: 700; border-radius: 6px;
  padding: 3px 10px; margin-bottom: 8px; }
.rp-commentary { font-size: 13px; color: #9da7b3; }
.rp-commentary summary { cursor: pointer; color: #58a6ff; font-size: 12px; margin-top: 6px; }
.pos { color: #f85149; } .neg { color: #58a6ff; } .mut { color: #8b949e; }
.idx-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.idx-cell { background: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 8px 10px; }
.idx-name { font-size: 11px; color: #8b949e; }
.idx-val { font-size: 14px; font-weight: 700; }
.idx-chg { font-size: 12px; font-weight: 700; }
.breadth-bar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; margin: 6px 0 4px; }
.breadth-note { font-size: 12px; color: #8b949e; }
.mv-row { display: flex; align-items: center; gap: 10px; padding: 8px 0;
  border-bottom: 1px solid #21262d55; }
.mv-row:last-child { border-bottom: none; }
.mv-chg { font-size: 15px; font-weight: 800; width: 62px; text-align: right; flex-shrink: 0; }
.mv-main { flex: 1; min-width: 0; }
.mv-name { font-weight: 700; font-size: 13.5px; }
.mv-meta { font-size: 11px; color: #484f58; }
.mv-reason { font-size: 12px; color: #9da7b3; margin-top: 2px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.mv-spark { flex-shrink: 0; }
.tr-group { margin-bottom: 10px; }
.tr-group:last-child { margin-bottom: 0; }
.tr-lbl { font-size: 12.5px; font-weight: 700; margin-bottom: 4px; }
.tr-desc { font-size: 10.5px; color: #484f58; font-weight: 400; margin-left: 4px; }
.tr-items { display: flex; flex-wrap: wrap; gap: 6px; }
.tr-chip { background: #0d1117; border: 1px solid #30363d; border-radius: 7px;
  padding: 4px 9px; font-size: 12px; }
.tr-chip a { color: #c9d1d9; font-weight: 600; }
.fl-row { display: flex; align-items: center; gap: 8px; padding: 5px 0; font-size: 13px; }
.fl-name { flex: 1; font-weight: 600; }
.fl-delta { font-weight: 700; }
.dc-pick { padding: 6px 0; border-bottom: 1px solid #21262d55; font-size: 12.5px; }
.dc-pick:last-child { border-bottom: none; }
.wl-row { display: flex; justify-content: space-between; gap: 8px; padding: 6px 0;
  border-bottom: 1px solid #21262d55; font-size: 13px; }
.wl-row:last-child { border-bottom: none; }
.wl-alert { background: #f8514912; margin: 0 -14px; padding: 6px 14px; }
.rp-links { display: flex; flex-wrap: wrap; gap: 10px; font-size: 12.5px; margin-top: 4px; }
.rp-foot { font-size: 11px; color: #484f58; text-align: center; margin-top: 18px; }
@media (max-width: 480px) {
  .idx-grid { grid-template-columns: repeat(2, 1fr); }
  .mv-reason { -webkit-line-clamp: 3; }
}
"""


def _chg_html(v: float | None, digits: int = 1, suffix: str = "%") -> str:
    if v is None:
        return '<span class="mut">—</span>'
    cls = "pos" if v > 0 else ("neg" if v < 0 else "mut")
    return f'<span class="{cls}">{v:+.{digits}f}{suffix}</span>'


def _spark_svg(series: list[float], w: int = 88, h: int = 30) -> str:
    if len(series) < 2:
        return ""
    vmin, vmax = min(series), max(series)
    rng = (vmax - vmin) or 1
    pad = 2
    xs = [pad + i * (w - 2 * pad) / (len(series) - 1) for i in range(len(series))]
    ys = [h - pad - (v - vmin) / rng * (h - 2 * pad) for v in series]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    color = "#f85149" if series[-1] >= series[0] else "#58a6ff"
    return (f'<svg class="mv-spark" width="{w}" height="{h}">'
            f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.5"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="2" fill="{color}"/></svg>')


def _judge_mood(indices: list[dict], breadth: dict) -> tuple[str, str]:
    """(ラベル, 色) の機械判定。"""
    chg = {i["sym"]: i["chg"] for i in indices if i["chg"] is not None}
    n225, topix, growth = chg.get("^N225"), chg.get("1306.T"), chg.get("2516.T")
    tot = breadth["up"] + breadth["down"]
    br = breadth["up"] / tot * 100 if tot else 50
    if n225 is not None and topix is not None:
        if n225 > 0.3 and topix > 0.3 and br >= 55:
            return ("リスクオン — 全面高", "#f85149")
        if n225 < -0.3 and topix < -0.3 and br <= 45:
            return ("リスクオフ — 全面安", "#58a6ff")
        if growth is not None and growth - max(n225, topix) > 0.8:
            return ("小型グロース優位 — 個人物色", "#d29922")
        if br < 45 and max(n225, topix) > 0:
            return ("指数高・中身弱い — 大型偏重", "#d29922")
        if br >= 55 and min(n225, topix) < 0:
            return ("指数安・中身強い — 個別物色", "#d29922")
    return ("まちまち — 方向感なし", "#8b949e")


def build_report_html(target_date: date | None = None) -> str:
    conn = get_conn()
    cur  = conn.cursor()
    target = _latest_trading_date(cur, target_date)
    if not target:
        cur.close(); conn.close()
        return "<p>価格データがありません</p>"

    indices   = _fetch_indices(cur, target)
    breadth   = _fetch_breadth(cur, target)
    ai_res    = _fetch_ai_commentary(cur, target)
    ai_text, ai_date = (ai_res if ai_res else (None, None))
    flows     = _fetch_flow_changes(cur, target)
    movers    = _fetch_movers(cur, target)
    triggers  = _fetch_triggers(cur, target)
    discs     = _fetch_disclosures(cur, target)
    watch     = _fetch_watchlist(cur, target)
    cur.close(); conn.close()

    esc = _html.escape
    wd = "月火水木金土日"[target.weekday()]

    # ── 1. 今日の結論 ──
    mood, mood_color = _judge_mood(indices, breadth)
    ai_html = ""
    if ai_text:
        stale = (f'<span class="mut" style="font-size:11px">（{ai_date.month}/{ai_date.day}の市況考察・当日分は生成待ち）</span> '
                 if ai_date and ai_date != target else "")
        head = esc(ai_text[:160]) + ("…" if len(ai_text) > 160 else "")
        rest = esc(ai_text[160:])
        ai_html = f'<div class="rp-commentary">{stale}{head}'
        if rest:
            ai_html += f'<details><summary>続きを読む</summary><p style="margin-top:6px">{rest}</p></details>'
        ai_html += "</div>"
    idx_badges = " ".join(
        f'<span style="font-size:12.5px;margin-right:10px"><span class="mut">{esc(i["name"])}</span> {_chg_html(i["chg"])}</span>'
        for i in indices if i["sym"] in ("^N225", "1306.T", "2516.T"))
    sec_summary = f"""<div class="rp-card">
  <div class="rp-mood" style="background:{mood_color}22;color:{mood_color}">{mood}</div>
  <div style="margin-bottom:8px">{idx_badges}</div>
  {ai_html}
</div>"""

    # ── 2. 相場の数字 ──
    idx_cells = "".join(f"""<div class="idx-cell">
  <div class="idx-name">{esc(i["name"])}</div>
  <div class="idx-val">{i["close"]:,.{i["decimals"]}f} <span class="idx-chg">{_chg_html(i["chg"])}</span></div>
  {_spark_svg(i["series"], w=120, h=26)}
</div>""" for i in indices)
    tot = breadth["up"] + breadth["down"] + breadth["flat"]
    up_w = breadth["up"] / tot * 100 if tot else 0
    dn_w = breadth["down"] / tot * 100 if tot else 0
    tv_chg = ((breadth["turnover_t"] / breadth["turnover_prev_t"] - 1) * 100
              if breadth["turnover_prev_t"] else None)
    sec_numbers = f"""<div class="rp-card">
  <div class="rp-h">相場の数字</div>
  <div class="idx-grid">{idx_cells}</div>
  <div class="breadth-bar">
    <div style="width:{up_w:.0f}%;background:#f85149"></div>
    <div style="width:{100-up_w-dn_w:.0f}%;background:#30363d"></div>
    <div style="width:{dn_w:.0f}%;background:#58a6ff"></div>
  </div>
  <div class="breadth-note">
    値上がり <b class="pos">{breadth["up"]:,}</b> / 値下がり <b class="neg">{breadth["down"]:,}</b> 銘柄
    ・売買代金 {breadth["turnover_t"]:.1f}兆円（前日比 {_chg_html(tv_chg, 0)}）
  </div>
</div>"""

    # ── 3. 資金フロー ──
    from urllib.parse import quote as _q

    def _fl_rows(items, sign):
        rows = ""
        for r in items:
            arrow = "↑" if sign > 0 else "↓"
            cls = "pos" if sign > 0 else "neg"
            rows += f"""<div class="fl-row">
  <span class="fl-name"><a href="/flowgroup?type=theme&key={_q(str(r.get("key", r["label"])))}" style="color:inherit">{esc(r["label"])}</a></span>
  <span class="mut" style="font-size:11px">{r["prev"]:.2f}x→{r["now"]:.2f}x</span>
  <span class="fl-delta {cls}">{arrow}{abs(r["delta"]):.2f}</span>
  <span style="width:56px;text-align:right">{_chg_html(r["ret"])}</span>
</div>"""
        return rows
    flow_body = ""
    if flows["accel"]:
        flow_body += f'<div class="tr-lbl pos" style="margin-bottom:2px">流入が加速中</div>{_fl_rows(flows["accel"], 1)}'
    if flows["decel"]:
        flow_body += f'<div class="tr-lbl neg" style="margin:8px 0 2px">流入が減速・流出へ</div>{_fl_rows(flows["decel"], -1)}'
    if flows["size_note"]:
        flow_body += f'<div class="breadth-note" style="margin-top:8px">🔄 {esc(flows["size_note"])}</div>'
    if not flow_body:
        flow_body = '<div class="mut" style="font-size:12px">今週は目立ったトレンド変化なし</div>'
    sec_flows = f"""<div class="rp-card">
  <div class="rp-h">資金フローの変化 <small>今週 vs 前週の売買代金シェア（<a href="/flows">詳細</a>）</small></div>
  {flow_body}
</div>"""

    # ── 4. 値上がり・値下がり ──
    def _mv_rows(items):
        rows = ""
        for m in items:
            mc = f'{m["mcap_oku"]:,.0f}億円' if m["mcap_oku"] else ""
            reason = f'<div class="mv-reason">{esc(m["reason"])}</div>' if m["reason"] else ""
            rows += f"""<div class="mv-row">
  <div class="mv-chg">{_chg_html(m["chg"])}</div>
  <div class="mv-main">
    <div class="mv-name"><a href="/stock/{m["code"]}">{esc(m["name"])}</a> <span class="mv-meta">{m["code"]} {mc}</span></div>
    {reason}
  </div>
  {_spark_svg(m.get("series", []))}
</div>"""
        return rows or '<div class="mut" style="font-size:12px">データなし</div>'
    sec_movers = f"""<div class="rp-card">
  <div class="rp-h">値上がりTOP5 <small>時価総額50億円未満は除外・チャートは直近30日</small></div>
  {_mv_rows(movers["gainers"])}
</div>
<div class="rp-card">
  <div class="rp-h">値下がりTOP5</div>
  {_mv_rows(movers["losers"])}
</div>"""

    # ── 5. トリガー銘柄 ──
    tr_groups = ""
    for key, meta in TRIGGER_DEFS.items():
        items = triggers.get(key, [])
        if not items:
            continue
        chips = "".join(
            f'<span class="tr-chip"><a href="/stock/{t["code"]}">{esc(t["name"])}</a> '
            f'{_chg_html(t["chg"])}'
            + (f' <span class="mut">出来高{t["volr"]:.1f}x</span>' if t.get("volr") else "")
            + (f' <span class="mut">{esc(t["note"])}</span>' if t.get("note") else "")
            + '</span>'
            for t in items)
        tr_groups += f"""<div class="tr-group">
  <div class="tr-lbl">{meta["label"]}<span class="tr-desc">{meta["desc"]}</span></div>
  <div class="tr-items">{chips}</div>
</div>"""
    if not tr_groups:
        tr_groups = '<div class="mut" style="font-size:12px">本日トリガーに掛かった銘柄はありません</div>'
    sec_triggers = f"""<div class="rp-card">
  <div class="rp-h">トリガー銘柄 <small>基準v1・調整前提</small></div>
  {tr_groups}
</div>"""

    # ── 6. 好材料開示 ──
    disc_date_note = ""
    if discs.get("date") and discs["date"] != target:
        disc_date_note = f'{discs["date"].month}/{discs["date"].day}分・'
    cat_note = disc_date_note + ("・".join(f"{esc(_CAT_LABELS.get(c, c))} {n}件" for c, n in discs["counts"][:4]) if discs["counts"] else "")
    picks = "".join(
        f'<div class="dc-pick"><a href="/stock/{p["code"]}">{esc(p["name"])}</a> '
        f'<span class="mut">[{esc(_CAT_LABELS.get(p["cat"], p["cat"] or ""))}]</span> {esc(p["summary"] or "")}</div>'
        for p in discs["picks"])
    sec_discs = f"""<div class="rp-card">
  <div class="rp-h">好材料開示 <small>{cat_note}（<a href="/disclosures">一覧</a>）</small></div>
  {picks or '<div class="mut" style="font-size:12px">本日の好材料開示はありません</div>'}
</div>""" if (discs["counts"] or discs["picks"]) else ""

    # ── 7. ウォッチリスト ──
    wl_rows = ""
    for w in watch:
        alert = w["chg"] is not None and abs(w["chg"]) >= WATCH_ALERT_PCT
        cls = ' class="wl-row wl-alert"' if alert else ' class="wl-row"'
        wl_rows += f"""<div{cls}>
  <span><a href="/stock/{w["code"]}">{esc(w["name"])}</a>{"⚡" if alert else ""}</span>
  <span>{f'{w["close"]:,.0f}円' if w["close"] else "—"}
    {_chg_html(w["chg"])} <span class="mut" style="font-size:11px">5日{_chg_html(w["chg5d"])}</span></span>
</div>"""
    sec_watch = f"""<div class="rp-card">
  <div class="rp-h">ウォッチリスト <small>±{WATCH_ALERT_PCT:.0f}%動いた銘柄は⚡</small></div>
  {wl_rows}
</div>""" if wl_rows else ""

    # ── 組み立て ──
    # <!--DATENAV--> は配信時(app.py)に前日/翌日ナビへ置換されるプレースホルダ
    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>日次レポート {target}</title>
<style>{_CSS}</style>
</head><body>
<div class="rp">
  <div class="rp-date">{target} ({wd}) 大引け後</div>
  <div class="rp-title">📰 日次相場レポート</div>
  <!--DATENAV-->
  {sec_summary}
  {sec_numbers}
  {sec_flows}
  {sec_movers}
  {sec_triggers}
  {sec_discs}
  {sec_watch}
  <div class="rp-card">
    <div class="rp-h">もっと詳しく</div>
    <div class="rp-links">
      <a href="/flows">資金フロー</a><a href="/themes">テーマ分析</a>
      <a href="/events">ランキング・イベント</a><a href="/disclosures">適時開示</a>
      <a href="/screen">スクリーニング</a><a href="/">ホーム</a>
    </div>
  </div>
  <div class="rp-foot">自動生成レポート — データ: Yahoo Finance / J-Quants / TDnet / kabutan</div>
</div>
</body></html>"""


# ═══════════════════════════════════════════════════════════════════════════
#  蓄積（daily_run.py から毎営業日呼ばれる）
# ═══════════════════════════════════════════════════════════════════════════

def ensure_table():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            report_date DATE PRIMARY KEY,
            html        MEDIUMTEXT,
            created_at  DATETIME
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def save_report(target_date: date | None = None) -> date | None:
    """レポートを生成してDBに保存する（その日の状態のスナップショットとして蓄積）。"""
    ensure_table()
    html = build_report_html(target_date)
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT MAX(date) FROM daily_prices" if not target_date else
                "SELECT MAX(date) FROM daily_prices WHERE date <= %s",
                () if not target_date else (target_date,))
    d = cur.fetchone()[0]
    if not d:
        cur.close(); conn.close()
        return None
    cur.execute("""
        INSERT INTO daily_reports (report_date, html, created_at)
        VALUES (%s, %s, NOW())
        ON DUPLICATE KEY UPDATE html = VALUES(html), created_at = VALUES(created_at)
    """, (d, html))
    conn.commit()
    cur.close()
    conn.close()
    print(f"  日次レポート保存: {d}")
    return d


def load_report(target_date: date) -> str | None:
    """保存済みレポートHTMLを返す。無ければNone。"""
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT html FROM daily_reports WHERE report_date = %s", (target_date,))
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None
    finally:
        cur.close()
        conn.close()


def report_dates(limit: int = 60) -> list[date]:
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT report_date FROM daily_reports ORDER BY report_date DESC LIMIT %s", (limit,))
        return [r[0] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    d = None
    for a in args:
        if not a.startswith("--"):
            d = datetime.strptime(a, "%Y-%m-%d").date()
    if "--save" in args:
        save_report(d)
    else:
        print(build_report_html(d))
