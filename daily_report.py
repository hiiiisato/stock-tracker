"""
日次相場レポート — 「サイトを見なくても数分で今日の相場がわかる」1枚のHTMLを生成する。

構成（逆ピラミッド: 上から重要順、途中離脱しても概要が掴める）:
  1. 相場の数字   — 指数グリッド / 騰落レシオバー / 売買代金（一番上）
  2. 今日の結論   — 市場ムード機械判定 + AI市況考察（market_summary）
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
import os
import sys
from datetime import date, datetime, timedelta
from config import get_conn

# 日次レポート全文（/daily）の公開URL既定値。REPORT_BASE_URL 環境変数で上書きする。
# ※Renderの実URLはサービス名＋サフィックス（render.yamlの kabushiki-tracker とは一致しない）。
#   本番は /daily を配信している下記ホスト（kabutanプロキシと同一サービス）。2026-07で到達確認済み。
DEFAULT_REPORT_BASE_URL = "https://stock-tracker-rfqn.onrender.com"

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
    """資金フロー: 最新週の「本物の資金流入(買い優勢)」と「投げ売り警戒(大商い×下落)」。
    Zスコア（母数非依存の流入強度）でノイズ（小グループの偶然のブレ）を排除する。"""
    cur.execute("SELECT MAX(week_end) FROM money_flow_weekly")
    r = cur.fetchone()
    w_now = r[0] if r and r[0] else None
    if not w_now:
        return {"inflow": [], "dump": [], "size_note": "", "week": None}

    cur.execute("""
        SELECT group_label, group_key, flow_ratio, zscore, ret_median, breadth, turnover, n_stocks, flow_class
        FROM money_flow_weekly
        WHERE week_end = %s AND group_type = 'theme' AND zscore IS NOT NULL
    """, (w_now,))
    rows = [{"label": r[0], "key": r[1], "flow": float(r[2] or 0), "z": float(r[3] or 0),
             "ret": float(r[4] or 0), "breadth": float(r[5] or 0), "tv": float(r[6] or 0),
             "n": int(r[7] or 0), "cls": r[8]}
            for r in cur.fetchall()]
    # 本物の資金流入: 買い優勢(inflow) かつ 規模足切り(100億+・8銘柄+)、Zスコア順
    inflow = sorted([r for r in rows if r["cls"] == "inflow" and r["tv"] >= 100 and r["n"] >= 8],
                    key=lambda r: -r["z"])[:6]
    # 投げ売り警戒: 大商い×下落(dump)、Zスコア順（貴重な逆張り/警戒情報として保持）
    dump = sorted([r for r in rows if r["cls"] == "dump" and r["tv"] >= 100],
                  key=lambda r: -r["z"])[:4]

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
    return {"inflow": inflow, "dump": dump, "size_note": size_note, "week": w_now}


def _fetch_youtube_daily(cur, target: date) -> dict | None:
    """当日（無ければ直近）のYouTube日次要約を取得（youtube_insights.py --daily が生成）。"""
    try:
        cur.execute("""
            SELECT report_date, n_videos, summary, consensus, themes_json, stocks_json
            FROM youtube_daily WHERE report_date <= %s
            ORDER BY report_date DESC LIMIT 1
        """, (target,))
    except Exception:  # noqa: BLE001  テーブル未作成（未実行）でもレポートは壊さない
        return None
    row = cur.fetchone()
    if not row:
        return None
    rd, n, summary, consensus, tj, sj = row
    return {"date": rd, "n": n or 0, "summary": summary or "", "consensus": consensus or "",
            "themes": json.loads(tj or "[]"), "stocks": json.loads(sj or "[]")}


def _extract_reason(ai_summary: str | None) -> str | None:
    """price_events.ai_summary から変動理由＋背景・詳細（＝業績の従来→修正後の数字等）を
    取り出す。イベント履歴と同等の具体性を日次レポートでも見せる（参考ソースだけ落とす）。"""
    if not ai_summary:
        return None
    text = ai_summary.strip().split("【参考ソース】", 1)[0].split("【参考", 1)[0]
    # 【変動理由】と【背景・詳細】の本文を見出しを外して連結（数値・固有名詞を残す）
    parts = []
    for label in ("【変動理由】", "【背景・詳細】", "【背景】", "【詳細】"):
        if label in text:
            seg = text.split(label, 1)[1].split("【", 1)[0].strip()
            if seg and seg not in parts:
                parts.append(seg)
    body = " ".join(parts) if parts else text.strip()
    return body.strip()[:600] or None


def _fetch_movers(cur, target: date) -> dict:
    """値上がり/値下がりTOP5（時価総額50億+でノイズ除去）+ 理由 + 30日終値系列。"""
    out = {"gainers": [], "losers": []}
    for direction, key in [("DESC", "gainers"), ("ASC", "losers")]:
        cur.execute(f"""
            SELECT dp.code, s.name, dp.change_pct, f.market_cap,
                   pe.ai_summary, pe.reason_category
            FROM daily_prices dp
            JOIN stocks s ON s.code = dp.code AND s.is_active = 1 AND s.market_id IN (2,3,4)
            JOIN stock_fundamentals f ON f.code = dp.code AND f.market_cap >= 5e9
            LEFT JOIN price_events pe
              ON pe.code = dp.code AND pe.event_date = %s AND pe.period = 'daily'
            WHERE dp.date = %s AND dp.change_pct IS NOT NULL
            ORDER BY dp.change_pct {direction}
            LIMIT 5
        """, (target, target))
        from event_classifier import label_of as _cat_label
        for code, name, chg, mcap, reason, rcat in cur.fetchall():
            out[key].append({
                "code": str(code), "name": name, "chg": float(chg or 0),
                "mcap_oku": float(mcap) / 1e8 if mcap else None,
                "reason": _extract_reason(reason),
                "cat": _cat_label(rcat),
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


def _fetch_revisions(cur, target: date) -> list[dict]:
    """業績修正・増配（当日+前営業日の発表）を当日の株価反応と合わせて返す。
    前営業日の引け後発表は当日（target）の値動きに現れるため、両日分を対象にする。"""
    cur.execute("SELECT MAX(date) FROM daily_prices WHERE date < %s", (target,))
    r = cur.fetchone()
    prev_day = r[0] if r and r[0] else target
    cur.execute("""
        SELECT r.code, s.name, r.period_type, r.announced_at,
               r.op_old, r.op_new, r.op_chg_pct, r.ord_chg_pct, r.net_chg_pct,
               r.revenue_chg_pct, r.dps_old, r.dps_new, r.direction, r.is_turnaround,
               dp.change_pct, r.session
        FROM forecast_revisions r
        JOIN stocks s ON s.code = r.code
        LEFT JOIN daily_prices dp ON dp.code = r.code AND dp.date = %s
        WHERE r.announced_at IN (%s, %s)
        ORDER BY r.direction DESC, ABS(COALESCE(r.op_chg_pct, r.ord_chg_pct, r.net_chg_pct, 0)) DESC
    """, (target, target, prev_day))
    out, seen = [], set()
    for row in cur.fetchall():
        code = str(row[0])
        # 同一銘柄で通期(A)と上期(H)の両方がある場合は通期を優先（ソート順で先に来た方を採用しA優先に補正）
        key = code
        if key in seen and row[2] != "A":
            continue
        if key in seen:
            out = [o for o in out if o["code"] != code]
        seen.add(key)
        out.append({
            "code": code, "name": row[1], "period_type": row[2],
            "announced_at": row[3],
            "op_chg": float(row[6]) if row[6] is not None else None,
            "ord_chg": float(row[7]) if row[7] is not None else None,
            "net_chg": float(row[8]) if row[8] is not None else None,
            "rev_chg": float(row[9]) if row[9] is not None else None,
            "dps_old": float(row[10]) if row[10] is not None else None,
            "dps_new": float(row[11]) if row[11] is not None else None,
            "direction": int(row[12] or 0), "turnaround": int(row[13] or 0),
            "px_chg": float(row[14]) if row[14] is not None else None,
            "session": row[15],
        })
    return out[:12]


def _attach_series(cur, target: date, items: list[dict]) -> None:
    """items（'code'キーを持つdictのリスト）に直近30営業日の終値系列 'series' を付与する。
    ミニチャート（スパークライン）描画用。値上がりTOP5と同じ見せ方を各セクションで共有する。"""
    codes = list({it["code"] for it in items if it.get("code")})
    if not codes:
        return
    ph = ",".join(["%s"] * len(codes))
    cur.execute(f"""
        SELECT code, COALESCE(adj_close, close)
        FROM daily_prices
        WHERE code IN ({ph}) AND date > %s AND date <= %s AND close > 0
        ORDER BY code, date
    """, (*codes, target - timedelta(days=45), target))
    ser: dict = {}
    for c, cl in cur.fetchall():
        ser.setdefault(str(c), []).append(float(cl))
    for it in items:
        it["series"] = ser.get(it["code"], [])


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
    # 選定: 当日の好材料(sentiment=1)を「AI要約あり→時価総額(=影響度の代理)が大きい順」で上位5件。
    # 旧仕様は「新しい順3件」で、大型の重要な上方修正が新着に押し出される問題があったため見直し。
    cur.execute("""
        SELECT d.code, s.name, d.category, COALESCE(d.ai_summary, d.title),
               (TIME(d.disclosed_at) >= '15:00') AS after_close
        FROM disclosures d
        JOIN stocks s ON s.code = d.code
        LEFT JOIN stock_fundamentals f ON f.code = d.code
        WHERE DATE(d.disclosed_at) = %s AND d.sentiment = 1
        ORDER BY (d.ai_summary IS NULL), COALESCE(f.market_cap, 0) DESC, d.disclosed_at DESC
        LIMIT 12
    """, (disc_date,))
    picks, seen = [], set()          # 同一銘柄の重複開示（修正の再修正等）は最新1件に集約
    for r in cur.fetchall():
        code = str(r[0])
        if code in seen:
            continue
        seen.add(code)
        picks.append({"code": code, "name": r[1], "cat": r[2], "summary": (r[3] or "")[:300],
                      "after_close": bool(r[4])})
        if len(picks) >= 5:
            break
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
.yt-sub { font-size: 11px; font-weight: 700; color: #8b949e; margin: 10px 0 3px; }
.yt-item { font-size: 12.5px; color: #c9d1d9; line-height: 1.65; padding: 1px 0; }
.yt-item a { color: #79c0ff; text-decoration: none; }
.yt-item a:hover { text-decoration: underline; }
.yt-item .mut { font-size: 11.5px; }
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
.mv-cat { font-size: 11px; font-weight: 700; color: #d2a8ff; background: #bc8cff14;
  border: 1px solid #bc8cff40; border-radius: 6px; padding: 0 6px; white-space: nowrap; }
.mv-reason { font-size: 12px; color: #9da7b3; margin-top: 3px; line-height: 1.6; }
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


def _fetch_ai_fund(cur, target: date) -> dict | None:
    """AIファンドの当日サマリー（NAV・当日約定・翌日予定・保有損益）。未稼働ならNone。"""
    try:
        cur.execute("SELECT cash FROM ai_fund_state WHERE id = 1")
        st = cur.fetchone()
    except Exception:
        return None
    if not st:
        return None

    out = {"nav": None, "pnl_pct": None, "vs_topix": None,
           "fills": [], "pending": [], "best": None, "worst": None}
    cur.execute("SELECT nav FROM ai_fund_nav WHERE nav > 0 ORDER BY date LIMIT 1")
    first = cur.fetchone()
    cur.execute("SELECT nav, bench FROM ai_fund_nav WHERE nav > 0 AND date <= %s ORDER BY date DESC LIMIT 1", (target,))
    last = cur.fetchone()
    if last:
        out["nav"] = float(last[0])
        out["pnl_pct"] = (float(last[0]) / 10_000_000 - 1) * 100
        cur.execute("SELECT bench FROM ai_fund_nav WHERE nav > 0 AND bench IS NOT NULL ORDER BY date LIMIT 1")
        b0 = cur.fetchone()
        if first and b0 and last[1]:
            fund_ret = float(last[0]) / float(first[0]) - 1
            bench_ret = float(last[1]) / float(b0[0]) - 1
            out["vs_topix"] = (fund_ret - bench_ret) * 100

    # 当日の約定
    cur.execute("""
        SELECT t.side, t.code, s.name, t.shares, t.price, t.pnl_pct, t.reason
        FROM ai_fund_trades t JOIN stocks s ON s.code = t.code
        WHERE t.trade_date = %s ORDER BY t.side DESC
    """, (target,))
    out["fills"] = cur.fetchall()

    # 翌営業日の売買予定
    cur.execute("""
        SELECT o.side, o.code, s.name, o.reason
        FROM ai_fund_orders o JOIN stocks s ON s.code = o.code
        WHERE o.status = 'pending' ORDER BY o.side DESC
    """)
    out["pending"] = cur.fetchall()

    # 保有中のベスト/ワースト
    cur.execute("""
        SELECT p.code, s.name, p.avg_cost,
               (SELECT close FROM daily_prices d WHERE d.code = p.code AND d.date <= %s ORDER BY d.date DESC LIMIT 1)
        FROM ai_fund_positions p JOIN stocks s ON s.code = p.code
    """, (target,))
    pos = [(c, n, (float(cl) / float(ac) - 1) * 100) for c, n, ac, cl in cur.fetchall() if cl]
    if pos:
        pos.sort(key=lambda x: -x[2])
        out["best"], out["worst"] = pos[0], pos[-1]
    return out


def build_report_html(target_date: date | None = None) -> str:
    conn = get_conn()
    cur  = conn.cursor()
    target = _latest_trading_date(cur, target_date)
    if not target:
        cur.close(); conn.close()
        return "<p>価格データがありません</p>"

    indices   = _fetch_indices(cur, target)
    revisions = _fetch_revisions(cur, target)
    breadth   = _fetch_breadth(cur, target)
    ai_res    = _fetch_ai_commentary(cur, target)
    ai_text, ai_date = (ai_res if ai_res else (None, None))
    flows     = _fetch_flow_changes(cur, target)
    ytd       = _fetch_youtube_daily(cur, target)
    movers    = _fetch_movers(cur, target)
    triggers  = _fetch_triggers(cur, target)
    discs     = _fetch_disclosures(cur, target)
    watch     = _fetch_watchlist(cur, target)
    aifund    = _fetch_ai_fund(cur, target)
    # 業績修正・トリガー銘柄にもミニチャート（値上がりTOP5と同じ30日スパークライン）を付与
    _attach_series(cur, target, revisions)
    _attach_series(cur, target, [it for lst in triggers.values() for it in lst])
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

    # ── 2.5 今日のYouTube要約（資金フローの前） ──
    sec_youtube = ""
    if ytd and (ytd["summary"] or ytd["themes"] or ytd["stocks"]):
        theme_rows = "".join(
            f'<div class="yt-item"><b>{esc(t.get("theme", ""))}</b>'
            + (f' <span class="mut">— {esc(t.get("note", ""))}</span>' if t.get("note") else "")
            + "</div>"
            for t in ytd["themes"][:4] if t.get("theme"))
        stock_rows = "".join(
            "<div class=\"yt-item\">"
            + (f'<a href="/stock/{s["code"]}"><b>{esc(s.get("name", ""))}</b> <span class="mut">{s["code"]}</span></a>'
               if s.get("code") else f'<b>{esc(s.get("name", ""))}</b>')
            + (f' <span class="mut">— {esc(s.get("note", ""))}</span>' if s.get("note") else "")
            + "</div>"
            for s in ytd["stocks"][:8] if s.get("name"))
        stale = ("" if ytd["date"] == target
                 else f'<span class="mut" style="font-size:11px">（{ytd["date"].month}/{ytd["date"].day}分）</span>')
        cons = (f'<div class="mut" style="font-size:12px;margin-top:5px">🧭 {esc(ytd["consensus"])}</div>'
                if ytd["consensus"] else "")
        sec_youtube = f"""<div class="rp-card">
  <div class="rp-h">📺 今日のYouTube要約 <small>岩井コスモ証券・日本株速報・日経CNBCを巡回（{ytd["n"]}本）{stale}・<a href="/youtube">詳細</a></small></div>
  <div style="font-size:13px;line-height:1.65">{esc(ytd["summary"])}</div>
  {cons}
  {f'<div class="yt-sub">🔥 注目テーマ</div>{theme_rows}' if theme_rows else ""}
  {f'<div class="yt-sub">👀 個別銘柄</div>{stock_rows}' if stock_rows else ""}
