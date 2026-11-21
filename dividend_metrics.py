"""配当通知を中間・期末等の事実単位で保存し、現在株数基準の年額DPSを作る。"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Iterable

from batch_dates import jst_today
from config import bulk_upsert, db
from price_adjustments import Action, actions_digest
from price_store import load_actions


CALCULATION_VERSION = "1"
COMPONENTS = {
    "FirstQuarterMember": "q1",
    "SecondQuarterMember": "interim",
    "ThirdQuarterMember": "q3",
    "YearEndMember": "final",
    "AnnualMember": "annual",
}


def _decimal(value) -> Decimal | None:
    try:
        result = Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None
    return result if result is None or result.is_finite() else None


def _json(values) -> str:
    return json.dumps(sorted(v for v in values if v), ensure_ascii=False, separators=(",", ":"))


def ensure_schema(cur) -> None:
    cur.execute("""CREATE TABLE IF NOT EXISTS dividend_notices (
        notice_id CHAR(64) PRIMARY KEY, code VARCHAR(10) NOT NULL,
        fiscal_year_end DATE NOT NULL, component VARCHAR(16) NOT NULL,
        notice_status VARCHAR(16) NOT NULL, raw_value DECIMAL(20,8) NULL,
        announced_at DATETIME NOT NULL, context_id VARCHAR(255) NOT NULL,
        source VARCHAR(30) NOT NULL, source_ref VARCHAR(2048) NOT NULL,
        payload_hash CHAR(64) NOT NULL, is_cancelled TINYINT(1) NOT NULL DEFAULT 0,
        ingested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_dividend_notice_payload (source,payload_hash),
        KEY idx_dividend_notice_current (code,fiscal_year_end,component,announced_at)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS dividend_events (
        event_id CHAR(64) PRIMARY KEY, notice_id CHAR(64) NOT NULL,
        code VARCHAR(10) NOT NULL, fiscal_year_end DATE NOT NULL,
        component VARCHAR(16) NOT NULL, notice_status VARCHAR(16) NOT NULL,
        raw_value DECIMAL(20,8) NULL, normalized_value DECIMAL(20,8) NULL,
        source_basis_date DATE NULL, source_basis_action_ids JSON NOT NULL,
        applied_action_ids JSON NOT NULL, share_adjustment_factor DECIMAL(28,18) NULL,
        action_digest CHAR(64) NOT NULL, target_basis_date DATE NOT NULL,
        quality_status VARCHAR(20) NOT NULL, quality_reason VARCHAR(512) NULL,
        calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_dividend_event_current (code,fiscal_year_end,component,quality_status)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS dividend_annual_normalized (
        code VARCHAR(10) NOT NULL, fiscal_year_end DATE NOT NULL,
        action_digest CHAR(64) NOT NULL, target_basis_date DATE NOT NULL,
        normalized_dps DECIMAL(20,8) NULL, component_event_ids JSON NOT NULL,
        basis_status VARCHAR(24) NOT NULL,
        quality_status VARCHAR(20) NOT NULL, quality_reason VARCHAR(512) NULL,
        calculation_version VARCHAR(16) NOT NULL,
        calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (code,fiscal_year_end,action_digest,calculation_version),
        KEY idx_dividend_annual_current (code,fiscal_year_end,quality_status)
    )""")


def extract_tdnet_notices(summary, disclosed_at: datetime, source_ref: str) -> list[tuple]:
    """TDnet短信XBRLの全配当コンテキストを、年額に潰さず返す。"""
    code5 = (summary.meta("SecuritiesCode") or "").strip()
    code = code5[:4] if len(code5) >= 4 else None
    fye_text = summary.meta("FiscalYearEnd")
    try:
        fye = date.fromisoformat(str(fye_text)[:10])
    except (TypeError, ValueError):
        return []
    if not code:
        return []
    rows = []
    for (local, context_id), fact in summary.facts.items():
        if local != "DividendPerShare":
            continue
        value = _decimal(getattr(fact, "get_text")(strip=True).replace(",", "").replace("△", "-"))
        if value is not None and fact.get("scale") not in (None, ""):
            value *= Decimal(10) ** int(fact.get("scale"))
        if value is not None and fact.get("sign") == "-":
            value = -value
        if value is None:
            continue
        component = next((value for member, value in COMPONENTS.items() if member in context_id), None)
        if component is None:
            continue
        if "ResultMember" in context_id:
            status = "result"
        elif "ForecastMember" in context_id:
            status = "forecast"
        else:
            continue
        # PriorYearは同じ短信の比較値。対象期を1年戻す。
        notice_fye = fye
        if context_id.startswith("PriorYear"):
            try:
                notice_fye = fye.replace(year=fye.year - 1)
            except ValueError:
                notice_fye = fye.replace(year=fye.year - 1, day=28)
        payload = {
            "code": code, "fiscal_year_end": str(notice_fye), "component": component,
            "status": status, "raw_value": str(value), "announced_at": str(disclosed_at),
            "context_id": context_id, "source_ref": source_ref,
        }
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        notice_id = hashlib.sha256(f"tdnet|{payload_hash}".encode()).hexdigest()
        rows.append((notice_id, code, notice_fye, component, status, value, disclosed_at,
                     context_id[:255], "tdnet", source_ref, payload_hash, 0, datetime.now()))
    return rows


def save_tdnet_notices(cur, rows: list[tuple], *, ensure: bool = True) -> int:
    if not rows:
        return 0
    if ensure:
        ensure_schema(cur)
    columns = ["notice_id","code","fiscal_year_end","component","notice_status","raw_value",
               "announced_at","context_id","source","source_ref","payload_hash","is_cancelled",
               "ingested_at"]
    bulk_upsert(cur, "dividend_notices", columns, rows, update_cols=["notice_id"])
    return len(rows)


def _backfill_legacy_forecasts(cur, requested: list[str]) -> None:
    """既存の通期会社予想を、新しい通知原本表へ一度だけ移す。"""
    sql = """SELECT id,code,fiscal_year_end,div_per_share,announced_at
             FROM financials_forecast
             WHERE period_type='A' AND div_per_share IS NOT NULL"""
    params: list[str] = []
    if requested:
        sql += " AND code IN (" + ",".join(["%s"] * len(requested)) + ")"
        params = requested
    cur.execute(sql, params)
    rows = []
    for forecast_id, code, fye, raw, announced in cur.fetchall():
        payload = {"forecast_id": forecast_id, "code": code, "fye": str(fye),
                   "raw": str(raw), "announced": str(announced)}
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        notice_id = hashlib.sha256(f"financials_forecast|{payload_hash}".encode()).hexdigest()
        announced_at = announced if isinstance(announced, datetime) else datetime.combine(announced, datetime.min.time())
        rows.append((notice_id,code,fye,"annual","forecast",_decimal(raw),announced_at,
                     f"legacy-forecast-{forecast_id}","financials_forecast",str(forecast_id),
                     payload_hash,0,datetime.now()))
    save_tdnet_notices(cur, rows, ensure=False)


def _normalize_notice(raw: Decimal, announced: date, status: str, actions: list[Action],
                      actual_amounts: list[Decimal], *, target_basis_date: date | None = None
                      ) -> tuple[list[Action], str, str]:
    target = target_basis_date or jst_today()
    confirmed = [a for a in sorted(actions, key=lambda x: (x.ex_date, x.action_id or ""))
                 if a.status == "confirmed" and a.kind in {"split", "consolidation", "bonus"}]
    active = [a for a in confirmed if a.ex_date <= target]
    if status == "forecast":
        # 権利落ち前に、すでに公表済みの分割後基準で会社予想が出ることがある。
        # 現在の分割前株価へ逆補正する根拠はXBRL値だけでは足りないため、
        # 権利落ちまでは推測で埋めず保留する。
        for action in confirmed:
            if action.ex_date <= target or action.ex_date <= announced:
                continue
            if action.announced_at is None:
                return [], "needs_review", "future_action_announcement_date_missing"
            if action.announced_at <= announced:
                return [], "needs_review", "forecast_basis_ambiguous_before_future_ex_date"
        applied = []
        for action in active:
            if action.ex_date <= announced:
                continue
            if action.announced_at is None:
                return [], "needs_review", "action_announcement_date_missing"
            if action.announced_at > announced:
                applied.append(action)
        return applied, "verified", "forecast_announcement_basis"
    # 実績はYahooの調整済配当イベントを数値照合にだけ使う。
    candidates = []
    for count in range(len(active) + 1):
        applied = active[len(active) - count:] if count else []
        factor = Decimal(1)
        for action in applied:
            factor *= action.price_factor
        value = raw * factor
        gap = min((abs(value - actual) for actual in actual_amounts), default=Decimal("Infinity"))
        candidates.append((gap, len(applied), applied))
    candidates.sort(key=lambda item: (item[0], item[1]))
    gap, _, applied = candidates[0]
    if gap <= max(Decimal("0.01"), abs(raw) * Decimal("0.001")):
        return applied, "verified", f"official_notice_matches_adjusted_event:gap={gap}"
    return [], "needs_review", "result_basis_not_verified"


def rebuild_dividend_metrics(codes: Iterable[str] | None = None, *, basis_date: date | None = None) -> int:
    target = basis_date or jst_today()
    requested = sorted({str(c).strip().upper() for c in (codes or []) if str(c).strip()})
    with db() as cur:
        ensure_schema(cur)
        _backfill_legacy_forecasts(cur, requested)
        where = ""
        params: list[str] = []
        if requested:
            where = " WHERE code IN (" + ",".join(["%s"] * len(requested)) + ")"
            params = requested
        cur.execute("""SELECT notice_id,code,fiscal_year_end,component,notice_status,raw_value,
                              announced_at
                       FROM dividend_notices""" + where +
                    " ORDER BY code,fiscal_year_end,component,announced_at DESC,notice_id DESC", params)
        notices = list(cur.fetchall())
        if not notices:
            return 0
        codes_seen = sorted({r[1] for r in notices})
        ph = ",".join(["%s"] * len(codes_seen))
        cur.execute(f"SELECT code,ex_date,amount FROM dividends WHERE code IN ({ph}) AND amount>0", codes_seen)
        actual_rows = list(cur.fetchall())
        actions_by_code = load_actions(cur)
        latest: dict[tuple, tuple] = {}
        for row in notices:
            key = (row[1], row[2], row[3])
            latest.setdefault(key, row)
        event_rows = []
        events_by_year: dict[tuple, list[tuple]] = defaultdict(list)
        for (code, fye, component), row in latest.items():
            notice_id, _, _, _, status, raw, announced_at = row
            all_actions = actions_by_code.get(code, [])
            digest = actions_digest(all_actions, target)
            raw_d = _decimal(raw)
            try:
                prior_fye = fye.replace(year=fye.year - 1)
            except ValueError:
                prior_fye = fye.replace(year=fye.year - 1, day=28)
            fiscal_amounts = [_decimal(amount) for actual_code, ex_date, amount in actual_rows
                              if actual_code == code and prior_fye < ex_date <= fye]
            applied, quality, reason = _normalize_notice(
                raw_d, announced_at.date() if isinstance(announced_at, datetime) else announced_at,
                status, all_actions, fiscal_amounts, target_basis_date=target,
            )
            active_ids = [a.action_id for a in all_actions
                          if a.status == "confirmed" and a.ex_date <= target
                          and a.kind in {"split","consolidation","bonus"}]
            applied_ids = [a.action_id for a in applied]
            source_ids = [value for value in active_ids if value not in set(applied_ids)]
            factor = Decimal(1)
            for action in applied:
                factor *= action.price_factor
            normalized = raw_d * factor if quality == "verified" else None
            event_id = hashlib.sha256(f"{notice_id}|{digest}|{CALCULATION_VERSION}".encode()).hexdigest()
            event = (event_id, notice_id, code, fye, component, status, raw_d, normalized,
                     announced_at.date() if isinstance(announced_at, datetime) else announced_at,
                     _json(source_ids), _json(applied_ids), factor if quality == "verified" else None,
                     digest, target, quality, reason, datetime.now())
            event_rows.append(event)
            events_by_year[(code, fye, digest)].append(event)
        columns = ["event_id","notice_id","code","fiscal_year_end","component","notice_status",
                   "raw_value","normalized_value","source_basis_date","source_basis_action_ids",
                   "applied_action_ids","share_adjustment_factor","action_digest","target_basis_date",
                   "quality_status","quality_reason","calculated_at"]
        bulk_upsert(cur, "dividend_events", columns, event_rows, update_cols=columns[6:])

        annual_rows = []
        for (code, fye, digest), events in events_by_year.items():
            components = [e for e in events if e[4] != "annual" and e[14] == "verified"]
            annual = next((e for e in events if e[4] == "annual" and e[14] == "verified"), None)
            # 複数回配当は各通知を正規化した後に足す。年額一括補正はしない。
            component_names = {e[4] for e in components}
            if {"interim", "final"} <= component_names:
                chosen = components
                total = sum((e[7] for e in chosen), Decimal(0))
                basis_status = "component_sum"
                quality, reason = "verified", None
            elif annual is not None:
                chosen = [annual]
                total = annual[7]
                basis_status = "annual_notice_fallback"
                quality, reason = "verified", None
            else:
                chosen = events
                total = None
                basis_status = "incomplete"
                quality, reason = "incomplete", "annual_components_incomplete"
            annual_rows.append((code, fye, digest, target, total, _json([e[0] for e in chosen]),
                                basis_status, quality, reason, CALCULATION_VERSION, datetime.now()))
        annual_columns = ["code","fiscal_year_end","action_digest","target_basis_date",
                          "normalized_dps","component_event_ids","basis_status","quality_status",
                          "quality_reason","calculation_version","calculated_at"]
        bulk_upsert(cur, "dividend_annual_normalized", annual_columns, annual_rows,
                    update_cols=annual_columns[3:])
        state_rows = [(code, "dividend", digest, digest, "complete", None, None, datetime.now())
                      for code, digest in sorted({(c, d) for c, _fye, d in events_by_year})]
        bulk_upsert(cur, "corporate_action_projection_state",
                    ["code","consumer","required_digest","completed_digest","status",
                     "not_before","last_error","updated_at"], state_rows)
        return len(annual_rows)


def main() -> None:
    import sys
    print(f"[配当正規化] {rebuild_dividend_metrics(sys.argv[1:] or None):,}年度")


if __name__ == "__main__":
    main()
