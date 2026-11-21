"""会社予想純利益から、予想EPS・予想ROEを一つの固定式で再計算する。

予想値を取得元ごとに切り替えると再現性を失うため、計算式は次の1本だけに固定する。

* 予想EPS = 会社予想純利益 / (直近実績純利益 / 直近実績EPS)
* 予想ROE = 予想EPS / 同じ実績期のBPS

会社予想純利益は ``financials_forecast``、実績純利益とEPSは同じ
``financials_edinet_annual`` 行の実績BPSを使う。canonical自己資本は通貨単位や
連結範囲の品質監査用にだけ残し、ROEの分母には使わない。
必要な入力が欠ける場合は別ソースで補完せず、NULLと不足理由を保存する。

単体実行: ``python forecast_metrics.py``
銘柄限定: ``python forecast_metrics.py 3003 9432``
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence

from batch_dates import jst_today
from config import bulk_upsert, get_conn


CALCULATION_VERSION = "3"
_METRIC_COLUMNS = [
    "forecast_id",
    "code",
    "fiscal_year_end",
    "announced_at",
    "forecast_net_income",
    "actual_period_end",
    "actual_net_income",
    "actual_eps",
    "actual_bps",
    "actual_eps_fact_id",
    "actual_bps_fact_id",
    "action_digest",
    "implied_average_shares",
    "canonical_actual_net_income",
    "actual_total_equity",
    "actual_net_income_gap_ratio",
    "current_shares_outstanding",
    "share_basis_gap_ratio",
    "equity_basis_gap_ratio",
    "has_corporate_action_since_basis",
    "forecast_eps",
    "forecast_roe",
    "is_scoring_eligible",
    "eligibility_reason",
    "quality_status",
    "quality_flags",
    "calculation_version",
    "missing_reason",
]


def ensure_schema(cur) -> None:
    """派生値テーブルを作る。Webリクエストからは呼ばない。"""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_metrics (
            forecast_id             BIGINT NOT NULL PRIMARY KEY,
            code                    VARCHAR(10) NOT NULL,
            fiscal_year_end         DATE NOT NULL,
            announced_at            DATE NOT NULL,
            forecast_net_income     BIGINT,
            actual_period_end       DATE,
            actual_net_income       BIGINT,
            actual_eps              DECIMAL(20,8),
            actual_bps              DECIMAL(20,8),
            actual_eps_fact_id      CHAR(64),
            actual_bps_fact_id      CHAR(64),
            action_digest           CHAR(64),
            implied_average_shares  DECIMAL(28,6),
            canonical_actual_net_income BIGINT,
            actual_total_equity     BIGINT,
            actual_net_income_gap_ratio DECIMAL(28,10),
            current_shares_outstanding BIGINT,
            share_basis_gap_ratio   DECIMAL(28,10),
            equity_basis_gap_ratio  DECIMAL(28,10),
            has_corporate_action_since_basis TINYINT(1) NOT NULL DEFAULT 0,
            forecast_eps            DECIMAL(20,8),
            forecast_roe            DECIMAL(16,10),
            is_scoring_eligible     TINYINT(1) NOT NULL DEFAULT 0,
            eligibility_reason      VARCHAR(64),
            quality_status          VARCHAR(20) NOT NULL DEFAULT 'incomplete',
            quality_flags           VARCHAR(512),
            calculation_version     VARCHAR(16) NOT NULL,
            missing_reason          VARCHAR(255),
            calculated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
                                      ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_forecast_metrics_current
                (code, fiscal_year_end, announced_at)
        )
        """
    )
    # 既存v1テーブルはデータを捨てず、NULL許容またはDEFAULT付きの列だけを足す。
    cur.execute("SHOW COLUMNS FROM forecast_metrics")
    existing = {row[0] for row in cur.fetchall()}
    additions = {
        "actual_bps": "DECIMAL(20,8) NULL",
        "actual_eps_fact_id": "CHAR(64) NULL",
        "actual_bps_fact_id": "CHAR(64) NULL",
        "action_digest": "CHAR(64) NULL",
        "canonical_actual_net_income": "BIGINT NULL",
        "actual_net_income_gap_ratio": "DECIMAL(28,10) NULL",
        "current_shares_outstanding": "BIGINT NULL",
        "share_basis_gap_ratio": "DECIMAL(28,10) NULL",
        "equity_basis_gap_ratio": "DECIMAL(28,10) NULL",
        "has_corporate_action_since_basis": "TINYINT(1) NOT NULL DEFAULT 0",
        "is_scoring_eligible": "TINYINT(1) NOT NULL DEFAULT 0",
        "eligibility_reason": "VARCHAR(64) NULL",
        "quality_status": "VARCHAR(20) NOT NULL DEFAULT 'incomplete'",
        "quality_flags": "VARCHAR(512) NULL",
    }
    for column, definition in additions.items():
        if column not in existing:
            cur.execute(f"ALTER TABLE forecast_metrics ADD COLUMN `{column}` {definition}")
    # v2初回実行時に作成した監査比率列が、異常値そのものを保存できる幅か確認する。
    # 正常値へ丸めず、既存値を保持したまま拡幅する。
    cur.execute("SHOW COLUMNS FROM forecast_metrics")
    column_types = {row[0]: str(row[1]).lower() for row in cur.fetchall()}
    for column in (
        "actual_net_income_gap_ratio",
        "share_basis_gap_ratio",
        "equity_basis_gap_ratio",
    ):
        if column_types.get(column) != "decimal(28,10)":
            cur.execute(
                f"ALTER TABLE forecast_metrics MODIFY COLUMN `{column}` DECIMAL(28,10) NULL"
            )


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def calculate_forecast_metrics(
    forecast_net_income,
    actual_net_income,
    actual_eps,
    actual_bps,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, str | None]:
    """固定式の純粋関数。

    戻り値は ``(予想EPS, 予想ROE, 実績から逆算した平均株式数, 不足理由)``。
    ROEは百分率ではなく0.12=12%の比率で保存する。
    """
    forecast_net = _decimal(forecast_net_income)
    actual_net = _decimal(actual_net_income)
    eps = _decimal(actual_eps)
    bps = _decimal(actual_bps)
    missing: list[str] = []

    if forecast_net is None:
        missing.append("forecast_net_income")

    shares = None
    forecast_eps = None
    if actual_net is None:
        missing.append("actual_net_income")
    if eps is None or eps == 0:
        missing.append("actual_eps")
    if actual_net is not None and eps not in (None, Decimal("0")):
        candidate = actual_net / eps
        if candidate > 0:
            shares = candidate
        else:
            missing.append("implied_average_shares_nonpositive")
    if forecast_net is not None and shares is not None:
        forecast_eps = forecast_net / shares

    forecast_roe = None
    if bps is None:
        missing.append("actual_bps")
    elif bps <= 0:
        missing.append("actual_bps_nonpositive")
    elif forecast_eps is not None:
        forecast_roe = forecast_eps / bps

    reason = ",".join(dict.fromkeys(missing)) or None
    return forecast_eps, forecast_roe, shares, reason


