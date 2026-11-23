"""スクリーニング用の完成済みスナップショットを生成・参照する。

現在画面とバックテストは、このモジュールで同じ行形式へ変換したデータだけを使う。
バッチは全銘柄を計算し終えてから ``ready`` にするため、途中結果は公開されない。
"""

from __future__ import annotations

import hashlib
import json
import zlib
from datetime import date
from typing import Any, Iterable, Sequence

from config import get_conn


HIGH_DIVIDEND_VALUE_CONDITIONS = (
    ("div_yield", ">=", 3),
    ("pbr", "<=", 1.5),
    ("roe", ">=", 10),
    ("cf_positive", "=", 1),
    ("turnover_20d", ">=", 1),
)

# 全プリセットが参照できる、週次スナップショット由来の共通項目。
SCREENING_CORE_FIELDS = {
    "code", "name", "market", "per", "pbr", "roe", "roa", "div_yield",
    "market_cap", "op_margin", "payout_ratio", "debt_to_equity", "beta",
    "close", "change_pct", "chg5d", "chg25d", "chg75d", "ma25", "ma50",
    "ma75", "ma200", "dev_ma25", "dev_ma50", "dev_ma75", "dev_ma200",
    "dev_high52w", "dev_low52w", "vol20_ratio", "vol_ratio_6_25",
    "volatility_60d", "rsi14", "macd", "macd_signal", "macd_gc",
    "turnover_day", "turnover_20d", "break_20d", "break_65d",
    "rev_growth", "op_growth", "eps_growth", "roic", "cf_positive",
    "gc_5_25", "gc_75_200", "bb_upper", "bb_lower", "stoch_k", "stoch_d",
    "ytd_high", "ytd_low", "break_ytd_high", "dev_ytd_high", "dev_ytd_low",
    "nikkei_rel_1m", "equity_ratio", "ord_margin", "ord_growth", "psr",
    "pcfr", "fscore", "vs_ma25", "vs_ma75", "ma25_vs_ma75",
    "earnings_yield", "peg", "fin_leverage",
}

_CURRENT_EXTRA_FIELDS = (
    "change_pct", "theo_ratio", "upside_3y_pct", "theo_pass_all",
    "buyback_recent", "rev_cagr_5y", "profit_cagr_5y", "consec_rev_growth",
    "consec_profit_growth", "consec_div_growth", "roe_5y_avg", "roa_5y_avg",
    "opm_5y_avg", "is_record_profit", "roe_delta_3y", "roe_driver_3y",
    "net_margin", "eps", "bps", "dps",
)