</div>"""

    # ── 3. 資金フロー ──
    from urllib.parse import quote as _q

    def _fl_rows(items):
        rows = ""
        for r in items:
            rows += f"""<div class="fl-row">
  <span class="fl-name"><a href="/screen?gtype=theme&gkey={_q(str(r.get("key", r["label"])))}" style="color:inherit">{esc(r["label"])}</a></span>
  <span class="mut" style="font-size:11px">{r["flow"]:.2f}x・{r["n"]}銘柄</span>
  <span class="fl-delta" style="color:#8b949e">勢いZ{r["z"]:+.1f}</span>
  <span style="width:100px;text-align:right">{_chg_html(r["ret"])} <span class="mut" style="font-size:11px">上昇{r["breadth"]:.0f}%</span></span>
</div>"""
        return rows
    flow_body = ""
    if flows["inflow"]:
        flow_body += f'<div class="tr-lbl pos" style="margin-bottom:2px">🔥 本物の資金流入（買われて上昇中）</div>{_fl_rows(flows["inflow"])}'
    if flows["dump"]:
        flow_body += f'<div class="tr-lbl neg" style="margin:8px 0 2px">⚠️ 投げ売り警戒（大商いだが下落）</div>{_fl_rows(flows["dump"])}'
    if flows["size_note"]:
        flow_body += f'<div class="breadth-note" style="margin-top:8px">🔄 {esc(flows["size_note"])}</div>'
    if not flow_body:
        flow_body = '<div class="mut" style="font-size:12px">今週は目立った資金流入・投げ売りなし</div>'
    sec_flows = f"""<div class="rp-card">
  <div class="rp-h">資金フロー <small>Zスコア×株価上昇で「買い優勢」を抽出・投げ売りは別枠（<a href="/flows">詳細</a>）</small></div>
  {flow_body}
