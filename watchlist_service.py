"""ウォッチリストの分類と価格リマインダー用DB操作。"""
from __future__ import annotations

import threading
import re
from urllib.parse import urlsplit
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from batch_dates import JST, MARKET_DATA_READY, jst_now
from config import (db, get_conn, STOCK_MEMO_MAX_LENGTH, STOCK_MEMO_MAX_BYTES,
                    STOCK_MEMO_TITLE_MAX_LENGTH, STOCK_MEMO_URL_MAX_LENGTH)


_schema_lock = threading.Lock()
_schema_ready = False


def ensure_schema() -> None:
    """追加専用のスキーマ移行。複数回実行しても既存データを変更しない。"""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = get_conn()
        cur = conn.cursor()
        try:
            # 初回: ensure_schema()（Web初回アクセスでも実行）で作成する。
            # stock_memos本体は変更せず、URL/見出しを付加テーブルへ分離する。
            # 旧メモ・旧アプリとの互換性を保ち、再実行でもデータを失わない。
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_memo_sources (
                    memo_id INT PRIMARY KEY,
                    title VARCHAR(200) NOT NULL DEFAULT '',
                    source_url VARCHAR(2048) NOT NULL DEFAULT ''
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watchlist_lists (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(40) NOT NULL,
                    sort_order INT NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_watchlist_list_name (name)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watchlist_list_items (
                    list_id INT NOT NULL,
                    code VARCHAR(10) NOT NULL,
                    added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (list_id, code),
                    KEY ix_watchlist_list_items_code (code)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    code VARCHAR(10) NOT NULL,
                    direction VARCHAR(5) NOT NULL,
                    target_price DECIMAL(14,4) NOT NULL,
                    effective_from DATE NOT NULL,
                    version INT NOT NULL DEFAULT 1,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_price_alert_code_direction (code, direction),
                    KEY ix_price_alert_status (status, effective_from)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_alert_events (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    alert_id INT NOT NULL,
                    alert_version INT NOT NULL,
                    code VARCHAR(10) NOT NULL,
                    direction VARCHAR(5) NOT NULL,
                    target_price DECIMAL(14,4) NOT NULL,
                    triggered_price DECIMAL(14,4) NOT NULL,
                    price_date DATE NOT NULL,
                    triggered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    acknowledged_at DATETIME NULL,
                    cancelled_at DATETIME NULL,
                    notification_batch_id CHAR(36) NULL,
                    notified_at DATETIME NULL,
                    UNIQUE KEY uq_price_alert_event (alert_id, alert_version),
                    KEY ix_price_alert_event_due (notified_at, cancelled_at, price_date)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_alert_notification_batches (
                    id CHAR(36) PRIMARY KEY,
                    retry_key CHAR(36) NOT NULL,
                    body TEXT NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    attempts INT NOT NULL DEFAULT 0,
                    last_error VARCHAR(500) NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at DATETIME NULL,
                    UNIQUE KEY uq_price_alert_retry_key (retry_key)
                )
            """)
            conn.commit()
            _schema_ready = True
        finally:
            cur.close()
            conn.close()


def parse_price(value: str | None) -> Decimal | None:
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        price = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("価格は数字で入力してください") from exc
    if not price.is_finite() or price <= 0 or price > Decimal("99999999"):
        raise ValueError("価格は0より大きい値で入力してください")
    return price.quantize(Decimal("0.0001"))


def next_effective_date(now: datetime | None = None) -> date:
    """設定後に初めて終値が確定する営業日。

    取引カレンダーに該当行が無い場合、土日だけで代用すると祝日を営業日と
    誤判定しうる（trading_calendarの正本はJ-Quants公式＋JPX休業日一覧。
    daily_run.pyが先読み180日を切ったら失敗させて気づける設計にしたので、
    ここで沈黙してフォールバックする必要はない）。取得できなければ例外を送出する。
    """
    current = (now or jst_now()).astimezone(JST)
    start = current.date() if current.time() < MARKET_DATA_READY else current.date() + timedelta(days=1)
    ensure_schema()
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT MIN(date) FROM trading_calendar
            WHERE date >= %s AND is_holiday = FALSE
        """, (start,))
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    finally:
        cur.close(); conn.close()
    raise ValueError(f"取引カレンダーが{start}以降で未登録です")


def set_memberships(code: str, list_ids: list[int]) -> None:
    ensure_schema()
    clean_ids = sorted({v for v in list_ids if v > 0})
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM watchlist_list_items WHERE code = %s", (code,))
        if clean_ids:
            placeholders = ",".join(["%s"] * len(clean_ids))
            cur.execute(f"SELECT id FROM watchlist_lists WHERE id IN ({placeholders})", clean_ids)
            valid = [r[0] for r in cur.fetchall()]
            if valid:
                cur.executemany(
                    "INSERT INTO watchlist_list_items (list_id, code) VALUES (%s, %s)",
                    [(list_id, code) for list_id in valid],
                )
        conn.commit()
    finally:
        cur.close(); conn.close()


def set_price_alerts(
    code: str,
    upper: Decimal | None,
    lower: Decimal | None,
    *,
    now: datetime | None = None,
    preserve: set[str] | None = None,
) -> date:
    if upper is not None and lower is not None and lower >= upper:
        raise ValueError("下値は上値より小さく設定してください")
    ensure_schema()
    effective = next_effective_date(now)
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM watchlist WHERE code = %s", (code,))
        if not cur.fetchone():
            raise ValueError("ウォッチリストに保存されていない銘柄です")
        for direction, target in (("upper", upper), ("lower", lower)):
            if direction in (preserve or set()):
                continue
            cur.execute(
                "SELECT id, version FROM price_alerts WHERE code = %s AND direction = %s FOR UPDATE",
                (code, direction),
            )
            old = cur.fetchone()
            if target is None:
                if old:
                    cur.execute("UPDATE price_alerts SET status = 'cancelled' WHERE id = %s", (old[0],))
                    cur.execute("""
                        UPDATE price_alert_events SET cancelled_at = NOW()
                        WHERE alert_id = %s AND alert_version = %s
                          AND notified_at IS NULL AND notification_batch_id IS NULL
                    """, (old[0], old[1]))
                continue
            if old:
                new_version = int(old[1]) + 1
                cur.execute("""
                    UPDATE price_alerts
                    SET target_price = %s, effective_from = %s, version = %s, status = 'active'
                    WHERE id = %s
                """, (target, effective, new_version, old[0]))
                cur.execute("""
                    UPDATE price_alert_events SET cancelled_at = NOW()
                    WHERE alert_id = %s AND alert_version < %s
                      AND notified_at IS NULL AND notification_batch_id IS NULL
                """, (old[0], new_version))
            else:
                cur.execute("""
                    INSERT INTO price_alerts
                      (code, direction, target_price, effective_from, status)
                    VALUES (%s, %s, %s, %s, 'active')
                """, (code, direction, target, effective))
        conn.commit()
        return effective
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


def acknowledge_event(event_id: int, code: str) -> None:
    ensure_schema()
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE price_alert_events SET acknowledged_at = NOW()
            WHERE id = %s AND code = %s AND acknowledged_at IS NULL
        """, (event_id, code))
        conn.commit()
    finally:
        cur.close(); conn.close()


def validate_memo(content: str, title: str = "", source_url: str = "") -> tuple[str, str, str]:
    """URLは保存のみ。外部ページの自動取得は行わない。"""
    content, title, source_url = content.strip(), title.strip(), source_url.strip()
    if len(content) > STOCK_MEMO_MAX_LENGTH:
        raise ValueError(f"本文は{STOCK_MEMO_MAX_LENGTH:,}文字以内で入力してください")
    if len(content.encode("utf-8")) > STOCK_MEMO_MAX_BYTES:
        raise ValueError("本文の保存容量を超えています。絵文字などを含む長文は複数のメモに分けてください")
    if len(title) > STOCK_MEMO_TITLE_MAX_LENGTH:
        raise ValueError(f"見出しは{STOCK_MEMO_TITLE_MAX_LENGTH}文字以内で入力してください")
    if len(source_url) > STOCK_MEMO_URL_MAX_LENGTH:
        raise ValueError("URLが長すぎます")
    if source_url:
        try:
            parsed = urlsplit(source_url)
            valid = (parsed.scheme in {"https", "http"} and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not re.search(r"[\s\\\x00-\x1f\x7f]", source_url))
            parsed.port  # 不正なポート指定も拒否する。
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("参照URLは https:// または http:// から始まる有効なURLを入力してください")
    if not (content or source_url):
        raise ValueError("本文または参照URLを入力してください")
    return content, title, source_url


def _save_memo_source(cur, memo_id: int, title: str, source_url: str) -> None:
    cur.execute("""INSERT INTO stock_memo_sources (memo_id, title, source_url)
                   VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE title = VALUES(title), source_url = VALUES(source_url)""",
                (memo_id, title, source_url))


def create_memo(cur, code: str, content: str, title: str = "", source_url: str = "") -> int:
    """呼出元のトランザクションで本文・参考URLをまとめて作成する。"""
    content, title, source_url = validate_memo(content, title, source_url)
    # URLだけの保存も既存ページ・旧版から読めるよう本文にも残す。
    cur.execute("INSERT INTO stock_memos (code, content) VALUES (%s, %s)",
                (code, content or source_url))
    memo_id = int(cur.lastrowid)
    _save_memo_source(cur, memo_id, title, source_url)
    return memo_id


def save_memo(code: str, content: str, title: str = "", source_url: str = "",
              memo_id: int | None = None) -> int:
    content, title, source_url = validate_memo(content, title, source_url)
    ensure_schema()
    with db() as cur:
        if memo_id is None:
            cur.execute("SELECT 1 FROM stocks WHERE code = %s", (code,))
            if not cur.fetchone():
                raise ValueError("銘柄が見つかりません")
            return create_memo(cur, code, content, title, source_url)
        else:
            cur.execute("SELECT id FROM stock_memos WHERE id = %s AND code = %s FOR UPDATE",
                        (memo_id, code))
            if not cur.fetchone():
                raise ValueError("対象のメモが見つかりません。再読み込みしてください")
            cur.execute("UPDATE stock_memos SET content = %s WHERE id = %s AND code = %s",
                        (content or source_url, memo_id, code))
        _save_memo_source(cur, int(memo_id), title, source_url)
    return int(memo_id)


def delete_memo(code: str, memo_id: int) -> None:
    ensure_schema()
    with db() as cur:
        cur.execute("SELECT id FROM stock_memos WHERE id = %s AND code = %s FOR UPDATE", (memo_id, code))
        if not cur.fetchone():
            raise ValueError("対象のメモが見つかりません")
        cur.execute("DELETE FROM stock_memo_sources WHERE memo_id = %s", (memo_id,))
        cur.execute("DELETE FROM stock_memos WHERE id = %s AND code = %s", (memo_id, code))


def migrate_legacy_memos() -> int:
    """旧watchlist.memoを同じ内容が無い場合だけ共通メモへ移す。"""
    ensure_schema()
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO stock_memos (code, content)
            SELECT w.code, w.memo FROM watchlist w
            WHERE w.memo IS NOT NULL AND TRIM(w.memo) <> ''
              AND NOT EXISTS (
                SELECT 1 FROM stock_memos sm
                WHERE sm.code = w.code AND sm.content = w.memo
              )
        """)
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        cur.close(); conn.close()