def ensure_schema() -> None:
    """バッチからだけ呼ぶ。Webリクエスト中にはDDLを実行しない。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS screening_snapshots (
            snapshot_id CHAR(64) PRIMARY KEY,
            snapshot_date DATE NOT NULL,
            content_digest CHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL,
            row_count INT NOT NULL,
            quality_json TEXT,
            payload LONGBLOB NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            published_at DATETIME NULL,
            KEY idx_screening_ready (status, snapshot_date, published_at)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def _float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None


def _stock_metadata(cur, include_current_supplement: bool) -> dict[str, dict[str, Any]]:
    if not include_current_supplement:
        cur.execute("SELECT code, name, is_active FROM stocks")
        return {
            code: {"name": name or "", "_is_active": bool(active)}
            for code, name, active in cur.fetchall()
        }

    cur.execute("""
        SELECT s.code, s.name, s.is_active,
               lp.change_pct,
               tv.theo_ratio, tv.upside_3y_pct, tv.pass_all,
               (bb.code IS NOT NULL) AS buyback_recent,
               fm.rev_cagr_5y, fm.profit_cagr_5y,
               fm.consec_rev_growth, fm.consec_profit_growth, fm.consec_div_growth,
               fm.roe_5y_avg, fm.roa_5y_avg, fm.opm_5y_avg,
               fm.is_record_profit, fm.roe_delta_3y, fm.roe_driver_3y,
               f.profit_margin,
               CASE WHEN v.code IS NOT NULL THEN v.eps_latest_annual ELSE f.eps_ttm END,
               CASE WHEN v.code IS NOT NULL THEN v.bps_latest ELSE f.bps END,
               CASE WHEN v.code IS NOT NULL THEN v.dps_company_forecast ELSE f.annual_dps END
        FROM stocks s
        LEFT JOIN fundamental_metrics fm ON s.code=fm.code
        LEFT JOIN stock_fundamentals f ON s.code=f.code
        LEFT JOIN price_adjustment_state vas ON vas.code=s.code
        LEFT JOIN valuation_metrics_current v ON v.code=s.code
          AND vas.status IN ('published','audit_due')
          AND vas.published_version=v.price_version
          AND vas.action_digest=v.action_digest
          AND vas.target_basis_date=v.target_basis_date
        LEFT JOIN (
            SELECT dp.code, dp.change_pct
            FROM price_history dp
            JOIN (SELECT code, MAX(date) AS max_date FROM price_history GROUP BY code) mx
              ON dp.code=mx.code AND dp.date=mx.max_date
        ) lp ON s.code=lp.code
        LEFT JOIN theoretical_values tv ON s.code=tv.code
        LEFT JOIN (
            SELECT DISTINCT code FROM disclosures
            WHERE category='buyback'
              AND (title LIKE '%取得に係る事項の決定%' OR title LIKE '%買付%')
              AND title NOT LIKE '%状況%' AND title NOT LIKE '%終了%'
              AND disclosed_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        ) bb ON bb.code=s.code
    """)
    result: dict[str, dict[str, Any]] = {}
    for r in cur.fetchall():
        result[r[0]] = {
            "name": r[1] or "",
            "_is_active": bool(r[2]),
            "change_pct": _float(r[3]),
            "theo_ratio": _float(r[4]),
            "upside_3y_pct": _float(r[5]),
            "theo_pass_all": _int_or_none(r[6]),
            "buyback_recent": _int_or_none(r[7]),
            "rev_cagr_5y": _float(r[8]),
            "profit_cagr_5y": _float(r[9]),
            "consec_rev_growth": _int_or_none(r[10]),
            "consec_profit_growth": _int_or_none(r[11]),
            "consec_div_growth": _int_or_none(r[12]),
            "roe_5y_avg": _float(r[13]),
            "roa_5y_avg": _float(r[14]),
            "opm_5y_avg": _float(r[15]),
            "is_record_profit": _int_or_none(r[16]),
            "roe_delta_3y": _float(r[17]),
            "roe_driver_3y": _int_or_none(r[18]),
            "net_margin": float(r[19]) * 100 if r[19] is not None else None,
            "eps": _float(r[20]),
            "bps": _float(r[21]),
            "dps": _float(r[22]),
        }
    return result


def serialize_history_rows(
    cur,
    rows: Iterable[Sequence[Any]],
    columns: Sequence[str],
    *,
    include_current_supplement: bool = False,
) -> list[dict[str, Any]]:
    """price_stats_historyの行を現在画面・バックテスト共通形式へ変換する。"""
    idx = {column: i for i, column in enumerate(columns)}
    metadata = _stock_metadata(cur, include_current_supplement)
    results: list[dict[str, Any]] = []

    for row in rows:
        def value(column: str) -> Any:
            return row[idx[column]]

        code = str(value("code"))
        extra = metadata.get(code, {})
        close = _float(value("close"))
        ma25 = _float(value("ma25"))
        ma50 = _float(value("ma50"))
        ma75 = _float(value("ma75"))
        per = _float(value("per"))
        eps_growth = _float(value("eps_growth"))
        equity_ratio = _float(value("equity_ratio"))

        stock = {
            "code": code,
            "name": extra.get("name", ""),
            "market": value("market") or "",
            "per": per,
            "pbr": _float(value("pbr")),
            "roe": _float(value("roe")),
            "div_yield": _float(value("div_yield")),
            "market_cap": int(value("market_cap")) if value("market_cap") is not None else None,
            "op_margin": _float(value("op_margin")),
            "payout_ratio": _float(value("payout_ratio")),
            "roa": _float(value("roa")),
            "debt_to_equity": _float(value("debt_to_equity")),
            "beta": _float(value("beta")),
            "close": close,
            "change_pct": extra.get("change_pct"),
            "chg5d": _float(value("chg5d")),
            "chg25d": _float(value("chg25d")),
            "chg75d": _float(value("chg75d")),
            "ma25": ma25,
            "ma75": ma75,
            "ma200": _float(value("ma200")),
            "dev_ma25": _float(value("dev_ma25")),
            "dev_ma75": _float(value("dev_ma75")),
            "dev_high52w": _float(value("dev_high52w")),
            "vol20_ratio": _float(value("vol20_ratio")),
            "rsi14": _float(value("rsi14")),
            "macd": _float(value("macd")),
            "macd_signal": _float(value("macd_signal")),
            "macd_gc": _int_or_none(value("macd_gc")),
            "turnover_day": _float(value("turnover_day")),
            "turnover_20d": _float(value("turnover_20d")),
            "break_20d": _int_or_none(value("break_20d")),
            "break_65d": _int_or_none(value("break_65d")),
            "rev_growth": _float(value("rev_growth")),
            "op_growth": _float(value("op_growth")),
            "eps_growth": eps_growth,
            "roic": _float(value("roic")),
            "cf_positive": _int_or_none(value("cf_positive")),
            "dev_ma200": _float(value("dev_ma200")),
            "dev_low52w": _float(value("dev_low52w")),
            "vol_ratio_6_25": _float(value("vol_ratio_6_25")),
            "volatility_60d": _float(value("volatility_60d")),
            "gc_5_25": _int_or_none(value("gc_5_25")),
            "gc_75_200": _int_or_none(value("gc_75_200")),
            "bb_upper": _int_or_none(value("bb_upper")),
            "bb_lower": _int_or_none(value("bb_lower")),
            "stoch_k": _float(value("stoch_k")),
            "stoch_d": _float(value("stoch_d")),
            "ytd_high": _float(value("ytd_high")),
            "ytd_low": _float(value("ytd_low")),
            "break_ytd_high": _int_or_none(value("break_ytd_high")),
            "dev_ytd_high": _float(value("dev_ytd_high")),
            "dev_ytd_low": _float(value("dev_ytd_low")),
            "nikkei_rel_1m": _float(value("nikkei_rel_1m")),
            "equity_ratio": equity_ratio,
            "ord_margin": _float(value("ord_margin")),
            "ord_growth": _float(value("ord_growth")),
            "psr": _float(value("psr")),
            "pcfr": _float(value("pcfr")),
            "fscore": _int_or_none(value("fscore")),
            "vs_ma25": close - ma25 if close is not None and ma25 is not None else None,
            "vs_ma75": close - ma75 if close is not None and ma75 is not None else None,
            "ma25_vs_ma75": ma25 - ma75 if ma25 is not None and ma75 is not None else None,
            "earnings_yield": round(100.0 / per, 2) if per and per > 0 else None,
            "peg": round(per / eps_growth, 2) if per and per > 0 and eps_growth and eps_growth > 0 else None,
            "fin_leverage": round(100.0 / equity_ratio, 2) if equity_ratio and equity_ratio > 0 else None,
            "dev_ma50": round((close - ma50) / ma50 * 100, 2) if close and ma50 else None,
            "_is_active": bool(extra.get("_is_active", False)),
        }
        for field in _CURRENT_EXTRA_FIELDS:
            stock[field] = extra.get(field)
        results.append(stock)
    return results


def _quality(stocks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    count = len(stocks)
    return {
        "row_count": count,
        "close_count": sum(s.get("close") is not None for s in stocks),
        "pbr_count": sum(s.get("pbr") is not None for s in stocks),
        "roe_count": sum(s.get("roe") is not None for s in stocks),
        "dividend_count": sum(s.get("div_yield") is not None for s in stocks),
        "cashflow_count": sum(s.get("cf_positive") is not None for s in stocks),
        "high_dividend_value_count": sum(matches_conditions(s) for s in stocks),
    }


def publish_snapshot(cur, snapshot_date: date, stocks: Sequence[dict[str, Any]]) -> str:
    """完成済みpayloadを1行で保存し、途中状態を公開しない。"""
    if not stocks:
        raise ValueError("空のスクリーニングスナップショットは公開しない")
    codes = [str(stock.get("code") or "") for stock in stocks]
    if "" in codes or len(codes) != len(set(codes)):
        raise ValueError("スクリーニングスナップショットのコードが不正")

    quality = _quality(stocks)
    if quality["close_count"] < len(stocks) * 0.9:
        raise ValueError("株価の欠損が10%を超えたため公開中止")

    cur.execute("""
        SELECT snapshot_date, row_count, quality_json FROM screening_snapshots
        WHERE status='ready'
        ORDER BY snapshot_date DESC, published_at DESC LIMIT 1
    """)
    previous = cur.fetchone()
    if previous and snapshot_date >= previous[0] and len(stocks) < previous[1] * 0.8:
        raise ValueError("前回の正常版よりコード数が20%以上減ったため公開中止")
    if previous and snapshot_date >= previous[0] and previous[2]:
        previous_quality = json.loads(previous[2])
        for field in ("pbr_count", "roe_count", "dividend_count", "cashflow_count"):
            old_count = int(previous_quality.get(field) or 0)
            if old_count >= 100 and quality[field] < old_count * 0.8:
                raise ValueError(f"{field}の有効データが20%以上減ったため公開中止")

    ordered_stocks = sorted(stocks, key=lambda item: str(item["code"]))
    payload_json = json.dumps(
        ordered_stocks, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    content_digest = hashlib.sha256(payload_json).hexdigest()
    snapshot_id = hashlib.sha256(f"{snapshot_date}:{content_digest}".encode()).hexdigest()
    quality_json = json.dumps(quality, ensure_ascii=False, sort_keys=True)
    compressed_payload = zlib.compress(payload_json, level=9)

    cur.execute("""
        INSERT INTO screening_snapshots
            (snapshot_id, snapshot_date, content_digest, status, row_count,
             quality_json, payload, published_at)
        VALUES (%s, %s, %s, 'ready', %s, %s, %s, CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE status='ready', row_count=VALUES(row_count),
                                quality_json=VALUES(quality_json), payload=VALUES(payload),
                                published_at=CURRENT_TIMESTAMP
    """, (snapshot_id, snapshot_date, content_digest, len(stocks), quality_json,
          compressed_payload))
    return snapshot_id


def _is_missing_table(error: Exception) -> bool:
    return bool(getattr(error, "args", ())) and error.args[0] == 1146


def _ready_runs(cur) -> list[tuple[str, date]]:
    try:
        cur.execute("""
            SELECT snapshot_id, snapshot_date
            FROM screening_snapshots
            WHERE status='ready'
            ORDER BY snapshot_date, published_at
        """)
        return [(r[0], r[1]) for r in cur.fetchall()]
    except Exception as error:
        if _is_missing_table(error):
            return []
        raise


def available_snapshot_dates(cur) -> list[date]:
    ready = _ready_runs(cur)
    ready_dates = {r[1] for r in ready}
    try:
        cur.execute("SELECT DISTINCT snapshot_date FROM price_stats_history_current ORDER BY snapshot_date")
        history_dates = {r[0] for r in cur.fetchall()}
    except Exception as error:
        if _is_missing_table(error):
            history_dates = set()
        else:
            raise
    if not ready_dates:
        return sorted(history_dates)
    # 導入前の履歴は互換読取。導入後にreadyになっていない新しい日は公開しない。
    first_ready = min(ready_dates)
    return sorted(ready_dates | {d for d in history_dates if d < first_ready})


def _load_ready(cur, snapshot_date: date) -> tuple[str, list[dict[str, Any]]] | None:
    try:
        cur.execute("""
            SELECT snapshot_id, payload FROM screening_snapshots
            WHERE status='ready' AND snapshot_date=%s
            ORDER BY published_at DESC LIMIT 1
        """, (snapshot_date,))
        row = cur.fetchone()
        if not row:
            return None
        snapshot_id, payload = row
        return snapshot_id, json.loads(zlib.decompress(payload))
    except Exception as error:
        if _is_missing_table(error):
            return None
        raise


def _load_history(cur, snapshot_date: date, include_current_supplement: bool) -> list[dict[str, Any]]:
    from compute_stats_history import HISTORY_COLS

    column_sql = ", ".join("h.`" + column + "`" for column in HISTORY_COLS)
    cur.execute(f"""
        SELECT {column_sql}
        FROM price_stats_history_current h
        WHERE h.snapshot_date=%s
    """, (snapshot_date,))
    return serialize_history_rows(
        cur,
        cur.fetchall(),
        HISTORY_COLS,
        include_current_supplement=include_current_supplement,
    )


def load_snapshot(
    cur,
    requested_date: date | None = None,
    *,
    current: bool = False,
) -> tuple[date | None, list[dict[str, Any]], str | None]:
    dates = available_snapshot_dates(cur)
    if not dates:
        return None, [], None
    if current or requested_date is None:
        snapshot_date = dates[-1]
    else:
        snapshot_date = min(dates, key=lambda d: (abs((d - requested_date).days), -d.toordinal()))

    ready = _load_ready(cur, snapshot_date)
    if ready:
        snapshot_id, stocks = ready
    else:
        snapshot_id = None
        stocks = _load_history(cur, snapshot_date, include_current_supplement=current)

    if current:
        stocks = [stock for stock in stocks if stock.get("_is_active")]
    for stock in stocks:
        stock.pop("_is_active", None)
    stocks.sort(key=lambda stock: (stock.get("market_cap") is not None, stock.get("market_cap") or 0), reverse=True)
    return snapshot_date, stocks, snapshot_id


def matches_conditions(stock: dict[str, Any], conditions=HIGH_DIVIDEND_VALUE_CONDITIONS) -> bool:
    """UIと同じNULL除外ルールで条件を判定する。検証・運用チェック用。"""
    for field, operator, expected in conditions:
        actual = stock.get(field)
        if actual is None:
            return False
        if operator == ">=" and not actual >= expected:
            return False
        if operator == "<=" and not actual <= expected:
            return False
        if operator == "=" and not actual == expected:
            return False
    return True