</div>"""

    # ── 4. 値上がり・値下がり ──
    def _mv_rows(items):
        rows = ""
        for m in items:
            mc = f'{m["mcap_oku"]:,.0f}億円' if m["mcap_oku"] else ""
            reason = f'<div class="mv-reason">{esc(m["reason"])}</div>' if m["reason"] else ""
            cat = f'<span class="mv-cat">{esc(m["cat"])}</span> ' if m.get("cat") else ""
            rows += f"""<div class="mv-row">
  <div class="mv-chg">{_chg_html(m["chg"])}</div>
  <div class="mv-main">
    <div class="mv-name">{cat}<a href="/stock/{m["code"]}">{esc(m["name"])}</a> <span class="mv-meta">{m["code"]} {mc}</span></div>
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

    # ── 4.5 業績修正・増配 ──
    def _rev_label(rv) -> str:
        parts = []
        if rv["turnaround"]:
            parts.append("黒字転換")
        if rv["op_chg"] is not None and rv["op_chg"] != 0:
            parts.append(f'営業益{rv["op_chg"]:+.0f}%')
        elif rv["ord_chg"] is not None and rv["ord_chg"] != 0:
            parts.append(f'経常益{rv["ord_chg"]:+.0f}%')
        elif rv["net_chg"] is not None and rv["net_chg"] != 0:
            parts.append(f'純利益{rv["net_chg"]:+.0f}%')
        if rv["rev_chg"] is not None and rv["rev_chg"] != 0 and len(parts) < 2:
            parts.append(f'売上{rv["rev_chg"]:+.0f}%')
        if rv["dps_old"] is not None and rv["dps_new"] is not None and rv["dps_new"] != rv["dps_old"]:
            parts.append(f'配当{rv["dps_old"]:.0f}→{rv["dps_new"]:.0f}円')
        return "・".join(parts) if parts else ("上方修正" if rv["direction"] > 0 else "下方修正")

    rev_rows = ""
    for rv in revisions:
        icon = "⤴️" if rv["direction"] > 0 else "⤵️"
        pt_lbl = "通期" if rv["period_type"] == "A" else "上期"
        sess = {"intraday": "場中", "after": "引け後"}.get(rv.get("session"), "")
        sess_html = f' <span class="mut">{sess}発表</span>' if sess else ""
        ann = f'{rv["announced_at"].month}/{rv["announced_at"].day}発表'
        rev_rows += f"""<div class="mv-row">
  <div class="mv-chg">{_chg_html(rv["px_chg"])}</div>
  <div class="mv-main">
    <div class="mv-name">{icon} <a href="/stock/{rv["code"]}">{esc(rv["name"])}</a>
      <span class="mv-meta">{rv["code"]} ・ {ann}{sess_html}</span></div>
    <div class="mv-reason">{pt_lbl}予想 {esc(_rev_label(rv))}</div>
  </div>
  {_spark_svg(rv.get("series", []))}
</div>"""
    sec_revisions = f"""<div class="rp-card">
  <div class="rp-h">業績修正・増配 <small>直近発表の会社予想の変化と株価反応（開示検知で当日反映）</small></div>
  {rev_rows}
</div>""" if rev_rows else ""

    # ── 5. トリガー銘柄 ──
    tr_groups = ""
    for key, meta in TRIGGER_DEFS.items():
        items = triggers.get(key, [])
        if not items:
            continue
        rows = ""
        for t in items:
            meta_bits = []
            if t.get("volr"):
                meta_bits.append(f'出来高{t["volr"]:.1f}x')
            if t.get("note"):
                meta_bits.append(esc(t["note"]))
            meta_html = f' <span class="mv-meta">{" ・ ".join(meta_bits)}</span>' if meta_bits else ""
            rows += f"""<div class="mv-row">
  <div class="mv-chg">{_chg_html(t["chg"])}</div>
  <div class="mv-main">
    <div class="mv-name"><a href="/stock/{t["code"]}">{esc(t["name"])}</a>
      <span class="mv-meta">{t["code"]}</span>{meta_html}</div>
  </div>
  {_spark_svg(t.get("series", []))}
</div>"""
        tr_groups += f"""<div class="tr-group">
  <div class="tr-lbl">{meta["label"]}<span class="tr-desc">{meta["desc"]}</span></div>
  {rows}
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
        f'<span class="mut">[{esc(_CAT_LABELS.get(p["cat"], p["cat"] or ""))}]</span>'
        + (' <span style="color:#d29922;font-size:11px;font-weight:700">⏰引け後＝明日の注目</span>'
           if p.get("after_close") else "")
        + f' {esc(p["summary"] or "")}</div>'
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

    # ── 8. AIファンド ──
    sec_aifund = ""
    if aifund and aifund["nav"]:
        af_head = (f'総資産 <b>{aifund["nav"]/1e4:,.0f}万円</b>（{_chg_html(aifund["pnl_pct"])}）'
                   + (f'　対TOPIX {_chg_html(aifund["vs_topix"])}' if aifund["vs_topix"] is not None else ""))
        fills_html = ""
        for side, code, name, shares, price, pnl_pct, reason in aifund["fills"]:
            act = "🟢買" if side == "buy" else "🔴売"
            pnlp = f'（{_chg_html(float(pnl_pct))}）' if side == "sell" and pnl_pct is not None else ""
            fills_html += (f'<div style="font-size:12.5px;margin:3px 0">{act} <a href="/stock/{code}">{esc(name)}</a> '
                           f'{shares}株 @{float(price):,.0f}円{pnlp}<br>'
                           f'<span class="mut" style="font-size:11.5px">{esc((reason or "")[:80])}</span></div>')
        pend_html = ""
        if aifund["pending"]:
            names = "、".join(f'{"買" if s == "buy" else "売"}:{esc(n)}' for s, c, n, _r in aifund["pending"][:8])
            pend_html = f'<div style="font-size:12px;margin-top:6px"><span class="mut">明日の予定:</span> {names}</div>'
        bw_html = ""
        if aifund["best"]:
            b, w = aifund["best"], aifund["worst"]
            bw_html = (f'<div style="font-size:12px;margin-top:4px"><span class="mut">保有ベスト:</span> {esc(b[1])} {_chg_html(b[2])}'
                       f'　<span class="mut">ワースト:</span> {esc(w[1])} {_chg_html(w[2])}</div>')
        sec_aifund = f"""<div class="rp-card">
  <div class="rp-h">🤖 AIファンド <small>（<a href="/aifund">詳細</a>）</small></div>
  <div style="font-size:13px;margin-bottom:4px">{af_head}</div>
  {fills_html or '<div class="mut" style="font-size:12px">本日の約定はありません</div>'}
  {bw_html}
  {pend_html}
