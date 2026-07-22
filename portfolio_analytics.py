"""ポートフォリオのリスク・ヘルスチェック分析（純粋ロジック層）。

app.py（インターフェース層）から呼ぶ。DBアクセスもHTML生成もしない。
記事(okikusan氏 Skills)の「ロジックとインターフェースを分離」に倣い、
ここは入力データ→分析結果(dict/list) の純関数だけを置く（テスト容易）。

- concentration : 集中度(HHI)・上位銘柄・テーマ/業種エクスポージャー
- health_check  : 2軸(テクニカル×ファンダ)の保有ヘルスチェック
- risk_metrics  : 年率ボラ・ヒストリカルVaR・最大DD・β(vs TOPIX)
- stress_test   : 現保有構成での過去最悪局面の再現＋β連動の市場ショック仮説
"""
from __future__ import annotations

import math
import statistics

# 主テーマ選定で除外する汎用ラベル（業種名・指数・属性系はエクスポージャーの主軸にしない）
GENERIC_THEMES = {
    "電気機器", "輸送用機器", "情報・通信業", "サービス業", "機械", "化学", "食料品",
    "小売業", "卸売業", "銀行業", "医薬品", "建設業", "不動産業", "陸運業", "証券業",
    "国際優良株", "輸出", "内需", "JPX日経400", "日経平均株価", "TOPIX Core30",
    "TOPIX100", "シャリア指数", "MSCI", "value", "大型株", "中型株", "小型株",
}


# ─── 集中度・エクスポージャー ────────────────────────────────────────────
def concentration(holdings: list[dict], theme_primary: dict, sector_map: dict) -> dict:
    """holdings: [{code,name,asset_class,sbi_value}]
    theme_primary: code -> 主テーマ名 / sector_map: code -> 業種名
    """
    total = sum(h["sbi_value"] or 0 for h in holdings) or 1
    stock_value = sum(h["sbi_value"] or 0 for h in holdings if h["asset_class"] == "stock")
    fund_value = total - stock_value

    # 銘柄単位（口座横断集計）
    bycode: dict[str, dict] = {}
    for h in holdings:
        c = bycode.setdefault(h["code"], {"name": h["name"], "value": 0, "asset": h["asset_class"]})
        c["value"] += h["sbi_value"] or 0
    hhi = sum((c["value"] / total) ** 2 for c in bycode.values()) * 10000
    top = sorted(bycode.values(), key=lambda x: x["value"], reverse=True)

    theme_alloc: dict[str, int] = {}
    sector_alloc: dict[str, int] = {}
    for code, c in bycode.items():
        if c["asset"] != "stock":
            continue
        th = theme_primary.get(code) or "未分類"
        theme_alloc[th] = theme_alloc.get(th, 0) + c["value"]
        sec = sector_map.get(code) or "その他"
        sector_alloc[sec] = sector_alloc.get(sec, 0) + c["value"]

    return {
        "total": total, "stock_value": stock_value, "fund_value": fund_value,
        "hhi": hhi, "hhi_label": _hhi_label(hhi), "top": top, "n_names": len(bycode),
        "theme_alloc": sorted(theme_alloc.items(), key=lambda x: -x[1]),
        "sector_alloc": sorted(sector_alloc.items(), key=lambda x: -x[1]),
    }


def _hhi_label(hhi: float) -> str:
    if hhi >= 2500:
        return "高集中"
    if hhi >= 1500:
        return "やや集中"
    return "分散良好"