def _chunks(values: Sequence[str], size: int = 500):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _fetch_for_codes(
    cur,
    sql_prefix: str,
    codes: Sequence[str],
    *,
    params: Sequence = (),
) -> list[tuple]:
    rows: list[tuple] = []
    for chunk in _chunks(codes):
        ph = ",".join(["%s"] * len(chunk))
        cur.execute(f"{sql_prefix} AND code IN ({ph})", [*params, *chunk])
        rows.extend(cur.fetchall())
    return rows


def _latest_before(
    rows: Sequence[tuple],
    fiscal_year_end,
    announced_at=None,
) -> tuple | None:
    """予想公表時点で利用可能な、予想決算期より前の最新実績を選ぶ。"""
    selected = None
    for row in rows:
        period_end = row[0]
        if period_end is None:
            continue
        available = announced_at is None or period_end <= announced_at
        if period_end < fiscal_year_end and available:
            selected = row
        elif (
            period_end >= fiscal_year_end
            or (announced_at is not None and period_end > announced_at)
        ):
            break
    return selected


def _relative_gap(left, right) -> Decimal | None:
    left_d, right_d = _decimal(left), _decimal(right)
    if left_d is None or right_d is None or right_d == 0:
        return None
    return abs(left_d - right_d) / abs(right_d)


