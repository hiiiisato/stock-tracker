"""
スイングトレード候補のスコアリング

採点基準（合計最大100点）:
  +30: Weinstein Stage 2（close > MA50 > MA200 かつ MA200 が20日前より上昇）
  +25: RS ≥ 1.3 かつ 全銘柄中 RS 上位30%（対日経225 6ヶ月リターン比）
  +20: 当日出来高 ≥ 20日平均の1.5倍（出来高サージ）
  +15: RSI14 が 45〜65（良好ゾーン）
  -10: RSI14 > 70（過熱ペナルティ）
  +10: 52週高値の5%以内（高値圏 or VCP）

MIN_SCORE = 55 以上を候補と判定

単体実行: python3 swing_scorer.py
"""

from datetime import date, timedelta
from config import get_conn

MIN_SCORE        = 70
RS_THRESHOLD     = 1.3
VOL_THRESHOLD    = 1.5
HIGH52W_THRESH   = -5.0   # 52週高値から -5% 以内
RSI_LOWER        = 45
RSI_UPPER        = 65
RSI_HOT          = 70
RS_TOP_PCT       = 0.30   # RS 上位30% のカットオフ


def _nikkei_6m_return() -> float | None:
    """日経225 の約6ヶ月リターン(%)を返す。データ不足時は None。"""
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute(
            "SELECT close FROM market_index_prices WHERE symbol=%s ORDER BY date DESC LIMIT 1",
            ("^N225",)
        )
        row = cur.fetchone()
        if not row:
            return None
        nk_latest = float(row[0])

        date_6m = (date.today() - timedelta(days=180)).strftime("%Y-%m-%d")
        cur.execute(
            "SELECT close FROM market_index_prices WHERE symbol=%s AND date<=%s ORDER BY date DESC LIMIT 1",
            ("^N225", date_6m)
        )
        row = cur.fetchone()
        if not row:
            return None
        return (nk_latest / float(row[0]) - 1) * 100
    finally:
        cur.close()
        conn.close()


def _fetch_stocks() -> list[dict]:
    """price_stats + 銘柄名 を一括取得する。"""
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT ps.code, s.name,
                   ps.close, ps.ma50, ps.ma200, ps.ma200_slope,
                   ps.chg126d, ps.vol20_ratio, ps.rsi14, ps.dev_high52w
            FROM price_stats ps
            JOIN stocks s ON s.code = ps.code
            WHERE ps.close IS NOT NULL AND ps.close > 0
              AND s.is_active = 1
              AND (s.market_id IS NULL OR s.market_id != 5)
        """)
        cols = ["code", "name", "close", "ma50", "ma200", "ma200_slope",
                "chg126d", "vol20_ratio", "rsi14", "dev_high52w"]
        return [
            {k: (float(v) if v is not None else None) if k != "code" and k != "name" else v
             for k, v in zip(cols, r)}
            for r in cur.fetchall()
        ]
    finally:
        cur.close()
        conn.close()


def score_all(min_score: int = MIN_SCORE) -> list[dict]:
    """全銘柄をスコアリングし、min_score 以上の候補を降順で返す。"""
    nk_ret = _nikkei_6m_return()
    stocks = _fetch_stocks()

    # RS 計算（日経対比リターン比率）
    for s in stocks:
        chg = s.get("chg126d")
        if chg is not None and nk_ret is not None:
            s["rs"] = (1 + chg / 100) / (1 + nk_ret / 100)
        else:
            s["rs"] = None

    # RS 上位30% のカットオフ値
    rs_vals = sorted([s["rs"] for s in stocks if s["rs"] is not None], reverse=True)
    rs_cutoff = rs_vals[int(len(rs_vals) * RS_TOP_PCT) - 1] if rs_vals else None

    results = []
    for s in stocks:
        score  = 0
        flags  = {}
        close  = s.get("close")
        ma50   = s.get("ma50")
        ma200  = s.get("ma200")
        slope  = s.get("ma200_slope")
        rs     = s.get("rs")
        vol_r  = s.get("vol20_ratio")
        rsi    = s.get("rsi14")
        dev_h  = s.get("dev_high52w")

        # 1. Weinstein Stage 2
        if (close and ma50 and ma200 and slope is not None
                and close > ma50 > ma200 and slope > 0):
            score += 30
            flags["stage2"] = True
        else:
            flags["stage2"] = False

        # 2. RS vs 日経
        if rs is not None and rs >= RS_THRESHOLD and (rs_cutoff is None or rs >= rs_cutoff):
            score += 25
            flags["rs"] = True
        else:
            flags["rs"] = False

        # 3. 出来高サージ
        if vol_r is not None and vol_r >= VOL_THRESHOLD:
            score += 20
            flags["volume"] = True
        else:
            flags["volume"] = False

        # 4. RSI ゾーン
        if rsi is not None:
            if RSI_LOWER <= rsi <= RSI_UPPER:
                score += 15
                flags["rsi"] = "good"
            elif rsi > RSI_HOT:
                score -= 10
                flags["rsi"] = "hot"
            else:
                flags["rsi"] = "low"
        else:
            flags["rsi"] = None

        # 5. 高値圏 or VCP
        if dev_h is not None and dev_h >= HIGH52W_THRESH:
            score += 10
            flags["near_high"] = True
        else:
            flags["near_high"] = False

        s["score"] = score
        s["flags"] = flags

        if score >= min_score:
            results.append(s)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


if __name__ == "__main__":
    nk = _nikkei_6m_return()
    print(f"日経225 6ヶ月リターン: {nk:.1f}%" if nk else "日経データなし")

    candidates = score_all()
    today_str = date.today().strftime("%Y/%m/%d")
    print(f"\n=== スイング候補 [{today_str}] {len(candidates)}銘柄 ===\n")

    for s in candidates:
        f = s["flags"]
        rs    = s.get("rs") or 0
        vol_r = s.get("vol20_ratio") or 0
        rsi_v = s.get("rsi14") or 0
        dev_h = s.get("dev_high52w") or 0

        badges = [
            "Stage2✓" if f["stage2"] else "Stage2✗",
            f"RS✓{rs:.2f}" if f["rs"] else f"RS✗{rs:.2f}",
            f"出来高✓{vol_r:.1f}x" if f["volume"] else f"出来高✗{vol_r:.1f}x",
            f"RSI✓{rsi_v:.0f}" if f["rsi"] == "good" else
            f"RSI熱{rsi_v:.0f}" if f["rsi"] == "hot" else f"RSI{rsi_v:.0f}",
            f"高値圏✓{dev_h:.1f}%" if f["near_high"] else f"高値圏✗{dev_h:.1f}%",
        ]
        name = (s.get("name") or s["code"])[:12]
        print(f"  {s['code']} {name:12s} ¥{s['close']:>8,.0f}  スコア:{s['score']:3d}  {' | '.join(badges)}")

    print()