# ─── 2軸ヘルスチェック（テクニカル×ファンダ） ────────────────────────────
def health_check(holdings: list[dict], stats_map: dict) -> list[dict]:
    """stats_map: code -> {close,ma50,ma200_slope,rsi14,gc_75_200,dev_ma25,fscore}

    撤退＝テクニカル崩壊 AND ファンダ悪化 の両立を要求（目先のブレで振り落とされない）。
    """
    bycode: dict[str, dict] = {}
    for h in holdings:
        if h["asset_class"] != "stock":
            continue
        c = bycode.setdefault(h["code"], {"name": h["name"], "value": 0})
        c["value"] += h["sbi_value"] or 0

    out = []
    for code, info in bycode.items():
        s = stats_map.get(code)
        if not s:
            continue
        close, ma50, slope = s.get("close"), s.get("ma50"), s.get("ma200_slope")
        rsi, gc, dev25, fscore = s.get("rsi14"), s.get("gc_75_200"), s.get("dev_ma25"), s.get("fscore")

        reasons = []
        below50 = close is not None and ma50 is not None and close < ma50
        deadcross = gc == 0
        downtrend = slope is not None and slope < 0
        rsi_weak = rsi is not None and rsi < 40
        far_below25 = dev25 is not None and dev25 < -3
        if below50:
            reasons.append("50日線割れ")
        if deadcross:
            reasons.append("デッドクロス(75<200)")
        if downtrend:
            reasons.append("200日線が下向き")
        if rsi_weak:
            reasons.append(f"RSI {rsi:.0f}")

        fund_weak = fscore is not None and fscore <= 3
        fund_strong = fscore is not None and fscore >= 7
        if fund_weak:
            reasons.append(f"F-score {int(fscore)}（低い）")

        tech_broken = below50 and (deadcross or downtrend)
        tech_warn = below50 or rsi_weak or far_below25

        if tech_broken and fund_weak:
            verdict, sev = "撤退検討", 3
        elif tech_broken or fund_weak:
            verdict, sev = "注意", 2
        elif tech_warn:
            verdict, sev = "早期警告", 1
        else:
            verdict, sev = "良好", 0
            if fund_strong:
                reasons.append(f"F-score {int(fscore)}（良好）")

        out.append({
            "code": code, "name": info["name"], "value": info["value"],
            "verdict": verdict, "severity": sev, "reasons": reasons,
        })
    out.sort(key=lambda x: (-x["severity"], -x["value"]))
    return out


# ─── リスク指標 ──────────────────────────────────────────────────────────
def _percentile(sorted_vals: list[float], p: float):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def risk_metrics(port_returns: list[float], topix_returns: list[float] | None = None) -> dict | None:
    """日次リターン列 → 年率ボラ・VaR(95/99)・最大DD・β。データ不足なら None。"""
    n = len(port_returns)
    if n < 40:
        return None
    mean = statistics.fmean(port_returns)
    sd = statistics.pstdev(port_returns)
    res = {
        "n": n,
        "vol_annual": sd * math.sqrt(252) * 100,
        "var95": -_percentile(sorted(port_returns), 0.05) * 100,
        "var99": -_percentile(sorted(port_returns), 0.01) * 100,
    }
    # 最大ドローダウン（累積）
    cum = peak = 1.0
    mdd = 0.0
    for r in port_returns:
        cum *= (1 + r)
        peak = max(peak, cum)
        mdd = min(mdd, cum / peak - 1)
    res["max_dd"] = mdd * 100
    # β vs TOPIX
    if topix_returns and len(topix_returns) == n:
        tmean = statistics.fmean(topix_returns)
        cov = sum((port_returns[i] - mean) * (topix_returns[i] - tmean) for i in range(n)) / n
        tvar = statistics.pvariance(topix_returns)
        res["beta"] = (cov / tvar) if tvar else None
    return res


# ─── ストレステスト ──────────────────────────────────────────────────────
def stress_test(calendar: list, port_returns: list[float], beta: float | None) -> list[dict]:
    """現保有構成を過去に当てはめた最悪局面 ＋ β連動の市場ショック仮説。"""
    scen: list[dict] = []
    if port_returns:
        i = min(range(len(port_returns)), key=lambda k: port_returns[k])
        scen.append({"name": "最悪の1日", "pct": port_returns[i] * 100,
                     "detail": f"{calendar[i]} 相当", "kind": "hist"})
        for w, label in ((5, "最悪の1週間"), (20, "最悪の1ヶ月")):
            if len(port_returns) >= w:
                worst, wi = None, 0
                for j in range(len(port_returns) - w + 1):
                    ssum = sum(port_returns[j:j + w])
                    if worst is None or ssum < worst:
                        worst, wi = ssum, j
                scen.append({"name": label, "pct": worst * 100,
                             "detail": f"{calendar[wi]}〜{calendar[wi + w - 1]}", "kind": "hist"})
    if beta is not None:
        for shock, lbl in ((-10, "TOPIX −10%"), (-20, "リーマン級 −20%")):
            scen.append({"name": lbl, "pct": beta * shock,
                         "detail": f"β={beta:.2f} 連動推定", "kind": "shock"})
    return scen