def assess_quality(
    *,
    eligible: bool,
    forecast_net_income,
    actual_period_end,
    actual_net_income,
    canonical_actual_net_income,
    actual_eps,
    actual_bps,
    forecast_eps,
    forecast_roe,
    share_gap: Decimal | None,
    equity_gap: Decimal | None,
    has_corporate_action: bool,
) -> tuple[str, str | None]:
    """計算値を採点へ渡せるか判定し、機械可読の品質フラグを返す。"""
    flags: list[str] = []
    if forecast_net_income is None:
        flags.append("no_company_guidance")
    if actual_period_end is None:
        flags.append("missing_actual_basis")
    if actual_net_income is None:
        flags.append("missing_actual_net_income")
        if canonical_actual_net_income is not None:
            flags.append("edinet_net_income_missing")
    if actual_eps is None or _decimal(actual_eps) == 0:
        flags.append("missing_actual_eps")
    if actual_bps is None:
        flags.append("missing_actual_bps")
    elif _decimal(actual_bps) <= 0:
        flags.append("nonpositive_bps")
    net_d, eps_d = _decimal(actual_net_income), _decimal(actual_eps)
    if net_d is not None and eps_d not in (None, Decimal("0")) and net_d / eps_d <= 0:
        flags.append("source_inconsistent")
    canonical_net_d = _decimal(canonical_actual_net_income)
    net_source_gap = _relative_gap(net_d, canonical_net_d)
    if (
        net_d is not None
        and canonical_net_d is not None
        and (
            net_d * canonical_net_d < 0
            or (net_source_gap is not None and net_source_gap > Decimal("0.20"))
        )
    ):
        flags.append("actual_net_income_source_mismatch")
    if share_gap is not None and share_gap > Decimal("0.05"):
        flags.append("share_basis_mismatch")
    if has_corporate_action:
        flags.append("corporate_action_since_basis")
    if equity_gap is not None and equity_gap > Decimal("0.20"):
        # v2のROE分母には使わないが、canonical側の通貨・連結範囲異常を監視する。
        flags.append("canonical_equity_mismatch")

    if not eligible:
        status = "not_applicable"
    elif "source_inconsistent" in flags or "actual_net_income_source_mismatch" in flags:
        status = "needs_review"
    elif forecast_eps is None or forecast_roe is None:
        status = "incomplete"
    elif "share_basis_mismatch" in flags or "corporate_action_since_basis" in flags:
        status = "needs_review"
    else:
        status = "verified"
    return status, ",".join(flags) or None