</div>"""

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
  {sec_numbers}
  {sec_summary}
  {sec_youtube}
  {sec_flows}
  {sec_movers}
  {sec_revisions}
  {sec_triggers}
  {sec_discs}
  {sec_aifund}
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


def _report_base_url() -> str:
    """レポート全文（/daily）の公開ベースURL。環境変数優先・末尾スラッシュは正規化。"""
    return (os.environ.get("REPORT_BASE_URL", "").strip() or DEFAULT_REPORT_BASE_URL).rstrip("/")


def notify_report_ready(report_date: date) -> bool:
    """確定した日次レポートを LINE に通知する（リンクのみの最小通知）。

    イブニング便（確定版）でのみ呼ぶ想定。LINE 未設定なら送信をスキップする。
    """
    from line_notify import is_configured, push_text
    if not is_configured():
        print("  [日次レポートLINE] LINE 未設定のためスキップ")
        return False
    wd = "月火水木金土日"[report_date.weekday()]
    base = _report_base_url()
    lines = ["📰 本日の日次レポートが完成しました",
             f"{report_date.strftime('%Y/%m/%d')}（{wd}）"]
    if base:
        lines += ["", f"▶ 全文を読む\n{base}/daily"]
    return push_text("\n".join(lines), label="日次レポートLINE")


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
        saved = save_report(d)
        if "--notify" in args and saved:
            notify_report_ready(saved)
    elif "--notify" in args:
        # 保存済みの最新レポート日付でLINE通知だけ送る（送信テスト用）
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT MAX(report_date) FROM daily_reports"
                    if not d else "SELECT MAX(report_date) FROM daily_reports WHERE report_date <= %s",
                    () if not d else (d,))
        rd = cur.fetchone()[0]
        cur.close(); conn.close()
        if rd:
            notify_report_ready(rd)
        else:
            print("通知対象のレポートがありません（先に --save してください）")
    else:
        print(build_report_html(d))
