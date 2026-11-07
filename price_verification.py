"""J-Quants Freeの12週遅延データによる、再開可能な価格の公式照合。"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import time

import requests

from batch_dates import jst_today
from config import (db, bulk_upsert, JQUANTS_BASE_URL, JQUANTS_HEADERS,
                    PRICE_JQUANTS_DELAY_DAYS, PRICE_JQUANTS_REQUEST_INTERVAL,
                    PRICE_JQUANTS_AUDIT_DATES, PRICE_JQUANTS_BACKOFF_SECONDS)
from price_adjustments import Action, matches, number
from price_store import ensure_schema, hold, put_action, snapshot

def reserve_request_slot() -> None:
    """別プロセス・日次/夜間便も含めFreeの呼出間隔を共有する。"""
    while True:
        with db() as cur:
            cur.execute("INSERT IGNORE INTO price_sync_state (name,value) VALUES (%s,%s),(%s,%s)",
                        ("jquants_rate_limit", "0", "jquants_backoff", "0"))
            cur.execute("SELECT value FROM price_sync_state WHERE name=%s FOR UPDATE", ("jquants_rate_limit",))
            previous = float(cur.fetchone()[0] or 0)
            cur.execute("SELECT value FROM price_sync_state WHERE name=%s FOR UPDATE", ("jquants_backoff",))
            cooldown = float(cur.fetchone()[0] or 0)
            cur.execute("SELECT UNIX_TIMESTAMP(UTC_TIMESTAMP(6))")
            now = float(cur.fetchone()[0])
            slot = max(now, previous, cooldown)
            cur.execute("UPDATE price_sync_state SET value=%s WHERE name=%s", (str(slot + PRICE_JQUANTS_REQUEST_INTERVAL), "jquants_rate_limit"))
        if slot > now:
            time.sleep(slot - now)
        with db() as cur:
            cur.execute("SELECT value FROM price_sync_state WHERE name=%s", ("jquants_backoff",))
            deadline = float(cur.fetchone()[0] or 0)
        if deadline <= slot:
            return
        # 待機中に別便が429になった場合、古い予約時刻でリクエストを送らない。


def defer_requests(seconds: float = PRICE_JQUANTS_BACKOFF_SECONDS) -> None:
    with db() as cur:
        cur.execute("SELECT UNIX_TIMESTAMP(UTC_TIMESTAMP(6))")
        until = float(cur.fetchone()[0]) + seconds
        cur.execute("""INSERT INTO price_sync_state (name,value) VALUES (%s,%s)
                       ON DUPLICATE KEY UPDATE value=GREATEST(CAST(value AS DECIMAL(24,6)),VALUES(value))""",
                    ("jquants_backoff",str(until)))


def free_cutoff(today: date | None = None) -> date:
    # 契約境界の包含差を避け1日余裕を置く。後からその日も必ず対象になる。
    return (today or jst_today()) - timedelta(days=PRICE_JQUANTS_DELAY_DAYS + 1)


def free_start(today: date | None = None) -> date:
    return (today or jst_today()) - timedelta(days=365 * 2 - 3)


def fetch_bars(*, code: str | None = None, target: date | None = None,
               since: date | None = None, until: date | None = None) -> list[dict]:
    params = {"code": code} if code else {"date": str(target)}
    if code:
        params.update({"from": str(since), "to": str(until or free_cutoff())})
    output, seen = [], set()
    while True:
        response = None
        for attempt in range(3):
            reserve_request_slot()
            response = requests.get(JQUANTS_BASE_URL + "/equities/bars/daily",
                                    params=params, headers=JQUANTS_HEADERS, timeout=30)
            if response.status_code != 429:
                break
            defer_requests()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload.get("data"), list):
            raise RuntimeError("J-Quantsの価格レスポンスにdata配列がありません")
        output.extend(payload["data"])
        key = payload.get("pagination_key")
        if not key:
            break
        if key in seen:
            raise RuntimeError("J-Quantsのページングキーが循環しています")
        seen.add(key)
        params["pagination_key"] = key
    return output


def remember_bars(rows: list[dict], *, update_actions: bool = True) -> None:
    grouped = {}
    for r in rows:
        code = str(r["Code"])
        if len(code) == 5 and code.endswith("0"):
            code = code[:-1]
        r = {**r, "code": code, "date": r["Date"]}
        grouped.setdefault(code, []).append(r)
    with db() as cur:
        # 日付指定の全銘柄応答も原本の単位で保存。銘柄数分のINSERTをしない。
        flattened = [r for bars in grouped.values() for r in bars]
        snapshot(cur, next(iter(grouped)) if len(grouped) == 1 else "", "jquants", "raw_and_adjusted", flattened)
        values = [(r["code"], r["date"], r.get("O"), r.get("H"), r.get("L"), r.get("C"),
                   r.get("Vo"), r.get("AdjFactor"), r.get("AdjC"),r.get("AdjO"),r.get("AdjH"),r.get("AdjL"),r.get("AdjVo")) for bars in grouped.values() for r in bars]
        bulk_upsert(cur, "price_official_checks", ["code", "date", "open", "high", "low", "close", "volume", "official_factor", "official_adj_close",
                                                  "official_adj_open","official_adj_high","official_adj_low","official_adj_volume"], values)
    if not update_actions:
        return
    for code, bars in grouped.items():
        for r in bars:
            factor = number(r.get("AdjFactor"))
            if factor is None or factor == 1:
                continue
            kind = {"1": "split", "2": "consolidation", "3": "rights"}.get(str(r.get("ExRT")), "consolidation" if factor > 1 else "split")
            put_action(Action(code, date.fromisoformat(r["date"]), factor, Decimal(1), kind,
                              source="jquants", source_url="https://jpx-jquants.com/ja/spec/eq-bars-daily",
                              price_ratio=factor))


def anchors_for(code: str) -> dict[str, Decimal]:
    with db() as cur:
        cur.execute("SELECT date,close FROM price_official_checks WHERE code=%s AND close > 0", (code,))
        result = {str(d): c for d, c in cur.fetchall()}
        # 新方式で元の約定基準まで確認して保存した行も、翌日の再取得時の基準になる。
        cur.execute("SELECT date,close FROM daily_prices WHERE code=%s AND price_basis=%s AND close > 0", (code, "raw"))
        for d, c in cur.fetchall():
            result.setdefault(str(d), c)
    return result


def obtain_anchors(code: str, earliest: date) -> dict[str, Decimal]:
    cutoff = free_cutoff()
    if earliest > cutoff:
        return {}  # 新規上場直後等。公式値が存在しないのに取得を繰り返さない。
    earliest = max(earliest, free_start())
    rows = fetch_bars(code=code, since=earliest, until=cutoff)
    if not rows:
        raise RuntimeError(f"{code}: 照合に必要なJ-Quantsの価格が空です")
    remember_bars(rows)
    return anchors_for(code)


def seed_reference_dates() -> None:
    """移行時は全銘柄を2日分取得。銘柄ごとに同じAPIへ殺到しない。"""
    with db() as cur:
        cur.execute("SELECT MIN(date),MAX(date) FROM trading_calendar WHERE is_holiday=FALSE AND date BETWEEN %s AND %s", (free_start(), free_cutoff()))
        bounds = cur.fetchone()
        cur.execute("SELECT DISTINCT date FROM price_official_checks")
        existing = {r[0] for r in cur.fetchall()}
    for target in sorted({d for d in bounds if d} - existing):
        bars = fetch_bars(target=target)
        if not bars:
            raise RuntimeError(f"{target}: 公式の基準価格を取得できません")
        remember_bars(bars)
        print(f"  公式基準日 {target}: {len(bars)}件", flush=True)


def audit_date(target: date, *, refetch: bool = True) -> dict:
    if target > free_cutoff():
        raise ValueError("J-Quants Freeの取得可能日より新しい日付です")
    if refetch:
        bars = fetch_bars(target=target)
        if not bars:
            raise RuntimeError(f"{target}: 公式価格が0件でした。照合済みにはしません")
        remember_bars(bars)
    bad = []
    with db() as cur:
        cur.execute("""SELECT p.code,p.open,p.high,p.low,p.close,p.volume,
                              CASE WHEN p.price_basis='raw' THEN p.adj_close ELSE p.close END,
                              CASE WHEN p.price_basis='raw' THEN p.adj_open ELSE p.open END,
                              CASE WHEN p.price_basis='raw' THEN p.adj_high ELSE p.high END,
                              CASE WHEN p.price_basis='raw' THEN p.adj_low ELSE p.low END,
                              CASE WHEN p.price_basis='raw' THEN p.adj_volume ELSE p.volume END,
                              q.open,q.high,q.low,q.close,q.volume,q.official_adj_close,
                              q.official_adj_open,q.official_adj_high,q.official_adj_low,q.official_adj_volume
                       FROM daily_prices p JOIN price_official_checks q ON q.code=p.code AND q.date=p.date
                       WHERE p.date=%s""", (target,))
        records = cur.fetchall()
        if not records:
            raise RuntimeError(f"{target}: 照合対象が0件でした。照合済みにはしません")
        for code, op, hi, lo, cl, vo, adj, aop, ahi, alo, avo, qo, qh, ql, qc, qv, qa, qao, qah, qal, qav in records:
            differences = ["no_trade"] if qc is None and cl is not None else []
            for name, actual, expected in (("open", op, qo), ("high", hi, qh), ("low", lo, ql), ("close", cl, qc)):
                if expected is not None and (actual is None or not matches(actual, expected, Decimal("0.00001"), Decimal("0.06"))):
                    differences.append(name)
            if qv is not None and (vo is None or abs(vo - qv) > max(1, qv * Decimal("0.001"))):
                differences.append("volume")
            # AdjCは照合時点の基準。古い保存時点のAdjCとは比較しない。
            if qa is not None and (adj is None or not matches(adj, qa, Decimal("0.00001"), Decimal("0.06"))):
                differences.append("adjusted_close")
            for name, actual, expected in (("adjusted_open",aop,qao),("adjusted_high",ahi,qah),("adjusted_low",alo,qal)):
                if expected is not None and (actual is None or not matches(actual,expected,Decimal("0.00001"),Decimal("0.06"))):
                    differences.append(name)
            if qav is not None and (avo is None or not matches(avo,qav,Decimal("0.00001"),Decimal("1"))):
                differences.append("adjusted_volume")
            error = "公式値との不一致: " + ",".join(differences) if differences else None
            cur.execute("UPDATE price_official_checks SET status=%s,error=%s,checked_at=CURRENT_TIMESTAMP WHERE code=%s AND date=%s",
                        ("mismatch" if error else "matched", error, code, target))
            if error:
                bad.append(code)
                hold(code, f"{target} {error}", cur)
        cur.execute("""INSERT INTO price_sync_state (name,value,error) VALUES (%s,%s,NULL)
                       ON DUPLICATE KEY UPDATE value=VALUES(value),error=VALUES(error),checked_at=CURRENT_TIMESTAMP""",
                    (f"jquants_date:{target}", str(len(records))))
        cur.execute("UPDATE price_sync_state SET error=%s WHERE name=%s",
                    (f"{len(bad)}銘柄が不一致" if bad else None, f"jquants_date:{target}"))
    return {"date": str(target), "checked": len(records), "mismatches": bad}


def run_audit(max_dates: int = PRICE_JQUANTS_AUDIT_DATES) -> dict:
    ensure_schema()
    with db() as cur:
        cur.execute("""SELECT DISTINCT p.date FROM daily_prices p
                       WHERE p.date BETWEEN %s AND %s AND NOT EXISTS (
                         SELECT 1 FROM price_sync_state s WHERE s.name=CONCAT(%s,p.date) AND s.error IS NULL)
                       ORDER BY p.date""", (free_start(), free_cutoff(), "jquants_date:"))
        dates = [r[0] for r in cur.fetchall()]
        cur.execute("""SELECT name FROM price_sync_state WHERE name LIKE %s AND error IS NOT NULL
                       AND name BETWEEN %s AND %s ORDER BY checked_at LIMIT %s""",
                    ("jquants_date:%", f"jquants_date:{free_start()}", f"jquants_date:{free_cutoff()}", max_dates))
        retry_dates = [date.fromisoformat(r[0].split(":",1)[1]) for r in cur.fetchall()]
        cur.execute("""SELECT name FROM price_sync_state WHERE name LIKE %s AND error IS NULL
                       AND checked_at < DATE_SUB(UTC_TIMESTAMP(),INTERVAL 30 DAY)
                       AND name BETWEEN %s AND %s ORDER BY checked_at LIMIT 1""",
                    ("jquants_date:%", f"jquants_date:{free_start()}", f"jquants_date:{free_cutoff()}"))
        stale = [date.fromisoformat(r[0].split(":",1)[1]) for r in cur.fetchall()]
    # 新たに12週を過ぎた日を優先し、残り枠で過去の未照合を順番に解消。
    selected = [d for d in dict.fromkeys(dates[-1:] + retry_dates[:1] + stale + dates)
                if free_start() <= d <= free_cutoff()][:max_dates]
    results = []
    for target in selected:
        results.append(audit_date(target))
    return {"cutoff": str(free_cutoff()), "results": results,
            "mismatches": sorted({c for r in results for c in r["mismatches"]})}