def rebuild_forecast_metrics(
    codes: Iterable[str] | None = None,
    *,
    cur=None,
    as_of: date | None = None,
    rebuild_actuals: bool = True,
) -> int:
    """年次会社予想の派生指標を冪等に再生成する。

    ``cur`` を渡した場合は呼び出し側のトランザクションを使い、commit/closeしない。
    TDnet取込直後は更新銘柄だけ、夜間保守では全銘柄を再計算できる。
    """
    own_connection = cur is None
    conn = None
    if own_connection and rebuild_actuals:
        # 予想EPS/ROEは必ず正規化した実績EPS/BPSのみから計算する。
        from per_share_metrics import rebuild_per_share_metrics
        rebuild_per_share_metrics(codes, basis_date=as_of)
    if own_connection:
        conn = get_conn()
        cur = conn.cursor()
    basis_date = as_of or jst_today()

    try:
        # 外部トランザクション内でDDLを実行すると暗黙commitになり得るため、
        # 呼び出し側は更新開始前にensure_schemaを済ませる。
        if own_connection:
            ensure_schema(cur)
        requested = sorted({str(code).strip().upper() for code in (codes or []) if str(code).strip()})
        forecast_sql = (
            "SELECT id, code, fiscal_year_end, announced_at, net_income "
            "FROM financials_forecast WHERE period_type='A'"
        )
        if requested:
            forecasts = _fetch_for_codes(cur, forecast_sql, requested)
        else:
            cur.execute(forecast_sql)
            forecasts = list(cur.fetchall())
        if not forecasts:
            if own_connection:
                conn.commit()
            return 0

        target_codes = sorted({row[1] for row in forecasts})
        actual_sql = (
            "WITH ranked AS ("
            " SELECT f.period_end,f.code,f.metric,f.numerator_raw,p.normalized_value,"
            " f.fact_id,p.action_digest,"
            " ROW_NUMBER() OVER (PARTITION BY f.code,f.period_end,f.metric "
            " ORDER BY f.announced_at DESC,f.ingested_at DESC,f.fact_id DESC) rn"
            " FROM per_share_fact_versions f"
            " JOIN per_share_fact_projections p ON p.fact_id=f.fact_id"
            " JOIN price_adjustment_state s ON s.code=f.code"
            "  AND s.action_digest=p.action_digest"
            "  AND s.target_basis_date=p.target_basis_date"
            " WHERE f.period_type='annual' AND p.quality_status='verified'"
            ") SELECT period_end,code,"
            " MAX(CASE WHEN metric='eps' THEN numerator_raw END) AS net_income,"
            " MAX(CASE WHEN metric='eps' THEN normalized_value END) AS eps,"
            " MAX(CASE WHEN metric='bps' THEN normalized_value END) AS bps,"
            " MAX(CASE WHEN metric='eps' THEN fact_id END) AS eps_fact_id,"
            " MAX(CASE WHEN metric='bps' THEN fact_id END) AS bps_fact_id,"
            " MAX(action_digest) AS action_digest"
            " FROM ranked WHERE rn=1 GROUP BY period_end,code HAVING 1=1"
        )
        financial_sql = (
            "SELECT period_end, code, net_income, total_equity "
            "FROM financials "
            "WHERE period_type='A' AND period_end IS NOT NULL "
            "AND (net_income IS NOT NULL OR total_equity IS NOT NULL)"
        )
        actual_rows = _fetch_for_codes(cur, actual_sql, target_codes)
        financial_rows = _fetch_for_codes(cur, financial_sql, target_codes)
        fundamental_rows = _fetch_for_codes(
            cur,
            "SELECT code, shares_outstanding FROM stock_fundamentals WHERE 1=1",
            target_codes,
        )
        active_rows = _fetch_for_codes(
            cur,
            "SELECT code FROM stocks WHERE is_active=TRUE",
            target_codes,
        )
        listing_rows = _fetch_for_codes(
            cur,
            "SELECT DISTINCT code FROM stock_listing_history "
            "WHERE exchange='TSE' AND product_type='株式' "
            "AND COALESCE(market_segment,'') NOT LIKE '%PRO%' "
            "AND listing_date<=%s AND (delisting_date IS NULL OR delisting_date>=%s)",
            target_codes,
            params=(basis_date, basis_date),
        )
        actual_by_code: dict[str, list[tuple]] = defaultdict(list)
        for period_end, code, net_income, eps, bps, eps_fact_id, bps_fact_id, digest in actual_rows:
            actual_by_code[code].append((period_end, net_income, eps, bps, eps_fact_id, bps_fact_id, digest))
        for rows in actual_by_code.values():
            rows.sort(key=lambda row: row[0])

        financial_by_key = {
            (code, period_end): (net_income, total_equity)
            for period_end, code, net_income, total_equity in financial_rows
        }
        current_shares = {code: shares for code, shares in fundamental_rows}
        active_codes = {row[0] for row in active_rows}
        listed_common_codes = {row[0] for row in listing_rows}
        output = []
        for forecast_id, code, fye, announced_at, forecast_net in forecasts:
            actual = _latest_before(actual_by_code.get(code, ()), fye, announced_at)
            actual_period_end = actual[0] if actual else None
            actual_net = actual[1] if actual else None
            actual_eps = actual[2] if actual else None
            actual_bps = actual[3] if actual else None
            actual_eps_fact_id = actual[4] if actual else None
            actual_bps_fact_id = actual[5] if actual else None
            action_digest = actual[6] if actual else None
            canonical_actual = financial_by_key.get((code, actual_period_end), (None, None))
            canonical_actual_net = canonical_actual[0]
            actual_equity = canonical_actual[1]
            forecast_eps, forecast_roe, shares, missing = calculate_forecast_metrics(
                forecast_net,
                actual_net,
                actual_eps,
                actual_bps,
            )
            shares_now = current_shares.get(code)
            actual_net_gap = _relative_gap(actual_net, canonical_actual_net)
            share_gap = _relative_gap(shares, shares_now)
            equity_implied = (
                _decimal(shares) * _decimal(actual_bps)
                if shares is not None and actual_bps is not None
                else None
            )
            equity_gap = _relative_gap(actual_equity, equity_implied)
            # 権利後でも同一digestへ検証済み投影できていれば正常。
            has_action = False
            eligible = code in active_codes and code in listed_common_codes
            eligibility_reason = None
            if code not in active_codes:
                eligibility_reason = "inactive"
            elif code not in listed_common_codes:
                eligibility_reason = "not_domestic_common_stock"
            quality_status, quality_flags = assess_quality(
                eligible=eligible,
                forecast_net_income=forecast_net,
                actual_period_end=actual_period_end,
                actual_net_income=actual_net,
                canonical_actual_net_income=canonical_actual_net,
                actual_eps=actual_eps,
                actual_bps=actual_bps,
                forecast_eps=forecast_eps,
                forecast_roe=forecast_roe,
                share_gap=share_gap,
                equity_gap=equity_gap,
                has_corporate_action=has_action,
            )
            output.append(
                (
                    forecast_id,
                    code,
                    fye,
                    announced_at,
                    forecast_net,
                    actual_period_end,
                    actual_net,
                    actual_eps,
                    actual_bps,
                    actual_eps_fact_id,
                    actual_bps_fact_id,
                    action_digest,
                    shares,
                    canonical_actual_net,
                    actual_equity,
                    actual_net_gap,
                    shares_now,
                    share_gap,
                    equity_gap,
                    has_action,
                    forecast_eps,
                    forecast_roe,
                    eligible,
                    eligibility_reason,
                    quality_status,
                    quality_flags,
                    CALCULATION_VERSION,
                    missing,
                )
            )

        bulk_upsert(
            cur,
            "forecast_metrics",
            _METRIC_COLUMNS,
            output,
            update_cols=[c for c in _METRIC_COLUMNS if c != "forecast_id"],
        )
        if own_connection:
            conn.commit()
        return len(output)
    except Exception:
        if own_connection and conn is not None:
            conn.rollback()
        raise
    finally:
        if own_connection and conn is not None:
            cur.close()
            conn.close()


