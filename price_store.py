"""価格の取得原本、公式権利台帳、公開済みOHLCVの保存と共通読取。

初回移行: python price_pipeline.py --migrate
既存daily_pricesは残し、NULL許容列と監査テーブルを追加する。
保存前の旧系列は圧縮したlegacyスナップショットとして退避する。
公開更新は銘柄単位のトランザクション。Web処理でDDLを実行しない。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import zlib
import time

from config import db, bulk_upsert
from price_adjustments import Action, PriceBasisError, StalePublication, actions_digest, day, number

_ready = False
_generation_cache = (0.0, None)


def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    with db() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS price_source_snapshots (
            id CHAR(64) PRIMARY KEY, code VARCHAR(10) NOT NULL,
            source VARCHAR(30) NOT NULL, basis VARCHAR(30) NOT NULL,
            date_from DATE NULL, date_to DATE NULL, observed_at DATETIME NOT NULL,
            row_count INT NOT NULL DEFAULT 0, payload LONGBLOB NOT NULL,
            KEY idx_price_snapshot_code (code, observed_at)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS corporate_actions (
            code VARCHAR(10) NOT NULL, ex_date DATE NOT NULL,
            kind VARCHAR(20) NOT NULL DEFAULT 'split',
            old_shares DECIMAL(28,12) NULL, new_shares DECIMAL(28,12) NULL,
            price_ratio DECIMAL(28,18) NULL, record_date DATE NULL, effective_date DATE NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'needs_ratio',
            source VARCHAR(30) NOT NULL, source_url VARCHAR(2048) NOT NULL DEFAULT '',
            revision CHAR(64) NOT NULL, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, ex_date), KEY idx_action_due (status, ex_date)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS corporate_action_history (
            revision CHAR(64) PRIMARY KEY, code VARCHAR(10) NOT NULL, ex_date DATE NOT NULL,
            recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, details JSON NOT NULL,
            KEY idx_action_history (code, ex_date)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS price_adjustment_state (
            code VARCHAR(10) PRIMARY KEY, status VARCHAR(24) NOT NULL DEFAULT 'pending',
            action_digest CHAR(64) NULL, published_version CHAR(64) NULL,
            price_date DATE NULL, basis VARCHAR(30) NULL, error TEXT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS price_official_checks (
            code VARCHAR(10) NOT NULL, date DATE NOT NULL,
            open DECIMAL(18,4) NULL, high DECIMAL(18,4) NULL, low DECIMAL(18,4) NULL,
            close DECIMAL(18,4) NULL, volume BIGINT NULL,
            official_factor DECIMAL(28,18) NULL, official_adj_close DECIMAL(18,4) NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'unchecked', error TEXT NULL,
            checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, date), KEY idx_official_check_date (date, status)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS price_sync_state (
            name VARCHAR(160) PRIMARY KEY, checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            value TEXT NULL, error TEXT NULL
        )""")
        cur.execute("SHOW COLUMNS FROM daily_prices")
        columns = {r[0] for r in cur.fetchall()}
        additions = {
            "adj_open": "DECIMAL(18,4) NULL", "adj_high": "DECIMAL(18,4) NULL",
            "adj_low": "DECIMAL(18,4) NULL", "adj_volume": "DECIMAL(24,4) NULL",
            "price_basis": "VARCHAR(30) NULL DEFAULT NULL",
            "price_version": "CHAR(64) NULL DEFAULT NULL",
        }
        for name, definition in additions.items():
            if name not in columns:
                cur.execute(f"ALTER TABLE daily_prices ADD COLUMN `{name}` {definition}")
        cur.execute("SHOW COLUMNS FROM price_adjustment_state")
        state_columns = {r[0] for r in cur.fetchall()}
        if "source_observed_at" not in state_columns:
            cur.execute("ALTER TABLE price_adjustment_state ADD COLUMN source_observed_at DATETIME(6) NULL")
        if "last_attempt_at" not in state_columns:
            cur.execute("ALTER TABLE price_adjustment_state ADD COLUMN last_attempt_at DATETIME NULL")
        cur.execute("SHOW COLUMNS FROM price_stats")
        if "source_price_version" not in {r[0] for r in cur.fetchall()}:
            cur.execute("ALTER TABLE price_stats ADD COLUMN source_price_version CHAR(64) NULL")
        cur.execute("""CREATE OR REPLACE VIEW price_stats_current AS
            SELECT ps.* FROM price_stats ps LEFT JOIN price_adjustment_state s ON s.code=ps.code
            WHERE s.status IS NULL OR s.status='legacy'
               OR (s.status IN ('published','audit_due') AND ps.source_price_version=s.published_version)
        """)
        # 旧APIとの互換用。公式認定イベントだけを投影し、通知も同じ台帳を使う。
        cur.execute("""CREATE TABLE IF NOT EXISTS stock_splits (
            code VARCHAR(10) NOT NULL, ex_date DATE NOT NULL, split_ratio DECIMAL(12,6) NOT NULL,
            detected_at DATETIME DEFAULT CURRENT_TIMESTAMP, source VARCHAR(20) DEFAULT 'jquants',
            PRIMARY KEY (code, ex_date)
        )""")
        # 調整係数が不明のものを1扱いして正常表示しない。未移行行は既存adjで互換表示。
        cur.execute("""CREATE OR REPLACE VIEW price_history AS
            SELECT p.id, p.code, p.date,
                CASE WHEN p.price_basis='raw' THEN p.adj_open ELSE p.open END AS open,
                CASE WHEN p.price_basis='raw' THEN p.adj_high ELSE p.high END AS high,
                CASE WHEN p.price_basis='raw' THEN p.adj_low ELSE p.low END AS low,
                CASE WHEN p.price_basis='raw' THEN p.adj_close ELSE p.close END AS close,
                CASE WHEN p.price_basis='raw' THEN p.adj_volume ELSE p.volume END AS volume,
                p.open AS raw_open, p.high AS raw_high, p.low AS raw_low,
                p.close AS raw_close, p.volume AS raw_volume,
                COALESCE(p.turnover,p.close*p.volume) AS turnover,
                CASE WHEN p.price_basis='raw' THEN p.adj_close ELSE p.close END AS adj_close,
                CASE WHEN p.price_basis='raw' THEN p.adj_factor ELSE 1 END AS adj_factor,
                p.change_pct, p.is_upper_limit,p.is_lower_limit,
                p.price_version, p.price_basis
            FROM daily_prices p LEFT JOIN price_adjustment_state s ON s.code=p.code
            WHERE (s.status IS NULL OR s.status IN ('legacy','published','audit_due'))
              AND (p.price_basis='raw' OR NOT EXISTS (
                SELECT 1 FROM corporate_actions a WHERE a.code=p.code AND a.status<>'cancelled'
                  AND a.ex_date <= DATE(UTC_TIMESTAMP()+INTERVAL 9 HOUR)))
        """)
        cur.execute("""CREATE OR REPLACE VIEW raw_price_history AS
            SELECT id,code,date,raw_open AS open,raw_high AS high,raw_low AS low,
                   raw_close AS close,raw_volume AS volume,turnover,adj_close,adj_factor,
                   change_pct,is_upper_limit,is_lower_limit,price_version,price_basis
            FROM price_history""")
    _ready = True


