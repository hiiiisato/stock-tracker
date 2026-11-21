"""EPS/BPS原本の株数基準を判定し、公開価格と同じ基準へ投影する。

原本値は上書きしない。公式権利台帳の集合差から毎回再生成し、
補正済み値へ係数を重ねて掛けない。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from typing import Iterable

from batch_dates import jst_today
from config import bulk_upsert, db
from price_adjustments import Action, actions_digest
from price_store import load_actions


CALCULATION_VERSION = "1"
SIMPLE_KINDS = {"split", "consolidation", "bonus"}


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _relative_gap(left: Decimal, right: Decimal) -> Decimal:
    return abs(left - right) / abs(right) if right else Decimal("Infinity")


def _json(values: Iterable[str]) -> str:
    return json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))


def resolve_applied_actions(
    *,
    numerator,
    raw_value,
    current_shares,
    actions: list[Action],
    source_basis_date: date | None = None,
    tolerance: Decimal = Decimal("0.15"),
) -> tuple[list[Action], Decimal | None, str, str]:
    """原本が未反映の「後ろから連続する権利」を一意に決める。

    EPS/BPSから逆算した株式数に各候補の累積株数係数を掛け、
    現在株式数と最も整合する連続suffixを選ぶ。中抜きの適用は認めない。
    """
    numerator_d = _decimal(numerator)
    value_d = _decimal(raw_value)
    shares_d = _decimal(current_shares)
    if numerator_d is None or value_d in (None, Decimal(0)):
        return [], None, "needs_review", "missing_numerator_or_per_share_value"
    implied = numerator_d / value_d
    if implied <= 0:
        return [], None, "needs_review", "nonpositive_implied_shares"

    eligible = [a for a in sorted(actions, key=lambda x: (x.ex_date, x.action_id or ""))
                if a.status == "confirmed" and a.kind in SIMPLE_KINDS
                and (source_basis_date is None or a.ex_date > source_basis_date)]
    if not eligible:
        return [], implied, "verified", "no_post_basis_corporate_action"
    if shares_d is None or shares_d <= 0:
        return [], implied, "needs_review", "missing_current_shares"
    candidates: list[tuple[Decimal, list[Action], Decimal]] = []
    with localcontext() as ctx:
        ctx.prec = 38
        # 原本はある時点までの連続した権利を反映済みとみなす。
        for count in range(len(eligible) + 1):
            applied = eligible[len(eligible) - count:] if count else []
            share_factor = Decimal(1)
            for action in applied:
                share_factor *= action.volume_factor
            projected = implied * share_factor
            candidates.append((_relative_gap(projected, shares_d), applied, projected))
    candidates.sort(key=lambda item: (item[0], len(item[1])))
    best_gap, best_actions, projected = candidates[0]
    if best_gap > tolerance:
        return [], implied, "needs_review", f"share_basis_unresolved:gap={best_gap:.6f}"
    # 異なる係数の候補がほぼ同程度なら、無理に正式値にしない。
    if len(candidates) > 1:
        second_gap, second_actions, _ = candidates[1]
        best_factor = Decimal(1)
        second_factor = Decimal(1)
        for action in best_actions:
            best_factor *= action.volume_factor
        for action in second_actions:
            second_factor *= action.volume_factor
        if best_factor != second_factor and second_gap - best_gap < Decimal("0.02"):
            return [], implied, "needs_review", "share_basis_ambiguous"
    return best_actions, implied, "verified", f"implied_share_gap={best_gap:.6f};projected={projected:.6f}"


def ensure_schema(cur) -> None:
    cur.execute("""CREATE TABLE IF NOT EXISTS per_share_fact_versions (
        fact_id CHAR(64) PRIMARY KEY, code VARCHAR(10) NOT NULL,
        metric VARCHAR(12) NOT NULL, source VARCHAR(30) NOT NULL,
        source_ref VARCHAR(255) NOT NULL, payload_hash CHAR(64) NOT NULL,
        period_type VARCHAR(32) NOT NULL, period_start DATE NULL, period_end DATE NOT NULL,
        announced_at DATE NULL, is_comparative TINYINT(1) NOT NULL DEFAULT 0,
        consolidation_scope VARCHAR(16) NULL, accounting_standard VARCHAR(16) NULL,
        raw_value DECIMAL(28,10) NULL, numerator_raw DECIMAL(30,4) NULL,
        reported_average_shares DECIMAL(30,6) NULL, reported_end_shares DECIMAL(30,6) NULL,
        xbrl_concept VARCHAR(255) NULL, source_basis_date DATE NULL,
        source_basis_status VARCHAR(32) NOT NULL,
        source_basis_action_ids JSON NOT NULL,
        quality_status VARCHAR(20) NOT NULL, quality_reason VARCHAR(512) NULL,
        ingested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_per_share_fact_current (code,metric,period_end,announced_at),
        UNIQUE KEY uq_per_share_payload (code,metric,period_end,source,payload_hash)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS per_share_fact_projections (
        fact_id CHAR(64) NOT NULL, action_digest CHAR(64) NOT NULL,
        target_basis_date DATE NOT NULL, calculation_version VARCHAR(16) NOT NULL,
        applied_action_ids JSON NOT NULL,
        share_adjustment_factor DECIMAL(28,18) NULL,
        normalized_value DECIMAL(28,10) NULL,
        quality_status VARCHAR(20) NOT NULL, quality_reason VARCHAR(512) NULL,
        computed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (fact_id,action_digest,calculation_version),
        KEY idx_per_share_projection_digest (action_digest,quality_status)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS corporate_action_projection_state (
        code VARCHAR(10) NOT NULL, consumer VARCHAR(24) NOT NULL,
        required_digest CHAR(64) NULL, completed_digest CHAR(64) NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'scheduled', not_before DATETIME NULL,
        last_error TEXT NULL, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (code,consumer), KEY idx_projection_pending (status,not_before)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS data_coverage_state (
        code VARCHAR(10) NOT NULL, dataset VARCHAR(30) NOT NULL,
        coverage_status VARCHAR(24) NOT NULL, reason VARCHAR(255) NULL,
        source_observed_at DATETIME NULL, checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (code,dataset), KEY idx_coverage_status (dataset,coverage_status)
    )""")


def _announced(value, fallback) -> date | None:
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            pass
    return fallback


def rebuild_per_share_metrics(codes: Iterable[str] | None = None, *, basis_date: date | None = None) -> int:
    """EDINET年次原本からEPS/BPSの版と現在投影を冪等再生成する。"""
    target = basis_date or jst_today()
    requested = sorted({str(c).strip().upper() for c in (codes or []) if str(c).strip()})
    with db() as cur:
        ensure_schema(cur)
        sql = """SELECT e.code,e.period_end,e.eps,e.bps,e.net_income,
                        COALESCE(e.net_assets,e.shareholders_equity),e.shares_issued,
                        e.accounting_standard,e.doc_id,e.submit_date,e.updated_at,
                        sf.shares_outstanding
                 FROM financials_edinet_annual e
                 LEFT JOIN stock_fundamentals sf ON sf.code=e.code
                 WHERE e.period_end IS NOT NULL"""
        params: list[str] = []
        if requested:
            sql += " AND e.code IN (" + ",".join(["%s"] * len(requested)) + ")"
            params = requested
        cur.execute(sql, params)
        rows = list(cur.fetchall())
        actions_by_code = load_actions(cur)
        output_facts = []
        output_projections = []
        state_rows = []
        now = datetime.now()
        for (code, period_end, eps, bps, net_income, equity, shares_issued,
             standard, doc_id, submit_date, updated_at, current_shares) in rows:
            all_actions = [a for a in actions_by_code.get(code, []) if a.ex_date <= target]
            active_actions = [a for a in all_actions if a.status == "confirmed" and a.kind in SIMPLE_KINDS]
            digest = actions_digest(all_actions, target)
            for metric, raw, numerator in (("eps", eps, net_income), ("bps", bps, equity)):
                if raw is None:
                    continue
                applied, implied, quality, reason = resolve_applied_actions(
                    numerator=numerator, raw_value=raw, current_shares=current_shares,
                    actions=active_actions, source_basis_date=period_end,
                )
                applied_ids = [a.action_id for a in applied if a.action_id]
                source_ids = [a.action_id for a in active_actions
                              if a.action_id and a.action_id not in set(applied_ids)]
                raw_d = _decimal(raw)
                factor = Decimal(1)
                for action in applied:
                    factor *= action.price_factor
                normalized = raw_d * factor if raw_d is not None and quality == "verified" else None
                announced = _announced(submit_date, period_end)
                source_ref = str(doc_id or f"edinetdb:{code}:{period_end}")
                payload = {
                    "code": code, "metric": metric, "period_end": str(period_end),
                    "raw_value": str(raw), "numerator": str(numerator),
                    "reported_end_shares": str(shares_issued), "source_ref": source_ref,
                    "updated_at": str(updated_at),
                }
                payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                fact_id = hashlib.sha256(f"edinetdb|{payload_hash}".encode()).hexdigest()
                basis_status = "derived_post_action" if not applied else "derived_pre_action"
                output_facts.append((
                    fact_id, code, metric, "edinetdb", source_ref, payload_hash,
                    "annual", None, period_end, announced, 0, "consolidated", standard,
                    raw_d, _decimal(numerator), implied if metric == "eps" else None,
                    _decimal(shares_issued), None, period_end, basis_status, _json(source_ids),
                    quality, reason, now,
                ))
                output_projections.append((
                    fact_id, digest, target, CALCULATION_VERSION, _json(applied_ids),
                    factor if quality == "verified" else None, normalized,
                    quality, reason, now,
                ))
            state_rows.append((code, "per_share", digest, digest, "complete", None, None, now))
        fact_columns = [
            "fact_id","code","metric","source","source_ref","payload_hash","period_type",
            "period_start","period_end","announced_at","is_comparative","consolidation_scope",
            "accounting_standard","raw_value","numerator_raw","reported_average_shares",
            "reported_end_shares","xbrl_concept","source_basis_date","source_basis_status",
            "source_basis_action_ids","quality_status","quality_reason","ingested_at",
        ]
        projection_columns = [
            "fact_id","action_digest","target_basis_date","calculation_version",
            "applied_action_ids","share_adjustment_factor","normalized_value",
            "quality_status","quality_reason","computed_at",
        ]
        if output_facts:
            bulk_upsert(cur, "per_share_fact_versions", fact_columns, output_facts,
                        update_cols=["fact_id"])
            bulk_upsert(cur, "per_share_fact_projections", projection_columns, output_projections,
                        update_cols=projection_columns[2:])
            bulk_upsert(cur, "corporate_action_projection_state",
                        ["code","consumer","required_digest","completed_digest","status",
                         "not_before","last_error","updated_at"], state_rows)
        quality_by_code: dict[str, set[str]] = {}
        fact_codes = {fact[0]: fact[1] for fact in output_facts}
        for row in output_projections:
            # projection tuple: fact_id,digest,target,version,ids,factor,value,status,...
            fact_code = fact_codes.get(row[0])
            if fact_code:
                quality_by_code.setdefault(fact_code, set()).add(row[7])
        coverage_rows = [
            (code, "per_share_metrics",
             "available" if "verified" in statuses else "needs_review",
             None if "verified" in statuses else "share_basis_unresolved", now, now)
            for code, statuses in quality_by_code.items()
        ]
        if coverage_rows:
            bulk_upsert(cur, "data_coverage_state",
                        ["code","dataset","coverage_status","reason","source_observed_at","checked_at"],
                        coverage_rows)
        # 新規上場等で有報がまだ無い場合も、欠落ではなく待ち状態として可視化。
        cur.execute("""INSERT INTO data_coverage_state
            (code,dataset,coverage_status,reason,checked_at)
            SELECT s.code,'per_share_metrics','pending','awaiting_official_annual_filing',UTC_TIMESTAMP()
            FROM stocks s
            WHERE s.is_active=TRUE AND NOT EXISTS (
                SELECT 1 FROM financials_edinet_annual e WHERE e.code=s.code
            )
            ON DUPLICATE KEY UPDATE coverage_status=VALUES(coverage_status),
                                    reason=VALUES(reason),checked_at=VALUES(checked_at)""")
        return len(output_projections)


def main() -> None:
    import sys
    count = rebuild_per_share_metrics(sys.argv[1:] or None)
    print(f"[1株指標] EPS/BPS投影 {count:,}件")


if __name__ == "__main__":
    main()