def current_metric(cur, code: str, as_of: date | None = None) -> tuple | None:
    """指定日時点の最新通期予想を返す。採点画面はこの1行だけを参照する。"""
    basis = as_of or jst_today()
    cur.execute(
        """
        SELECT fm.forecast_eps, fm.forecast_roe, fm.quality_status,
               fm.quality_flags, fm.missing_reason, fm.is_scoring_eligible,
               fm.fiscal_year_end, fm.announced_at
        FROM forecast_metrics fm
        JOIN price_adjustment_state s ON s.code=fm.code
          AND s.action_digest=fm.action_digest
        WHERE fm.code=%s AND fm.announced_at<=%s
          AND fm.fiscal_year_end>=%s
        ORDER BY fm.announced_at DESC, fm.fiscal_year_end ASC
        LIMIT 1
        """,
        (code, basis, basis),
    )
    return cur.fetchone()


def current_quality_summary(as_of: date | None = None) -> dict[str, int]:
    """国内普通株の最新予想について品質状態別件数を返す（夜間監視用）。"""
    basis = as_of or jst_today()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            WITH ranked AS (
                SELECT fm.quality_status,
                       ROW_NUMBER() OVER (
                           PARTITION BY fm.code
                           ORDER BY fm.announced_at DESC, fm.fiscal_year_end ASC,
                                    fm.forecast_id DESC
                       ) AS rn
                FROM forecast_metrics fm
                JOIN price_adjustment_state s ON s.code=fm.code
                  AND s.action_digest=fm.action_digest
                WHERE fm.is_scoring_eligible=TRUE
                  AND fm.announced_at<=%s AND fm.fiscal_year_end>=%s
            )
            SELECT quality_status, COUNT(*)
            FROM ranked WHERE rn=1 GROUP BY quality_status
            """,
            (basis, basis),
        )
        return {status: count for status, count in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def main() -> None:
    codes = sys.argv[1:] or None
    count = rebuild_forecast_metrics(codes)
    print(f"[予想指標] {count:,}件を再計算")
    if codes is None:
        summary = current_quality_summary()
        print("[予想指標品質] " + " / ".join(
            f"{status}={value:,}" for status, value in sorted(summary.items())
        ))


if __name__ == "__main__":
    main()
