"""公式台帳と取得原本から価格を公開する唯一の書込経路。

初回: --migrate → --sync-actions --history → --repair（対象は--codesで指定可）。
夜間: --audit が12週間を過ぎた日付を公式照合し、不一致を保留・修復する。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from decimal import Decimal
import json
import re
import time

from batch_dates import jst_today, price_fetch_end_date
from config import db, PRICE_HISTORY_START, PRICE_NIGHTLY_REPAIR_CODES, PRICE_REFERENCE_OVERLAP_DAYS, price_db_session
from price_adjustments import (PriceBasisError, StalePublication, actions_digest, day, factors,
                               identify_basis, normalize, matches)
from price_store import (ensure_schema, load_actions, backup_legacy, snapshot, publish, hold, lock_code, PricePublicationHeld, iter_price_rows)


def _source_events_verified(code: str, rows: list[dict], actions: list, *, allow_lookup: bool) -> None:
    events = getattr(rows, "split_events", [])
    known = {str(a.ex_date): a for a in actions if a.status == "confirmed"}
    unknown = []
    for event in events:
        ex = event["date"]
        ratio = Decimal(str(event["denominator"])) / Decimal(str(event["numerator"]))
        if day(ex) > jst_today():
            continue
        if ex in known and abs(known[ex].price_factor - ratio) <= Decimal("0.000002"):
            continue
        with db() as cur:
            cur.execute("SELECT official_factor FROM price_official_checks WHERE code=%s AND date=%s", (code, ex))
            official = cur.fetchone()
        # Yahooだけの偽イベントは、同日の公式係数1で否定できる。
        if official and official[0] == 1:
            continue
        unknown.append(ex)
    if unknown:
        if allow_lookup:
            from corporate_actions import confirm_disclosed_candidate
            for ex in unknown:
                confirm_disclosed_candidate(code,day(ex))
            try:
                _source_events_verified(code,rows,load_actions(code=code).get(code,[]),allow_lookup=False)
                return
            except PriceBasisError:
                pass
            from price_verification import free_cutoff, free_start
            mature = [day(ex) for ex in unknown if free_start() <= day(ex) <= free_cutoff()]
            if mature:
                # 過去の偽候補は公式の当日を照合する。価格差から採用しない。
                from price_verification import fetch_bars, remember_bars
                remember_bars(fetch_bars(code=code, since=min(mature), until=max(mature)))
                return _source_events_verified(code, rows, load_actions().get(code, []), allow_lookup=False)
        raise PriceBasisError(f"{code}: Yahooの分割候補{','.join(unknown)}を公式台帳で確認できません")


def _volume_anchors(code: str) -> dict[str, Decimal]:
    with db() as cur:
        cur.execute("SELECT date,volume FROM price_official_checks WHERE code=%s AND volume > 0", (code,))
        result = {str(d): Decimal(v) for d, v in cur.fetchall()}
        cur.execute("SELECT date,volume FROM daily_prices WHERE code=%s AND price_basis=%s AND volume > 0", (code, "raw"))
        for d, v in cur.fetchall():
            result.setdefault(str(d), Decimal(v))
    return result


def _with_official_values(code: str, rows: list[dict], actions: list, today: date,
                          basis: str, volume_basis: str) -> list[dict]:
    """基準確認後、同日の当時値がある箇所は公式OHLCVを優先する。"""
    with db() as cur:
        cur.execute("SELECT date,open,high,low,close,volume FROM price_official_checks WHERE code=%s AND close > 0", (code,))
        official = {str(r[0]): dict(zip(("open","high","low","close","volume"),r[1:])) for r in cur.fetchall()}
    output = []
    for row in rows:
        ref = official.get(str(row["date"]))
        if not ref:
            output.append(row)
            continue
        pf, vf = factors(day(row["date"]),actions,today)
        values = {f: ref[f] * (pf if basis == "split_adjusted" else 1)
                  if ref[f] is not None else None for f in ("open","high","low","close")}
        values["volume"] = ref["volume"] * (vf if volume_basis == "split_adjusted" else 1) if ref["volume"] is not None else None
        output.append({**row,**values})
    return output


def official_window_candidate(official: list[dict], provider: list[dict], actions: list,
                              today: date, cutoff: date, price_refs: dict, volume_refs: dict) -> list[dict]:
    """提供元の古い区間が不整合でも、公式履歴と基準確認した直近区間で再構築する。"""
    if not official:
        raise PriceBasisError("再構築できる公式当時値がありません")
    # 後続の権利を織り込んだ、直近の重複期間で基準を確認する。
    # 古い壊れた提供値の係数を新しい区間へ推測で持ち込まない。
    start = cutoff - timedelta(days=PRICE_REFERENCE_OVERLAP_DAYS)
    reference_rows = [r for r in provider if day(r["date"]) >= start]
    basis = identify_basis(reference_rows,actions,today,price_refs)
    volume_basis = identify_basis(reference_rows,actions,today,volume_refs,field="volume",absolute=Decimal(1))
    recent = normalize([r for r in provider if day(r["date"]) > cutoff],actions,today,basis,volume_basis=volume_basis)
    keys = ("code","date","open","high","low","close","volume")
    return official + [{k:r[k] for k in keys} for r in recent]


def publish_yahoo_code(code: str, rows: list[dict], *, until: date,
                       force_full: bool = False, allow_official_lookup: bool = True,
                       action_cache: dict | None = None, metadata: tuple | None = None) -> int:
    from prices_yahoo import _fetch_yahoo
    from price_verification import anchors_for, obtain_anchors, free_cutoff, free_start
    if not rows:
        raise RuntimeError(f"{code}: 価格を取得できません")
    with db() as cur:
        # 台帳が未確定・既存公式値と不一致の場合も、今回受け取った原本を失わない。
        snapshot(cur,code,"yahoo","provider_observation",rows,
                 split_events=getattr(rows,"split_events",[]))
    today = jst_today()
    actions = (action_cache if action_cache is not None else load_actions()).get(code, [])
    digest = actions_digest(actions, today)
    if metadata is None:
        with db() as cur:
            cur.execute("SELECT MIN(date) FROM daily_prices WHERE code=%s", (code,))
            earliest = cur.fetchone()[0] or date.fromisoformat(PRICE_HISTORY_START)
            cur.execute("SELECT action_digest,status FROM price_adjustment_state WHERE code=%s", (code,))
            state = cur.fetchone()
    else:
        earliest, prior_digest, prior_status = metadata
        earliest = earliest or date.fromisoformat(PRICE_HISTORY_START)
        state = (prior_digest, prior_status) if prior_status else None
    if allow_official_lookup:
        with db() as cur:
            cur.execute("SELECT error FROM price_official_checks WHERE code=%s AND status=%s", (code,"mismatch"))
            mismatches=[r[0] or '' for r in cur.fetchall()]
            needs_official_history=any('adjusted_close' in e or 'adjusted_volume' in e or
                                       re.search(r'(?: |,)close(?:,|$)',e) for e in mismatches)
            if mismatches and all(e.endswith(': no_trade') for e in mismatches):
                cur.execute("SELECT 1 FROM price_official_checks WHERE code=%s AND close > 0 LIMIT 1",(code,))
                if cur.fetchone() is None:
                    raise PriceBasisError(f'{code}: 保存済みの公式市場データに約定を確認できません。提供元の価格を保留します')
        if needs_official_history:
            from corporate_actions import confirm_disclosed_candidate
            confirm_disclosed_candidate(code,None)
            # 台帳に欠けた古いイベントも、公式の不一致から銘柄履歴を取り直して発見する。
            obtain_anchors(code,earliest)
            actions = load_actions(code=code).get(code,[])
            digest = actions_digest(actions,today)
    due_actions = [a for a in actions if a.ex_date <= today and a.status != "cancelled"]
    if any(a.status != "confirmed" for a in due_actions):
        raise PriceBasisError(f"{code}: 実施済みの分割・併合の比率が未確定です")
    full = force_full or bool(state and (state[0] != digest or state[1] == "held")) or bool(due_actions and not state)
    if full:
        rows = _fetch_yahoo(code, earliest, until)
        if not rows:
            raise RuntimeError(f"{code}: 再生成用の過去価格を取得できません")
    with db() as cur:
        # 基準を判定できない取得結果も原本は残す。変換成功と取得成功を区別する。
        original_id = snapshot(cur, code, "yahoo", "provider_observation", rows,
                               split_events=getattr(rows, "split_events", []))
    observed_at = getattr(rows, "observed_at", None)
    _source_events_verified(code, rows, actions, allow_lookup=allow_official_lookup)
    with db() as cur:
        cur.execute("SELECT date FROM price_official_checks WHERE code=%s AND close IS NULL AND date <= %s", (code, free_cutoff()))
        no_trade_dates = {str(r[0]) for r in cur.fetchall()}
    rows = [r for r in rows if str(r["date"]) not in no_trade_dates]
    if not rows:
        raise PriceBasisError(f"{code}: 公式無約定日を除くと価格がありません")
    # 公式候補の解決で台帳に追加されたイベントも同じ実行に反映する。
    actions = load_actions(code=code).get(code, [])
    due_actions = [a for a in actions if a.ex_date <= today and a.status != "cancelled"]
    anchors = anchors_for(code) if due_actions else {}
    official_window = False
    try:
        basis = identify_basis(rows, actions, today, anchors)
    except PriceBasisError:
        if not allow_official_lookup:
            raise
        anchors = obtain_anchors(code, earliest)
        actions = load_actions(code=code).get(code, [])
        due_actions = [a for a in actions if a.ex_date <= today and a.status != "cancelled"]
        try:
            basis = identify_basis(rows, actions, today, anchors)
        except PriceBasisError:
            # 無料枠より古い分割で判別日が失われた場合、推測して過去全体を補正しない。
            # 取得可能な公式当時値と、全イベント実施後の直近値だけで再構築する。
            cutoff = free_cutoff()
            with db() as cur:
                cur.execute("""SELECT date,open,high,low,close,volume FROM price_official_checks
                               WHERE code=%s AND date BETWEEN %s AND %s AND close > 0 ORDER BY date""",
                            (code, free_start(), cutoff))
                official = [dict(zip(("date","open","high","low","close","volume"), r),code=code) for r in cur.fetchall()]
            rows = official_window_candidate(official,rows,actions,today,cutoff,anchors,_volume_anchors(code))
            basis, official_window, full = "raw", True, True
            earliest = day(official[0]["date"])
    try:
        volume_basis = "raw" if official_window or not due_actions else identify_basis(
            rows, actions, today, _volume_anchors(code), field="volume", absolute=Decimal(1))
    except PriceBasisError:
        if not allow_official_lookup:raise
        anchors=obtain_anchors(code,earliest)
        actions=load_actions(code=code).get(code,[])
        cutoff=free_cutoff()
        with db() as cur:
            cur.execute("""SELECT date,open,high,low,close,volume FROM price_official_checks
                           WHERE code=%s AND date BETWEEN %s AND %s AND close > 0 ORDER BY date""",(code,free_start(),cutoff))
            official=[dict(zip(("date","open","high","low","close","volume"),r),code=code) for r in cur.fetchall()]
        rows=official_window_candidate(official,rows,actions,today,cutoff,anchors,_volume_anchors(code))
        basis,volume_basis,official_window,full="raw","raw",True,True
        earliest=day(official[0]['date'])
    # 空のOHLCを補完するために原本を加工しない。公式で確認できる欠落日だけ補う。
    dates = {str(r["date"]) for r in rows}
    supplemental = []
    with db() as cur:
        if full:
            cur.execute("""SELECT p.date FROM daily_prices p JOIN trading_calendar c ON c.date=p.date
                           WHERE p.code=%s AND c.is_holiday=FALSE AND p.close IS NOT NULL
                             AND p.date BETWEEN %s AND %s""", (code, earliest, until))
            missing = [r[0] for r in cur.fetchall() if str(r[0]) not in dates]
            if any(d < free_start() and str(d) not in no_trade_dates for d in missing):
                # 再取得不能な古い欠落を架空の足で埋めない。確認可能な期間から再構築し、
                # 古い原本はbackup_legacyへ残して公開範囲から外す。
                rows = [r for r in rows if day(r['date']) >= free_start()]
                missing = [d for d in missing if d >= free_start()]
                earliest = free_start()
            if allow_official_lookup and any(free_start() <= d <= free_cutoff() for d in missing):
                cur.execute("SELECT date FROM price_official_checks WHERE code=%s",(code,))
                recorded = {r[0] for r in cur.fetchall()}
                if any(d not in recorded and free_start() <= d <= free_cutoff() for d in missing):
                    # 長い欠落も1日ずつFree枠を消費せず、公式履歴を一度で取得する。
                    from price_verification import fetch_bars, remember_bars
                    remember_bars(fetch_bars(code=code,since=max(earliest,free_start()),until=free_cutoff()))
            for missing_date in missing:
                if str(missing_date) in no_trade_dates:
                    continue
                cur.execute("SELECT open,high,low,close,volume FROM price_official_checks WHERE code=%s AND date=%s", (code, missing_date))
                official = cur.fetchone()
                if official is None and free_start() <= missing_date <= free_cutoff() and allow_official_lookup:
                    from price_verification import fetch_bars, remember_bars
                    remember_bars(fetch_bars(code=code,since=missing_date,until=missing_date))
                    cur.execute("SELECT open,high,low,close,volume FROM price_official_checks WHERE code=%s AND date=%s", (code, missing_date))
                    official = cur.fetchone()
                if not official:
                    raise PriceBasisError(f"{code}: 過去価格の再取得に欠落があります: {missing_date}")
                if official[3] is None:
                    no_trade_dates.add(str(missing_date))
                    continue
                supplemental.append(dict(zip(("open", "high", "low", "close", "volume"), official), code=code, date=str(missing_date)))
        first_date = min(day(r["date"]) for r in rows)
        cur.execute("SELECT adj_close FROM daily_prices WHERE code=%s AND date < %s ORDER BY date DESC LIMIT 1", (code, first_date))
        previous = cur.fetchone()
    def generate():
        candidate = _with_official_values(code,rows,actions,today,basis,volume_basis)
        return normalize(candidate, actions, today, basis,
                         previous_adjusted=None if full else (previous[0] if previous else None),volume_basis=volume_basis)
    try:
        normalized = generate()
    except PriceBasisError:
        if not allow_official_lookup:
            raise
        obtain_anchors(code,earliest)
        if actions_digest(load_actions(code=code).get(code,[]),today) != actions_digest(actions,today):
            raise PriceBasisError(f"{code}: 新しい公式イベントが見つかりました。次回に全系列を再生成します")
        try:
            normalized = generate()
        except PriceBasisError:
            # 公式取得範囲より古い壊れたOHLCは、推測で高値・安値を補わない。
            # 基準確認済みの公式取得範囲から公開し、古い原本は保持する。
            if not due_actions or not any(day(r["date"]) < free_start() for r in rows):
                raise
            rows = [r for r in rows if day(r["date"]) >= free_start()]
            full = True
            normalized = generate()
            supplemental = [r for r in supplemental if day(r["date"]) >= free_start()]
    if supplemental:
        normalized += normalize(supplemental, actions, today, "raw")
        normalized.sort(key=lambda r: r["date"])
        prev = None
        for row in normalized:
            row["change_pct"] = ((row["adj_close"] / prev - 1) * 100).quantize(Decimal("0.0001")) if prev else None
            prev = row["adj_close"]
    # 同じ基準日に取得した独立公式値がある場合は、公開前にも照合する。
    # 未登録イベントが残ったまま「修復成功」として一時公開しない。
    with db() as cur:
        cur.execute("""SELECT date,official_adj_open,official_adj_high,official_adj_low,official_adj_close,official_adj_volume
                       FROM price_official_checks WHERE code=%s AND reference_basis_date=%s""",(code,today))
        references={str(r[0]):r[1:] for r in cur.fetchall()}
    for row in normalized:
        expected=references.get(row['date'])
        if not expected:continue
        for field,value in zip(('adj_open','adj_high','adj_low','adj_close','adj_volume'),expected):
            if value is not None and (row[field] is None or not matches(row[field],value,Decimal('0.00001'),Decimal(1) if field=='adj_volume' else Decimal('0.06'))):
                raise PriceBasisError(f"{code} {row['date']}: {field}が同じ基準日の公式値と一致しません")
    with db() as cur:
        lock_code(cur, code)
        backup_legacy(cur, code)
        sid = snapshot(cur, code, "normalization", basis, normalized, original_id=original_id,
                       volume_basis=volume_basis, basis_date=str(today), official_window=official_window)
        if full:
            cur.execute("UPDATE daily_prices SET price_basis=NULL,price_version=NULL WHERE code=%s AND date < %s AND price_basis=%s",
                        (code, normalized[0]["date"], "raw"))
            # 公式休場日の幽霊行は原本へ退避済み。新しい系列へ残さない。
            cur.execute("""DELETE p FROM daily_prices p JOIN trading_calendar c ON c.date=p.date
                           WHERE p.code=%s AND c.is_holiday=TRUE""", (code,))
            cur.execute("""DELETE p FROM daily_prices p JOIN price_official_checks q ON q.code=p.code AND q.date=p.date
                           WHERE p.code=%s AND q.close IS NULL AND q.date <= %s""", (code, free_cutoff()))
        publish(cur, code, normalized, actions, today, "official_window" if official_window else basis, sid, observed_at,
                rebuild=full)
    return len(normalized)


@price_db_session()
def store_yahoo_batches(batches: dict[str, list[dict]], until: date) -> int:
    ensure_schema()
    action_cache = load_actions()
    with db() as cur:
        cur.execute("""SELECT p.code,MIN(p.date),s.action_digest,s.status FROM daily_prices p
                       LEFT JOIN price_adjustment_state s ON s.code=p.code
                       GROUP BY p.code,s.action_digest,s.status""")
        metadata = {r[0]: r[1:] for r in cur.fetchall()}
    count, errors = 0, {}
    for code, rows in batches.items():
        try:
            count += publish_yahoo_code(code, rows, until=until, action_cache=action_cache,
                                        metadata=metadata.get(code, (None, None, None)))
        except StalePublication:
            continue
        except Exception as exc:
            # 他銘柄は進めるが、不一致の銘柄は読取ビューから保留する。
            hold(code, str(exc), attempted=True)
            errors[code] = str(exc)
    if errors:
        raise PricePublicationHeld(count,errors)
    return count


@price_db_session()
def repair_codes(codes: list[str] | None = None, *, max_workers: int = 4) -> dict:
    from prices_yahoo import _fetch_yahoo
    ensure_schema()
    from price_verification import seed_reference_dates
    seed_reference_dates()
    if codes is None:
        check_integrity()
        with db() as cur:
            cur.execute("""SELECT DISTINCT s.code FROM stocks s LEFT JOIN corporate_actions a ON a.code=s.code
                           LEFT JOIN price_adjustment_state p ON p.code=s.code
                           WHERE s.is_active=TRUE AND (a.ex_date <= %s OR p.status=%s)
                             AND (p.status IS NULL OR p.status<>%s OR p.price_date < %s) ORDER BY s.code""",
                        (jst_today(), "held", "published", price_fetch_end_date()))
            codes = [r[0] for r in cur.fetchall()]
    until = price_fetch_end_date()
    initial = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_yahoo, c, until - timedelta(days=14), until): c for c in codes}
        for future in as_completed(futures):
            initial[futures[future]] = future.result()
    repaired, errors = {}, {}
    for code in codes:
        try:
            repaired[code] = publish_yahoo_code(code, initial[code], until=until, force_full=True)
        except StalePublication:
            continue
        except Exception as exc:
            hold(code, str(exc), attempted=True)
            errors[code] = str(exc)
        print(f"  再生成 {code}: {repaired.get(code, errors.get(code))}", flush=True)
    return {"repaired": repaired, "errors": errors}


def check_integrity() -> dict:
    """調整の過不足を数式で検査。大きな日次騰落そのものは異常と認定しない。"""
    ensure_schema()
    bad = []
    actions = load_actions()
    with db() as cur:
        for code, dt, raw, adj, op,hi,lo,vo,aop,ahi,alo,avo in iter_price_rows(cur,
            ("code","date","close","adj_close","open","high","low","volume","adj_open","adj_high","adj_low","adj_volume"),
            source="daily_prices",positive_only=False):
            try:
                pf, vf = factors(dt, actions.get(code, []), jst_today())
                if raw is None or adj is None or abs(raw * pf - adj) > Decimal("0.06"):
                    bad.append(code)
                for original,adjusted in ((op,aop),(hi,ahi),(lo,alo)):
                    if (original is None) != (adjusted is None) or original is not None and abs(original*pf-adjusted)>Decimal("0.06"):
                        bad.append(code)
                if (vo is None) != (avo is None) or vo is not None and abs(Decimal(vo)*vf-avo)>Decimal(1):
                    bad.append(code)
            except PriceBasisError:
                bad.append(code)
        cur.execute("""SELECT code,action_digest FROM price_adjustment_state WHERE status=%s""", ("published",))
        for code, digest in cur.fetchall():
            if digest != actions_digest(actions.get(code, []), jst_today()):
                bad.append(code)
        for code in set(bad):
            hold(code, "分割台帳と公開価格の係数が一致しません。再生成待ちです", cur)
        cur.execute("SELECT code FROM price_adjustment_state WHERE status=%s", ("held",))
        bad.extend(r[0] for r in cur.fetchall())
    return {"held": sorted(set(bad))}


def refresh_derived() -> None:
    """価格再公開後の派生値を更新。通知・AI売買は実行しない。"""
    from fundamentals import recompute_price_metrics
    from compute_price_stats import run as compute_stats
    from compute_theoretical import run as theoretical
    from rankings import compute_daily_rankings, compute_weekly_rankings
    from theme_score import compute_day
    from money_flow import compute as compute_money_flow
    from compute_stats_history import run as snapshot_stats
    recompute_price_metrics()
    compute_stats()
    theoretical()
    compute_daily_rankings()
    compute_weekly_rankings()
    compute_day()
    compute_money_flow()
    snapshot_stats(latest_only=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--sync-actions", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="価格に依存する派生指標だけを再計算")
    parser.add_argument("--codes", nargs="+")
    args = parser.parse_args()
    ensure_schema()
    errors = []
    refreshed = set()
    if args.sync_actions:
        from corporate_actions import sync_official_actions
        result = sync_official_actions(history=args.history)
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        errors += result["errors"]
    if args.audit:
        from price_verification import run_audit, audit_date
        result = run_audit()
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        pending = sorted(set(result["mismatches"]) | set(check_integrity()["held"]))
        if pending:
            with db() as cur:
                cur.execute("""SELECT s.code FROM price_adjustment_state s LEFT JOIN watchlist w ON w.code=s.code
                               WHERE s.status=%s ORDER BY (w.code IS NOT NULL) DESC,
                               COALESCE(s.last_attempt_at,'1900-01-01'),s.code LIMIT %s""", ("held", PRICE_NIGHTLY_REPAIR_CODES))
                queued = [r[0] for r in cur.fetchall()]
            repaired = repair_codes(queued)
            refreshed.update(repaired["repaired"])
            errors += list(repaired["errors"].values())
            # 修復後も同じ公式応答で再照合。失敗した日付は翌晩の対象に残す。
            for checked in result["results"]:
                if checked["mismatches"]:
                    verified = audit_date(day(checked["date"]), refetch=False)
                    print(json.dumps(verified, ensure_ascii=False, default=str), flush=True)
                    errors += verified["mismatches"]
    if args.repair:
        result = repair_codes(args.codes)
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        errors += list(result["errors"].values())
        refreshed.update(result["repaired"])
    if refreshed or args.refresh:
        refresh_derived()
    if args.check:
        result = check_integrity()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        errors += result["held"]
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
