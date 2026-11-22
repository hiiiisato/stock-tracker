"""価格・1株指標・配当を同じaction digestで結合する現在値の唯一読取表。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

from batch_dates import jst_today
from config import bulk_upsert, db


CALCULATION_VERSION = "1"
BATCH_CODES = 250


def _decimal(value) -> Decimal | None:
    try:
        result = Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None
    return result if result is None or result.is_finite() else None


def _ratio(numerator, denominator, multiplier=Decimal(1)) -> Decimal | None:
    n, d = _decimal(numerator), _decimal(denominator)
    return n / d * multiplier if n is not None and d is not None and d > 0 else None


def ensure_schema(cur) -> None:
    cur.execute("""CREATE TABLE IF NOT EXISTS valuation_metrics_current (
        code VARCHAR(10) PRIMARY KEY, price_date DATE NOT NULL,
        target_basis_date DATE NOT NULL, price_version CHAR(64) NOT NULL,
        action_digest CHAR(64) NOT NULL, price DECIMAL(18,4) NOT NULL,
        eps_latest_annual DECIMAL(28,10) NULL, eps_ttm DECIMAL(28,10) NULL,
        eps_company_forecast DECIMAL(28,10) NULL, bps_latest DECIMAL(28,10) NULL,
        dps_last_fiscal_year DECIMAL(20,8) NULL,
        dps_company_forecast DECIMAL(20,8) NULL,
        shares_outstanding DECIMAL(30,6) NULL, shares_basis_date DATE NULL,
        shares_source_observed_at DATETIME NULL, shares_basis_status VARCHAR(24) NULL,
        per_latest_annual DECIMAL(20,8) NULL, per_ttm DECIMAL(20,8) NULL,
        per_forecast DECIMAL(20,8) NULL, pbr DECIMAL(20,8) NULL,
        div_yield DECIMAL(20,8) NULL, market_cap DECIMAL(30,2) NULL,
        eps_fact_id CHAR(64) NULL, bps_fact_id CHAR(64) NULL,
        forecast_metric_id BIGINT NULL, dividend_source_ref VARCHAR(255) NULL,
        quality_status VARCHAR(20) NOT NULL, quality_reason VARCHAR(1024) NULL,
        calculation_version VARCHAR(16) NOT NULL,
        calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_valuation_quality (quality_status,price_date)
    )""")


def _where_codes(requested: list[str], prefix: str = "") -> tuple[str, list[str]]:
    if not requested:
        return "", []
    return f" AND {prefix}code IN (" + ",".join(["%s"] * len(requested)) + ")", requested


def rebuild_valuation_metrics(codes: Iterable[str] | None = None, *, basis_date: date | None = None) -> int:
    target = basis_date or jst_today()
    if codes is None:
        with db() as cur:
            ensure_schema(cur)
            cur.execute("""SELECT code FROM price_adjustment_state
                           WHERE status IN ('published','audit_due')
                             AND action_digest IS NOT NULL
                             AND target_basis_date IS NOT NULL
                           ORDER BY code""")
            all_codes = [row[0] for row in cur.fetchall()]
        return sum(rebuild_valuation_metrics(
            all_codes[start:start + BATCH_CODES], basis_date=target
        ) for start in range(0, len(all_codes), BATCH_CODES))
    requested = sorted({str(c).strip().upper() for c in (codes or []) if str(c).strip()})
    if len(requested) > BATCH_CODES:
        return sum(rebuild_valuation_metrics(
            requested[start:start + BATCH_CODES], basis_date=target
        ) for start in range(0, len(requested), BATCH_CODES))
    with db() as cur:
        ensure_schema(cur)
        condition, params = _where_codes(requested, "s.")
        cur.execute("""SELECT s.code,s.price_date,s.target_basis_date,s.published_version,
                              s.action_digest,p.close,sf.shares_outstanding,sf.updated_at
                       FROM price_adjustment_state s
                       JOIN price_history p ON p.code=s.code AND p.date=s.price_date
                         AND p.price_version=s.published_version
                       LEFT JOIN stock_fundamentals sf ON sf.code=s.code
                       WHERE s.status IN ('published','audit_due')
                         AND s.action_digest IS NOT NULL AND s.target_basis_date IS NOT NULL"""
                    + condition, params)
        bases = list(cur.fetchall())
        if not bases:
            return 0
        target_codes = sorted({r[0] for r in bases})
        ph = ",".join(["%s"] * len(target_codes))

        cur.execute(f"""WITH ranked AS (
            SELECT f.code,f.metric,f.period_end,f.fact_id,p.normalized_value,
                   ROW_NUMBER() OVER (PARTITION BY f.code,f.metric
                     ORDER BY f.period_end DESC,f.announced_at DESC,f.fact_id DESC) rn
            FROM per_share_fact_versions f
            JOIN per_share_fact_projections p ON p.fact_id=f.fact_id
            JOIN price_adjustment_state s ON s.code=f.code
             AND s.action_digest=p.action_digest AND s.target_basis_date=p.target_basis_date
            WHERE f.period_type='annual' AND p.quality_status='verified'
              AND f.period_end<=%s AND f.code IN ({ph})
        ) SELECT code,metric,fact_id,normalized_value FROM ranked WHERE rn=1""",
                    [target, *target_codes])
        actual: dict[tuple[str, str], tuple] = {(c, m): (fid, value) for c, m, fid, value in cur.fetchall()}

        cur.execute(f"""WITH ranked AS (
            SELECT fm.code,fm.forecast_id,fm.forecast_eps,fm.forecast_roe,
                   fm.implied_average_shares,
                   ROW_NUMBER() OVER (PARTITION BY fm.code
                     ORDER BY fm.announced_at DESC,fm.fiscal_year_end ASC,fm.forecast_id DESC) rn
            FROM forecast_metrics fm
            JOIN price_adjustment_state s ON s.code=fm.code
              AND s.action_digest=fm.action_digest
            WHERE fm.quality_status='verified' AND fm.is_scoring_eligible=TRUE
              AND fm.announced_at<=%s AND fm.fiscal_year_end>=%s AND fm.code IN ({ph})
        ) SELECT code,forecast_id,forecast_eps,forecast_roe,implied_average_shares
          FROM ranked WHERE rn=1""", [target, target, *target_codes])
        forecasts = {r[0]: r[1:] for r in cur.fetchall()}

        cur.execute(f"""WITH ranked AS (
            SELECT d.code,d.fiscal_year_end,d.normalized_dps,d.component_event_ids,
                   CASE WHEN d.fiscal_year_end>=%s THEN 'forecast' ELSE 'actual' END kind,
                   ROW_NUMBER() OVER (PARTITION BY d.code,
                     CASE WHEN d.fiscal_year_end>=%s THEN 1 ELSE 0 END
                     ORDER BY CASE WHEN d.fiscal_year_end>=%s THEN d.fiscal_year_end END ASC,
                              CASE WHEN d.fiscal_year_end<%s THEN d.fiscal_year_end END DESC) rn
            FROM dividend_annual_normalized d
            JOIN price_adjustment_state s ON s.code=d.code
             AND s.action_digest=d.action_digest AND s.target_basis_date=d.target_basis_date
            WHERE d.quality_status='verified' AND d.code IN ({ph})
        ) SELECT code,kind,normalized_dps,component_event_ids FROM ranked WHERE rn=1""",
                    [target, target, target, target, *target_codes])
        dividends = {(c, kind): (value, refs) for c, kind, value, refs in cur.fetchall()}

        rows = []
        compatibility = []
        state_rows = []
        now = datetime.now()
        for code, price_date, target_basis, price_version, digest, price, shares, shares_observed in bases:
            eps_fact, eps = actual.get((code, "eps"), (None, None))
            bps_fact, bps = actual.get((code, "bps"), (None, None))
            forecast_id, forecast_eps, _forecast_roe, implied_shares = forecasts.get(code, (None, None, None, None))
            dps_actual, _actual_refs = dividends.get((code, "actual"), (None, None))
            dps_forecast, forecast_refs = dividends.get((code, "forecast"), (None, None))
            price_d = _decimal(price)
            shares_d = _decimal(shares)
            implied_d = _decimal(implied_shares)
            share_gap = (abs(shares_d - implied_d) / abs(implied_d)
                         if shares_d is not None and implied_d not in (None, Decimal(0)) else None)
            shares_verified = shares_d is not None and shares_d > 0 and share_gap is not None and share_gap <= Decimal("0.05")
            market_cap = price_d * shares_d if shares_verified else None
            per_actual = _ratio(price_d, eps)
            per_forecast = _ratio(price_d, forecast_eps)
            pbr = _ratio(price_d, bps)
            div_yield = _ratio(dps_forecast, price_d, Decimal(100))
            reasons = []
            if eps is None:
                reasons.append("missing_verified_eps")
            if bps is None:
                reasons.append("missing_verified_bps")
            if forecast_eps is None:
                reasons.append("missing_verified_forecast_eps")
            if dps_forecast is None:
                reasons.append("missing_verified_forecast_dps")
            if not shares_verified:
                reasons.append("shares_basis_unverified")
            quality = "verified" if not reasons else "partial"
            rows.append((
                code,price_date,target_basis,price_version,digest,price_d,
                _decimal(eps),None,_decimal(forecast_eps),_decimal(bps),
                _decimal(dps_actual),_decimal(dps_forecast),shares_d,
                target_basis if shares_verified else None,shares_observed,
                "verified_implied_shares" if shares_verified else "needs_review",
                per_actual,None,per_forecast,pbr,div_yield,market_cap,
                eps_fact,bps_fact,forecast_id,str(forecast_refs)[:255] if forecast_refs else None,
                quality,",".join(reasons) or None,CALCULATION_VERSION,now,
            ))
            compatibility.append((market_cap,per_actual,pbr,div_yield,code))
            state_rows.append((code,"valuation",digest,digest,"complete",None,None,now))
        columns = ["code","price_date","target_basis_date","price_version","action_digest","price",
                   "eps_latest_annual","eps_ttm","eps_company_forecast","bps_latest",
                   "dps_last_fiscal_year","dps_company_forecast","shares_outstanding",
                   "shares_basis_date","shares_source_observed_at","shares_basis_status",
                   "per_latest_annual","per_ttm","per_forecast","pbr","div_yield","market_cap",
                   "eps_fact_id","bps_fact_id","forecast_metric_id","dividend_source_ref",
                   "quality_status","quality_reason","calculation_version","calculated_at"]
        bulk_upsert(cur,"valuation_metrics_current",columns,rows,update_cols=columns[1:])
        # 移行期間の旧画面も、不整合なYahoo/旧予想DPSを読まないよう同値を投影する。
        cur.executemany("""UPDATE stock_fundamentals
                           SET market_cap=%s,per=%s,pbr=%s,div_yield=%s
                           WHERE code=%s""", compatibility)
        bulk_upsert(cur,"corporate_action_projection_state",
                    ["code","consumer","required_digest","completed_digest","status",
                     "not_before","last_error","updated_at"],state_rows)
        return len(rows)


def main() -> None:
    import sys
    print(f"[バリュエーション] {rebuild_valuation_metrics(sys.argv[1:] or None):,}銘柄")


if __name__ == "__main__":
    main()