def snapshot(cur, code: str, source: str, basis: str, rows: list[dict], **metadata) -> str:
    content = json.dumps({"rows": rows, **metadata}, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode()
    identifier = hashlib.sha256(code.encode() + source.encode() + content).hexdigest()
    dates = [str(r["date"]) for r in rows if r.get("date")]
    cur.execute("""INSERT IGNORE INTO price_source_snapshots
        (id,code,source,basis,date_from,date_to,observed_at,row_count,payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (identifier, code, source, basis, min(dates) if dates else None,
         max(dates) if dates else None, datetime.now(timezone.utc).replace(tzinfo=None),
         len(rows), zlib.compress(content)))
    return identifier


def backup_legacy(cur, code: str) -> None:
    cur.execute("SELECT 1 FROM price_source_snapshots WHERE code=%s AND source=%s LIMIT 1", (code, "legacy"))
    if cur.fetchone():
        return
    cur.execute("""SELECT date,open,high,low,close,volume,adj_close,adj_factor,change_pct,turnover
                   FROM daily_prices WHERE code=%s ORDER BY date""", (code,))
    keys = ("date", "open", "high", "low", "close", "volume", "adj_close", "adj_factor", "change_pct", "turnover")
    snapshot(cur, code, "legacy", "unknown", [dict(zip(keys, r)) for r in cur.fetchall()])


def load_actions(cur=None, code: str | None = None) -> dict[str, list[Action]]:
    if cur is None:
        with db() as own:
            return load_actions(own, code)
    cur.execute("""SELECT code,ex_date,old_shares,new_shares,kind,status,source,source_url,
                          record_date,effective_date,price_ratio FROM corporate_actions"""
                + (" WHERE code=%s" if code else "") + " ORDER BY code,ex_date", (code,) if code else ())
    result = defaultdict(list)
    for c, ex, old, new, kind, status, src, url, rec, eff, ratio in cur.fetchall():
        result[c].append(Action(c, ex, old or Decimal(1), new or Decimal(1), kind, status,
                                src, url, rec, eff, ratio))
    return dict(result)


def put_action(action: Action, *, correction: bool = False) -> bool:
    """公式イベントの採用。段差検証と独立し、競合する公式値は保留する。"""
    if action.source not in {"jpx_schedule", "jpx_monthly", "tdnet", "jquants"}:
        raise ValueError("分割の確定には公式出典が必要です")
    details = {k: str(v) if isinstance(v, (date, Decimal)) else v for k, v in vars(action).items()}
    revision = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
    with db() as cur:
        lock_code(cur, action.code)
        cur.execute("SELECT old_shares,new_shares,status,revision,source,kind,price_ratio FROM corporate_actions WHERE code=%s AND ex_date=%s FOR UPDATE", (action.code, action.ex_date))
        old = cur.fetchone()
        if old and old[3] == revision:
            return False
        if old and old[2] == "confirmed" and action.status == "needs_ratio":
            return False
        if old and old[2] == "cancelled" and not correction:
            return False
        status = action.status
        if old and old[2] == "confirmed" and action.status == "confirmed" and not correction:
            old_ratio = old[6] if old[6] is not None else old[0] / old[1]
            if abs(old_ratio - action.price_factor) > Decimal("0.000002"):
                status = "conflict"
            elif old[5] == action.kind:
                # 同一イベントを別出典で確認できても、1/3を0.333333へ劣化させない。
                # 全ての原文は取得原本に残り、先に確定した正確な比率を維持する。
                return False
        cur.execute("INSERT IGNORE INTO corporate_action_history (revision,code,ex_date,details) VALUES (%s,%s,%s,%s)",
                    (revision, action.code, action.ex_date, json.dumps(details, ensure_ascii=False)))
        bulk_upsert(cur, "corporate_actions",
                    ["code", "ex_date", "kind", "old_shares", "new_shares", "price_ratio", "record_date", "effective_date", "status", "source", "source_url", "revision", "updated_at"],
                    [(action.code, action.ex_date, action.kind, action.before, action.after,
                      action.price_ratio, action.record_date, action.effective_date, status,
                      action.source, action.source_url, revision, datetime.now(timezone.utc).replace(tzinfo=None))])
        if status == "confirmed":
            bulk_upsert(cur, "stock_splits", ["code", "ex_date", "split_ratio", "source"],
                        [(action.code, action.ex_date, action.price_factor, action.source)])
        elif status in {"conflict", "cancelled"}:
            cur.execute("DELETE FROM stock_splits WHERE code=%s AND ex_date=%s", (action.code, action.ex_date))
        from batch_dates import jst_today
        if action.ex_date <= jst_today():
            hold(action.code, "公式の権利情報が更新されました。価格系列の再生成待ちです", cur)
        # 公開済みの台帳とは異なることだけ記録。将来分は価格を止めない。
    return True


def hold(code: str, message: str, cur=None, *, attempted: bool = False) -> None:
    if cur is None:
        with db() as own:
            return hold(code, message, own, attempted=attempted)
    bulk_upsert(cur, "price_adjustment_state", ["code", "status", "error", "updated_at"],
                [(code, "held", message[:4000], datetime.now(timezone.utc).replace(tzinfo=None))])
    bump_generation(cur)
    if attempted:
        cur.execute("UPDATE price_adjustment_state SET last_attempt_at=UTC_TIMESTAMP() WHERE code=%s", (code,))


def lock_code(cur, code: str) -> None:
    # 台帳更新と価格公開が同じ順序で同じ行をロックする。初回も競合可能。
    cur.execute("INSERT IGNORE INTO price_adjustment_state (code,status) VALUES (%s,%s)", (code, "legacy"))
    cur.execute("SELECT code FROM price_adjustment_state WHERE code=%s FOR UPDATE", (code,))
    cur.fetchone()


def bump_generation(cur) -> None:
    # 公開トランザクションと同時に単調増加。ハッシュ和の衝突へ依存しない。
    cur.execute("""INSERT INTO price_sync_state (name,value) VALUES (%s,%s)
                   ON DUPLICATE KEY UPDATE value=CAST(value AS UNSIGNED)+1""",
                ("publication_generation", "1"))


def publish(cur, code: str, rows: list[dict], actions: list[Action], basis_date: date,
            basis: str, snapshot_id: str, observed_at: datetime | None = None) -> None:
    """呼出元の1トランザクション内で、価格・前日比・公開状態を同時更新。"""
    if not rows:
        raise PriceBasisError("公開する価格がありません")
    digest = actions_digest(actions, basis_date)
    version = hashlib.sha256((snapshot_id + digest).encode()).hexdigest()
    lock_code(cur, code)
    current_digest = actions_digest(load_actions(cur, code).get(code, []), basis_date)
    if current_digest != digest:
        raise PriceBasisError("取得中に公式台帳が更新されました。新しい台帳で再生成してください")
    cur.execute("SELECT price_date,source_observed_at FROM price_adjustment_state WHERE code=%s FOR UPDATE", (code,))
    prior = cur.fetchone()
    if prior and prior[0] and prior[0] > day(rows[-1]["date"]):
        raise StalePublication("新しい価格の公開後に古い取得結果が到着しました")
    if prior and prior[1] and observed_at and prior[1] > observed_at:
        raise StalePublication("先に公開された新しい取得結果を維持します")
    columns = ["code", "date", "open", "high", "low", "close", "volume", "turnover",
               "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume", "adj_factor",
               "change_pct", "price_basis", "price_version"]
    values = [(r["code"], r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"],
               r["turnover"], r["adj_open"], r["adj_high"], r["adj_low"], r["adj_close"],
               r["adj_volume"], r["factor"], r["change_pct"], "raw", version) for r in rows]
    bulk_upsert(cur, "daily_prices", columns, values, update_cols=columns[2:])
    bulk_upsert(cur, "price_adjustment_state",
                ["code", "status", "action_digest", "published_version", "price_date", "basis", "error", "updated_at", "source_observed_at"],
                [(code, "published", digest, version, day(rows[-1]["date"]), basis, None,
                  datetime.now(timezone.utc).replace(tzinfo=None), observed_at)])
    bump_generation(cur)


def load_candles(codes: list[str], since: date, cur=None) -> dict[str, list[dict]]:
    if not codes:
        return {}
    if cur is None:
        with db() as own:
            return load_candles(codes, since, own)
    placeholders = ",".join(["%s"] * len(codes))
    cur.execute(f"""SELECT code,date,open,high,low,close,volume FROM price_history
                    WHERE code IN ({placeholders}) AND date >= %s AND close > 0 ORDER BY code,date""",
                (*codes, since))
    result = defaultdict(list)
    for code, dt, op, hi, lo, close, volume in cur.fetchall():
        result[code].append({"date": str(dt), "open": float(op) if op is not None else None,
                             "high": float(hi) if hi is not None else None,
                             "low": float(lo) if lo is not None else None,
                             "close": float(close), "volume": float(volume) if volume is not None else None})
    return dict(result)


def state_for(code: str) -> dict:
    with db() as cur:
        cur.execute("SELECT status,price_date,error,published_version FROM price_adjustment_state WHERE code=%s", (code,))
        row = cur.fetchone()
    return dict(zip(("status", "price_date", "error", "version"), row)) if row else {"status": "audit_due", "version": "legacy"}


def publication_generation() -> str:
    """複数Webワーカーが共有する公開状態。5秒以内に価格依存キャッシュを失効。"""
    global _generation_cache
    now = time.monotonic()
    if _generation_cache[1] is not None and now - _generation_cache[0] < 5:
        return _generation_cache[1]
    with db() as cur:
        cur.execute("SELECT value FROM price_sync_state WHERE name=%s", ("publication_generation",))
        generation = str(cur.fetchone() or ("initial",))
    _generation_cache = (now, generation)
    return generation
