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

from config import db, bulk_upsert, PRICE_READ_BATCH_CODES
from price_adjustments import (Action, PriceBasisError, StalePublication, actions_digest,
                               day, economic_kind, number, stable_action_id)

_ready = False
_generation_cache = (0.0, None)


class PricePublicationHeld(RuntimeError):
    """個別銘柄は公開せず、公開できた件数と失敗銘柄を呼出元へ返す。"""
    def __init__(self, published_rows: int, errors: dict[str,str]):
        self.published_rows = published_rows
        self.errors = errors
        super().__init__(f"価格公開を保留: {len(errors)}銘柄 / " + " | ".join(f"{c}: {e}" for c,e in list(errors.items())[:12]))


def watched_codes(codes) -> set[str]:
    if not codes:
        return set()
    with db() as cur:
        cur.execute("SELECT code FROM watchlist")
        return set(codes) & {r[0] for r in cur.fetchall()}


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
            action_id CHAR(64) NOT NULL,
            code VARCHAR(10) NOT NULL, ex_date DATE NOT NULL,
            kind VARCHAR(20) NOT NULL DEFAULT 'split',
            old_shares DECIMAL(28,12) NULL, new_shares DECIMAL(28,12) NULL,
            price_ratio DECIMAL(28,18) NULL, record_date DATE NULL, effective_date DATE NULL,
            announced_at DATE NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'needs_ratio',
            source VARCHAR(30) NOT NULL, source_url VARCHAR(2048) NOT NULL DEFAULT '',
            revision CHAR(64) NOT NULL, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (action_id), KEY idx_action_code_date (code, ex_date),
            KEY idx_action_due (status, ex_date)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS corporate_action_history (
            revision CHAR(64) PRIMARY KEY, code VARCHAR(10) NOT NULL, ex_date DATE NOT NULL,
            action_id CHAR(64) NULL, source_observation_key VARCHAR(255) NULL,
            recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, details JSON NOT NULL,
            KEY idx_action_history (code, ex_date)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS corporate_action_observations (
            observation_id CHAR(64) PRIMARY KEY, action_id CHAR(64) NOT NULL,
            code VARCHAR(10) NOT NULL, ex_date DATE NOT NULL,
            source VARCHAR(30) NOT NULL, source_observation_key VARCHAR(255) NOT NULL,
            observed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            payload JSON NOT NULL,
            UNIQUE KEY uq_action_observation (source, source_observation_key),
            KEY idx_action_observation_action (action_id, observed_at)
        )""")
        _migrate_action_schema(cur)
        _resolve_duplicate_pending_actions(cur)
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
            official_adj_open DECIMAL(18,4) NULL, official_adj_high DECIMAL(18,4) NULL,
            official_adj_low DECIMAL(18,4) NULL, official_adj_volume DECIMAL(24,4) NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'unchecked', error TEXT NULL,
            checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, date), KEY idx_official_check_date (date, status)
        )""")
        cur.execute("SHOW COLUMNS FROM price_official_checks")
        check_columns = {r[0] for r in cur.fetchall()}
        for name, definition in {"official_adj_open":"DECIMAL(18,4) NULL", "official_adj_high":"DECIMAL(18,4) NULL",
                                 "official_adj_low":"DECIMAL(18,4) NULL", "official_adj_volume":"DECIMAL(24,4) NULL",
                                 "reference_basis_date":"DATE NULL"}.items():
            if name not in check_columns:
                cur.execute(f"ALTER TABLE price_official_checks ADD COLUMN `{name}` {definition}")
        cur.execute("""CREATE TABLE IF NOT EXISTS price_sync_state (
            name VARCHAR(160) PRIMARY KEY, checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            value TEXT NULL, error TEXT NULL
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS data_coverage_state (
            code VARCHAR(10) NOT NULL, dataset VARCHAR(30) NOT NULL,
            coverage_status VARCHAR(24) NOT NULL, reason VARCHAR(255) NULL,
            source_observed_at DATETIME NULL, checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code,dataset), KEY idx_coverage_status (dataset,coverage_status)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS corporate_action_projection_state (
            code VARCHAR(10) NOT NULL, consumer VARCHAR(24) NOT NULL,
            required_digest CHAR(64) NULL, completed_digest CHAR(64) NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'scheduled', not_before DATETIME NULL,
            last_error TEXT NULL, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code,consumer), KEY idx_projection_pending (status,not_before)
        )""")
        # J-Quants対象外市場は「失敗」ではなく適用外。通常市場の修復順を塞がない。
        cur.execute("SHOW TABLES LIKE 'stock_listing_history'")
        if cur.fetchone():
            cur.execute("""INSERT INTO data_coverage_state
                (code,dataset,coverage_status,reason,source_observed_at,checked_at)
                SELECT DISTINCT code,'jquants_price','not_applicable','TOKYO_PRO_MARKET',
                       MAX(source_observed_at),UTC_TIMESTAMP()
                FROM stock_listing_history
                WHERE delisting_date IS NULL AND UPPER(COALESCE(market_segment,'')) LIKE '%PRO%'
                GROUP BY code
                ON DUPLICATE KEY UPDATE coverage_status=VALUES(coverage_status),reason=VALUES(reason),
                                        source_observed_at=VALUES(source_observed_at),checked_at=VALUES(checked_at)""")
            cur.execute("""UPDATE price_adjustment_state p
                JOIN data_coverage_state d ON d.code=p.code AND d.dataset='jquants_price'
                SET p.status='not_applicable',p.error='J-Quants対象外: TOKYO PRO MARKET',
                    p.updated_at=UTC_TIMESTAMP()
                WHERE d.coverage_status='not_applicable' AND p.status='held'""")
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
        # 複数回分割の累積係数を丸め落とさない。既存値はそのまま拡幅する。
        cur.execute("SHOW COLUMNS FROM daily_prices")
        daily_types = {r[0]: str(r[1]).lower() for r in cur.fetchall()}
        if daily_types.get("adj_factor") != "decimal(28,18)":
            cur.execute("ALTER TABLE daily_prices MODIFY COLUMN adj_factor DECIMAL(28,18) NULL")
        cur.execute("SHOW COLUMNS FROM price_adjustment_state")
        state_columns = {r[0] for r in cur.fetchall()}
        if "source_observed_at" not in state_columns:
            cur.execute("ALTER TABLE price_adjustment_state ADD COLUMN source_observed_at DATETIME(6) NULL")
        if "last_attempt_at" not in state_columns:
            cur.execute("ALTER TABLE price_adjustment_state ADD COLUMN last_attempt_at DATETIME NULL")
        if "series_revision" not in state_columns:
            # 初回移行: 再生成済みの系列と古い週次指標を区別する。原本は変更しない。
            cur.execute("ALTER TABLE price_adjustment_state ADD COLUMN series_revision CHAR(64) NULL")
            cur.execute("UPDATE price_adjustment_state SET series_revision=published_version WHERE published_version IS NOT NULL")
        if "published_from" not in state_columns:
            cur.execute("ALTER TABLE price_adjustment_state ADD COLUMN published_from DATE NULL")
        if "target_basis_date" not in state_columns:
            cur.execute("ALTER TABLE price_adjustment_state ADD COLUMN target_basis_date DATE NULL")
        cur.execute("""UPDATE price_adjustment_state
                       SET target_basis_date=DATE(UTC_TIMESTAMP()+INTERVAL 9 HOUR)
                       WHERE published_version IS NOT NULL AND target_basis_date IS NULL""")
        # digestの包含列追加は係数を変えないメタデータ移行。
        # 公開OHLCVを再補正せず、同じ台帳行から新digestだけを記録する。
        action_map = load_actions(cur)
        cur.execute("""SELECT code,target_basis_date FROM price_adjustment_state
                       WHERE status IN ('published','audit_due') AND target_basis_date IS NOT NULL""")
        for state_code, target_date in cur.fetchall():
            if state_code in action_map:
                cur.execute("UPDATE price_adjustment_state SET action_digest=%s WHERE code=%s",
                            (actions_digest(action_map[state_code], target_date), state_code))
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
            LEFT JOIN (SELECT code FROM corporate_actions
                       WHERE status<>'cancelled' AND ex_date <= DATE(UTC_TIMESTAMP()+INTERVAL 9 HOUR)
                       GROUP BY code) due ON due.code=p.code
            WHERE (s.status IS NULL OR s.status IN ('legacy','published','audit_due'))
              AND (s.published_from IS NULL OR p.date >= s.published_from)
              AND (p.price_basis='raw' OR due.code IS NULL)
        """)
        cur.execute("""CREATE OR REPLACE VIEW raw_price_history AS
            SELECT id,code,date,raw_open AS open,raw_high AS high,raw_low AS low,
                   raw_close AS close,raw_volume AS volume,turnover,adj_close,adj_factor,
                   change_pct,is_upper_limit,is_lower_limit,price_version,price_basis
            FROM price_history""")
    _ready = True


def _migrate_action_schema(cur) -> None:
    """旧 `(code, ex_date)` PKを事象IDに移行する。原本と履歴は削除しない。"""
    cur.execute("SHOW COLUMNS FROM corporate_actions")
    columns = {r[0] for r in cur.fetchall()}
    if "action_id" not in columns:
        cur.execute("ALTER TABLE corporate_actions ADD COLUMN action_id CHAR(64) NULL FIRST")
    if "announced_at" not in columns:
        cur.execute("ALTER TABLE corporate_actions ADD COLUMN announced_at DATE NULL AFTER effective_date")
    cur.execute("""SELECT code,ex_date,old_shares,new_shares,kind,status,source,source_url,
                          record_date,effective_date,price_ratio,action_id,announced_at
                   FROM corporate_actions""")
    for code, ex, old, new, kind, status, source, url, record, effective, ratio, action_id, announced in cur.fetchall():
        if action_id:
            continue
        action = Action(code, ex, old or Decimal(1), new or Decimal(1), kind, status,
                        source, url, record, effective, ratio, None, announced)
        cur.execute("UPDATE corporate_actions SET action_id=%s WHERE code=%s AND ex_date=%s",
                    (stable_action_id(action), code, ex))
    cur.execute("SHOW COLUMNS FROM corporate_actions")
    action_id_type = {r[0]: (str(r[1]).lower(), str(r[2]).upper()) for r in cur.fetchall()}.get("action_id")
    if action_id_type != ("char(64)", "NO"):
        cur.execute("ALTER TABLE corporate_actions MODIFY COLUMN action_id CHAR(64) NOT NULL")
    cur.execute("SHOW INDEX FROM corporate_actions WHERE Key_name='PRIMARY'")
    primary_columns = [r[4] for r in cur.fetchall()]
    if primary_columns != ["action_id"]:
        # TiDBのクラスタ化主キーはDROPできない。旧表を直接作り替えず、
        # action_id主キーの新表へ全件コピー→件数検証→原子的RENAMEし、
        # 旧表は復旧用バックアップとして残す。
        migration_table = "corporate_actions_action_id_migration"
        backup_table = "corporate_actions_legacy_pk"
        cur.execute(f"""CREATE TABLE IF NOT EXISTS `{migration_table}` (
            action_id CHAR(64) NOT NULL,
            code VARCHAR(10) NOT NULL, ex_date DATE NOT NULL,
            kind VARCHAR(20) NOT NULL DEFAULT 'split',
            old_shares DECIMAL(28,12) NULL, new_shares DECIMAL(28,12) NULL,
            price_ratio DECIMAL(28,18) NULL, record_date DATE NULL,
            effective_date DATE NULL, announced_at DATE NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'needs_ratio',
            source VARCHAR(30) NOT NULL, source_url VARCHAR(2048) NOT NULL DEFAULT '',
            revision CHAR(64) NOT NULL, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (action_id), KEY idx_action_code_date (code,ex_date),
            KEY idx_action_due (status,ex_date)
        )""")
        cur.execute(f"""INSERT INTO `{migration_table}`
            (action_id,code,ex_date,kind,old_shares,new_shares,price_ratio,record_date,
             effective_date,announced_at,status,source,source_url,revision,updated_at)
            SELECT action_id,code,ex_date,kind,old_shares,new_shares,price_ratio,record_date,
                   effective_date,announced_at,status,source,source_url,revision,updated_at
            FROM corporate_actions
            ON DUPLICATE KEY UPDATE code=VALUES(code),ex_date=VALUES(ex_date),
              kind=VALUES(kind),old_shares=VALUES(old_shares),new_shares=VALUES(new_shares),
              price_ratio=VALUES(price_ratio),record_date=VALUES(record_date),
              effective_date=VALUES(effective_date),announced_at=VALUES(announced_at),
              status=VALUES(status),source=VALUES(source),source_url=VALUES(source_url),
              revision=VALUES(revision),updated_at=VALUES(updated_at)""")
        cur.execute("SELECT COUNT(*) FROM corporate_actions")
        source_count = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM `{migration_table}`")
        migrated_count = cur.fetchone()[0]
        if source_count != migrated_count:
            raise RuntimeError(
                f"権利台帳の主キー移行件数が不一致です: {source_count} != {migrated_count}"
            )
        cur.execute("SHOW TABLES LIKE %s", (backup_table,))
        if cur.fetchone():
            raise RuntimeError(f"復旧用旧表 `{backup_table}` がすでに存在します")
        cur.execute(
            f"RENAME TABLE corporate_actions TO `{backup_table}`, "
            f"`{migration_table}` TO corporate_actions"
        )
    cur.execute("SHOW INDEX FROM corporate_actions")
    indexes = {r[2] for r in cur.fetchall()}
    if "idx_action_code_date" not in indexes:
        cur.execute("ALTER TABLE corporate_actions ADD KEY idx_action_code_date (code,ex_date)")

    cur.execute("SHOW COLUMNS FROM corporate_action_history")
    history_columns = {r[0] for r in cur.fetchall()}
    if "action_id" not in history_columns:
        cur.execute("ALTER TABLE corporate_action_history ADD COLUMN action_id CHAR(64) NULL AFTER ex_date")
    if "source_observation_key" not in history_columns:
        cur.execute("ALTER TABLE corporate_action_history ADD COLUMN source_observation_key VARCHAR(255) NULL AFTER action_id")


def _resolve_duplicate_pending_actions(cur) -> None:
    """確定済み正本の後に再取得した「比率未確定の予定表」だけを統合する。

    同日でも別種・別基準日の事象は残す。取得原本と改訂履歴は消さず、
    observationsの参照先を確定済みaction_idへ付け替える。
    """
    cur.execute("""SELECT action_id,code,ex_date,kind,old_shares,new_shares,record_date,status
                   FROM corporate_actions
                   WHERE status IN ('confirmed','needs_ratio')
                   ORDER BY code,ex_date,action_id""")
    rows = list(cur.fetchall())
    confirmed = defaultdict(list)
    for row in rows:
        if row[7] == "confirmed":
            confirmed[(row[1], row[2], economic_kind(row[3]))].append(row)
    for pending in rows:
        if (pending[7] != "needs_ratio" or pending[4] != pending[5]
                or pending[4] not in (None, Decimal(1))):
            continue
        matches = [row for row in confirmed.get(
            (pending[1], pending[2], economic_kind(pending[3])), []
        ) if pending[6] is None or row[6] is None or pending[6] == row[6]]
        if len(matches) != 1:
            continue
        canonical_id = matches[0][0]
        cur.execute("UPDATE corporate_action_observations SET action_id=%s WHERE action_id=%s",
                    (canonical_id, pending[0]))
        cur.execute("DELETE FROM corporate_actions WHERE action_id=%s AND status='needs_ratio'",
                    (pending[0],))


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
    # テスト用の旧スキーマも読めるが、本番はensure_schema後に必ずID付きとなる。
    cur.execute("SELECT * FROM corporate_actions LIMIT 0")
    description = getattr(cur, "description", None)
    if not isinstance(description, (list, tuple)) and hasattr(cur, "cur"):
        description = cur.cur.description
    if not isinstance(description, (list, tuple)):
        description = ()
    columns = {d[0] for d in (description or ())}
    action_id_sql = "action_id" if "action_id" in columns else "NULL AS action_id"
    announced_sql = "announced_at" if "announced_at" in columns else "NULL AS announced_at"
    cur.execute(f"""SELECT code,ex_date,old_shares,new_shares,kind,status,source,source_url,
                          record_date,effective_date,price_ratio,{action_id_sql},{announced_sql}
                   FROM corporate_actions"""
                + (" WHERE code=%s" if code else "") + " ORDER BY code,ex_date", (code,) if code else ())
    result = defaultdict(list)
    for c, ex, old, new, kind, status, src, url, rec, eff, ratio, action_id, announced in cur.fetchall():
        result[c].append(Action(c, ex, old or Decimal(1), new or Decimal(1), kind, status,
                                src, url, rec, eff, ratio, action_id, announced))
    return dict(result)


def put_action(action: Action, *, correction: bool = False) -> bool:
    """公式イベントの採用。段差検証と独立し、競合する公式値は保留する。"""
    if action.source not in {"jpx_schedule", "jpx_monthly", "tdnet", "jquants"}:
        raise ValueError("分割の確定には公式出典が必要です")
    details = {k: str(v) if isinstance(v, (date, Decimal)) else v for k, v in vars(action).items()}
    revision = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
    with db() as cur:
        lock_code(cur, action.code)
        identifier = action.action_id or stable_action_id(action)
        # 同一日でも比率が異なる経済事象は別行。split/bonusの呼称差は同じ行へ束ねる。
        cur.execute("""SELECT action_id,old_shares,new_shares,status,revision,source,kind,price_ratio,
                              record_date,effective_date,announced_at FROM corporate_actions
                       WHERE code=%s AND ex_date=%s FOR UPDATE""", (action.code, action.ex_date))
        candidates = cur.fetchall()
        old = None
        placeholder_matches = [candidate for candidate in candidates
            if action.status == "needs_ratio" and candidate[3] == "confirmed"
            and economic_kind(candidate[6]) == economic_kind(action.kind)
            and (candidate[8] is None or action.record_date is None
                 or candidate[8] == action.record_date)]
        for candidate in candidates:
            old_ratio = (candidate[7] if candidate[7] is not None else
                         candidate[1] / candidate[2] if candidate[1] and candidate[2] else None)
            same_ratio = old_ratio is not None and abs(old_ratio - action.price_factor) <= Decimal("0.000002")
            same_pending = candidate[3] != "confirmed" and (
                candidate[8] is None or action.record_date is None or candidate[8] == action.record_date
            )
            if candidate[0] == identifier or (
                economic_kind(candidate[6]) == economic_kind(action.kind) and (same_ratio or same_pending)
            ) or (
                len(placeholder_matches) == 1 and candidate[0] == placeholder_matches[0][0]
            ) or (
                correction and len(candidates) == 1
                and economic_kind(candidate[6]) == economic_kind(action.kind)
                and (candidate[8] is None or action.record_date is None or candidate[8] == action.record_date)
            ):
                old = candidate
                identifier = candidate[0]
                break
        if old and old[4] == revision:
            return False
        if old and old[3] == "confirmed" and action.status == "needs_ratio":
            observation_key = f"{revision}|{action.source_url}" if action.source_url else revision
            observation_id = hashlib.sha256(f"{action.source}|{observation_key}".encode()).hexdigest()
            cur.execute("""INSERT IGNORE INTO corporate_action_observations
                (observation_id,action_id,code,ex_date,source,source_observation_key,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (observation_id, identifier, action.code, action.ex_date, action.source,
                 observation_key[:255], json.dumps(details, ensure_ascii=False)))
            return False
        if old and old[3] == "cancelled" and not correction:
            return False
        status = action.status
        if old and old[3] == "confirmed" and action.status == "confirmed" and not correction:
            old_ratio = old[7] if old[7] is not None else old[1] / old[2]
            if abs(old_ratio - action.price_factor) > Decimal("0.000002"):
                status = "conflict"
            elif economic_kind(old[6]) == economic_kind(action.kind):
                # 同一イベントを別出典で確認できても、1/3を0.333333へ劣化させない。
                # ただし観測原本は下のobservationsへ必ず残す。
                observation_key = f"{revision}|{action.source_url}" if action.source_url else revision
                observation_id = hashlib.sha256(f"{action.source}|{observation_key}".encode()).hexdigest()
                cur.execute("""INSERT IGNORE INTO corporate_action_observations
                    (observation_id,action_id,code,ex_date,source,source_observation_key,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (observation_id, identifier, action.code, action.ex_date, action.source,
                     observation_key[:255], json.dumps(details, ensure_ascii=False)))
                enriches_boundary = (
                    (old[8] is None and action.record_date is not None)
                    or (old[9] is None and action.effective_date is not None)
                    or (old[10] is None and action.announced_at is not None)
                )
                if not enriches_boundary:
                    return False
        old_ratio = (old[7] if old and old[7] is not None else
                     old[1] / old[2] if old and old[1] and old[2] else None)
        metadata_only = bool(
            old and old[3] == status
            and economic_kind(old[6]) == economic_kind(action.kind)
            and old_ratio is not None
            and abs(old_ratio - action.price_factor) <= Decimal("0.000002")
        )
        merged_record = action.record_date or (old[8] if old else None)
        merged_effective = action.effective_date or (old[9] if old else None)
        announced_values = [value for value in (action.announced_at, old[10] if old else None) if value]
        merged_announced = min(announced_values) if announced_values else None
        observation_key = f"{revision}|{action.source_url}" if action.source_url else revision
        observation_id = hashlib.sha256(f"{action.source}|{observation_key}".encode()).hexdigest()
        cur.execute("""INSERT IGNORE INTO corporate_action_observations
            (observation_id,action_id,code,ex_date,source,source_observation_key,payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (observation_id, identifier, action.code, action.ex_date, action.source,
             observation_key[:255], json.dumps(details, ensure_ascii=False)))
        cur.execute("""INSERT IGNORE INTO corporate_action_history
            (revision,code,ex_date,action_id,source_observation_key,details)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (revision, action.code, action.ex_date, identifier, observation_key[:255],
             json.dumps(details, ensure_ascii=False)))
        bulk_upsert(cur, "corporate_actions",
                    ["action_id", "code", "ex_date", "kind", "old_shares", "new_shares", "price_ratio", "record_date", "effective_date", "announced_at", "status", "source", "source_url", "revision", "updated_at"],
                    [(identifier, action.code, action.ex_date, action.kind, action.before, action.after,
                      action.price_ratio, merged_record, merged_effective, merged_announced, status,
                      action.source, action.source_url, revision, datetime.now(timezone.utc).replace(tzinfo=None))])
        if status == "confirmed":
            cur.execute("""SELECT old_shares,new_shares,price_ratio,kind FROM corporate_actions
                           WHERE code=%s AND ex_date=%s AND status='confirmed'""",
                        (action.code, action.ex_date))
            combined = Decimal(1)
            for before, after, ratio, kind in cur.fetchall():
                combined *= ratio if ratio is not None else before / after
            bulk_upsert(cur, "stock_splits", ["code", "ex_date", "split_ratio", "source"],
                        [(action.code, action.ex_date, combined, action.source)])
        elif status in {"conflict", "cancelled"}:
            cur.execute("""SELECT old_shares,new_shares,price_ratio FROM corporate_actions
                           WHERE code=%s AND ex_date=%s AND status='confirmed'""", (action.code, action.ex_date))
            remaining = cur.fetchall()
            if remaining:
                combined = Decimal(1)
                for before, after, ratio in remaining:
                    combined *= ratio if ratio is not None else before / after
                bulk_upsert(cur, "stock_splits", ["code", "ex_date", "split_ratio", "source"],
                            [(action.code, action.ex_date, combined, action.source)])
            else:
                cur.execute("DELETE FROM stock_splits WHERE code=%s AND ex_date=%s", (action.code, action.ex_date))
        from batch_dates import jst_today
        if action.ex_date <= jst_today():
            if metadata_only:
                # 公表日・基準日の補完はDPSの基準判定を変えるが、
                # OHLCVの係数は変えない。チャートを保留せず新digestへ移し、
                # 財務派生値だけを再生成待ちにする。
                cur.execute("""SELECT status,target_basis_date FROM price_adjustment_state
                               WHERE code=%s""", (action.code,))
                price_state = cur.fetchone()
                if price_state and price_state[0] in {"published", "audit_due"} and price_state[1]:
                    new_digest = actions_digest(
                        load_actions(cur, action.code).get(action.code, []), price_state[1]
                    )
                    cur.execute("UPDATE price_adjustment_state SET action_digest=%s WHERE code=%s",
                                (new_digest, action.code))
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    bulk_upsert(cur, "corporate_action_projection_state",
                                ["code","consumer","required_digest","completed_digest",
                                 "status","last_error","updated_at"],
                                [(action.code,"price",new_digest,new_digest,"complete",None,now)])
                    for consumer in ("per_share", "dividend", "valuation"):
                        bulk_upsert(cur, "corporate_action_projection_state",
                                    ["code","consumer","required_digest","status",
                                     "last_error","updated_at"],
                                    [(action.code,consumer,new_digest,"scheduled",None,now)])
                    bump_generation(cur)
            else:
                hold(action.code, "公式の権利情報が更新されました。価格系列の再生成待ちです", cur)
        # 公開済みの台帳とは異なることだけ記録。将来分は価格を止めない。
    return True


def hold(code: str, message: str, cur=None, *, attempted: bool = False) -> None:
    if cur is None:
        with db() as own:
            return hold(code, message, own, attempted=attempted)
    bulk_upsert(cur, "price_adjustment_state", ["code", "status", "error", "updated_at"],
                [(code, "held", message[:4000], datetime.now(timezone.utc).replace(tzinfo=None))])
    if not hasattr(cur, "cur"):  # SQLite互換テストは旧最小スキーマ。
        bulk_upsert(cur, "corporate_action_projection_state",
                    ["code","consumer","status","last_error","updated_at"],
                    [(code,"price","pending",message[:4000],datetime.now(timezone.utc).replace(tzinfo=None))])
    bump_generation(cur)
    if attempted:
        cur.execute("UPDATE price_adjustment_state SET last_attempt_at=UTC_TIMESTAMP() WHERE code=%s", (code,))


def lock_code(cur, code: str) -> None:
    # 台帳更新と価格公開が同じ順序で同じ行をロックする。初回も競合可能。
    cur.execute("INSERT IGNORE INTO price_adjustment_state (code,status) VALUES (%s,%s)", (code, "legacy"))
    cur.execute("SELECT code FROM price_adjustment_state WHERE code=%s FOR UPDATE", (code,))
    cur.fetchone()


def bump_generation(cur, name: str = "publication_generation") -> None:
    # 公開トランザクションと同時に単調増加。ハッシュ和の衝突へ依存しない。
    cur.execute("""INSERT INTO price_sync_state (name,value) VALUES (%s,%s)
                   ON DUPLICATE KEY UPDATE value=CAST(value AS UNSIGNED)+1""",
                (name, "1"))


def publish(cur, code: str, rows: list[dict], actions: list[Action], basis_date: date,
            basis: str, snapshot_id: str, observed_at: datetime | None = None, *,
            rebuild: bool = False, bump_publication: bool = True) -> None:
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
    state_columns = ["code", "status", "action_digest", "published_version", "price_date"]
    state_values = [code, "published", digest, version, day(rows[-1]["date"])]
    cur.execute("SELECT * FROM price_adjustment_state LIMIT 0")
    description = getattr(cur, "description", None) or getattr(getattr(cur, "cur", None), "description", None)
    if "target_basis_date" in {d[0] for d in (description or ())}:
        state_columns.append("target_basis_date")
        state_values.append(basis_date)
    state_columns += ["basis", "error", "updated_at", "source_observed_at"]
    state_values += [basis, None, datetime.now(timezone.utc).replace(tzinfo=None), observed_at]
    bulk_upsert(cur, "price_adjustment_state", state_columns, [tuple(state_values)])
    if not hasattr(cur, "cur"):
        bulk_upsert(cur, "corporate_action_projection_state",
                    ["code","consumer","required_digest","completed_digest","status","last_error","updated_at"],
                    [(code,"price",digest,digest,"complete",None,
                      datetime.now(timezone.utc).replace(tzinfo=None))])
    # 日々の追記では維持。過去の再生成では週次指標を無効化し、次便で全週を再計算する。
    cur.execute("""UPDATE price_adjustment_state SET series_revision=%s
                   WHERE code=%s AND (series_revision IS NULL OR %s)""", (version,code,rebuild))
    if rebuild:
        cur.execute("UPDATE price_adjustment_state SET published_from=%s WHERE code=%s",(day(rows[0]['date']),code))
        bump_generation(cur,"historical_prices_revision")
    if bump_publication:
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


def iter_price_rows(cur, fields: tuple[str,...], *, since=None, source="price_history",
                    positive_only: bool = True, batch_size: int = PRICE_READ_BATCH_CODES):
    """全銘柄の長期バッチも100銘柄ずつ読む。TiDBで数百万行を一括ソートしない。"""
    allowed = {"code","date","open","high","low","close","volume","raw_open","raw_high","raw_low",
               "raw_close","raw_volume","adj_open","adj_high","adj_low","adj_close","adj_volume",
               "adj_factor","price_basis","price_version","change_pct","turnover"}
    if source not in {"price_history","daily_prices"} or not fields or not set(fields) <= allowed:
        raise ValueError("価格読取のテーブル・カラムが許可されていません")
    if batch_size < 1:
        raise ValueError("読取バッチは1銘柄以上が必要です")
    cur.execute("SELECT DISTINCT code FROM daily_prices" + (" WHERE date >= %s" if since else "") + " ORDER BY code",
                (since,) if since else ())
    codes = [r[0] for r in cur.fetchall()]
    projection = ",".join(f"p.`{f}`" for f in fields)
    for start in range(0,len(codes),batch_size):
        batch = codes[start:start+batch_size]
        clauses,params=[],[]
        if since:
            clauses.append("p.date >= %s");params.append(since)
        clauses.append("p.code IN (" + ",".join(["%s"]*len(batch)) + ")");params.extend(batch)
        if source == "daily_prices":
            clauses.append("p.price_basis=%s");params.append("raw")
        if positive_only:
            clauses.append("p.close > 0")
        cur.execute(f"SELECT {projection} FROM {source} p WHERE " + " AND ".join(clauses) + " ORDER BY code, date",params)
        yield from cur.fetchall()


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