def trade_summary(trades: list[dict]) -> dict:
    """約定履歴 → 実現損益(概算・移動平均法)・投資活動サマリー・初回購入日。

    trades: [{trade_date, code, name, side('buy'/'sell'), account_type,
              asset_class, qty, price, settle_amount}]
    実現損益は (code, 口座) 単位で移動平均取得原価を追跡して算出（SBIの総平均に整合）。
    履歴窓の外で買った玉を売った銘柄（保有株数を超える売り）は正確に出せないため
    incomplete に入れ、実現損益合計からは除外する（数値の正確性を優先）。
    """
    ts = sorted(trades, key=lambda t: (t["trade_date"], t.get("id", 0)))
    pos: dict[tuple, dict] = {}
    realized_by_code: dict[str, float] = {}
    incomplete: set[str] = set()
    first_buy: dict[str, object] = {}
    n_buys = n_sells = 0
    buy_amount = sell_amount = 0
    fund_invested = 0

    for t in ts:
        code = t.get("code")
        side = t.get("side")
        amt = t.get("settle_amount") or 0
        q = float(t.get("qty") or 0)
        p = float(t.get("price") or 0)

        if t.get("asset_class") != "stock" or not code:
            if side == "buy":
                fund_invested += amt
            continue

        if side == "buy":
            n_buys += 1
            buy_amount += amt or int(q * p)
            st = pos.setdefault((code, t.get("account_type")), {"qty": 0.0, "cost": 0.0})
            st["qty"] += q
            st["cost"] += q * p
            d = t["trade_date"]
            if code not in first_buy or d < first_buy[code]:
                first_buy[code] = d
        elif side == "sell":
            n_sells += 1
            sell_amount += amt or int(q * p)
            st = pos.get((code, t.get("account_type")))
            if not st or st["qty"] < q - 1e-6:
                incomplete.add(code)          # 窓外の玉を含む売り → 正確な原価不明
                if st and st["qty"] > 0:
                    avg = st["cost"] / st["qty"]
                    realized_by_code[code] = realized_by_code.get(code, 0.0) + (p - avg) * st["qty"]
                    st["qty"] = st["cost"] = 0.0
                continue
            avg = st["cost"] / st["qty"] if st["qty"] > 0 else p
            realized_by_code[code] = realized_by_code.get(code, 0.0) + (p - avg) * q
            st["qty"] -= q
            st["cost"] -= avg * q

    realized_total = sum(v for c, v in realized_by_code.items() if c not in incomplete)
    return {
        "realized_total": int(round(realized_total)),
        "realized_by_code": sorted(
            ((c, int(round(v))) for c, v in realized_by_code.items() if c not in incomplete),
            key=lambda x: -x[1]),
        "incomplete": incomplete,
        "first_buy": first_buy,
        "n_buys": n_buys, "n_sells": n_sells,
        "buy_amount": buy_amount, "sell_amount": sell_amount,
        "fund_invested": fund_invested,
    }


def build_port_returns(series: dict, calendar: list, weights: dict) -> list[float]:
    """series: code -> {date: 日次リターン}, calendar: 基準日リスト, weights: code->比率。
    各基準日の加重ポートリターン列を返す（欠損日はその銘柄0%扱い）。
    """
    out = []
    for d in calendar:
        out.append(sum(w * series.get(code, {}).get(d, 0.0) for code, w in weights.items()))
    return out
